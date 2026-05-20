#!/usr/bin/env python3
"""
Azure PIM Role Activator — desktop UI

On launch:
  1. Authenticates silently via the existing `az login` session; falls back to
     an interactive browser sign-in (no Azure CLI required).
  2. Fetches all available subscriptions from `az account list` and populates
     the subscription / tenant dropdown.
  3. Loads PIM eligibilities and active assignments for the selected tenant.
  4. Changing the dropdown re-authenticates (if needed) and refreshes the list.
  5. Polls Azure every hour to keep STATUS / UNTIL columns fresh.
"""

import base64
import concurrent.futures
import json
import logging
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from datetime import datetime
from typing import Optional

import webbrowser

import requests
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import AzureCliCredential, DeviceCodeCredential
import customtkinter as ctk

from activate_pim_roles import (
    ARM_SCOPE, GRAPH_SCOPE,
    get_signed_in_user_id, get_my_group_ids,
    list_eligible_roles, list_active_assignments,
    activate_role, deactivate_role, wait_for_activation,
    _PIM_TERMINAL_OK,
    _WIN_NO_WINDOW,
)

log = logging.getLogger(__name__)

REFRESH_INTERVAL_MS = 60 * 60 * 1000
ACTIVATION_TIMEOUT_S = 180

# ── ISO normalization (Python ≤3.10 fromisoformat sub-second bug) ─────────────
_SUB_SEC = re.compile(r'\.\d+')


def _norm_iso(iso: str) -> str:
    return _SUB_SEC.sub(lambda m: (m.group() + "000000")[:7], iso).replace("Z", "+00:00")


def format_until(iso: str) -> str:
    if not iso or iso == "permanent":
        return iso or "permanent"
    try:
        dt = datetime.fromisoformat(_norm_iso(iso)).astimezone()
        delta = dt - datetime.now(tz=dt.tzinfo)
        secs = int(delta.total_seconds())
        local = dt.strftime("%Y-%m-%d %H:%M")
        if secs <= 0:
            return f"{local}  (expired)"
        h, rem = divmod(secs, 3600)
        m = rem // 60
        rel = f"{h}h {m}m" if h else f"{m}m"
        return f"{local}  (in {rel})"
    except Exception:
        return iso


def expiry_urgency(until_iso: str) -> str:
    """Returns 'critical' (<30 min), 'warn' (<2 h), or '' (healthy/not active)."""
    if not until_iso or until_iso == "permanent":
        return ""
    try:
        dt = datetime.fromisoformat(_norm_iso(until_iso)).astimezone()
        secs = int((dt - datetime.now(tz=dt.tzinfo)).total_seconds())
        if secs <= 0 or secs < 30 * 60:
            return "critical"
        if secs < 2 * 3600:
            return "warn"
        return ""
    except Exception:
        return ""


# ── Color palette ─────────────────────────────────────────────────────────────
_PALETTE = {
    "Dark": {
        "app_bg":           "#0D1117",
        "surface":          "#161B22",
        "surface2":         "#21262D",
        "border":           "#30363D",
        "text_primary":     "#E6EDF3",
        "text_secondary":   "#8B949E",
        "text_muted":       "#656D76",
        "accent_blue":      "#58A6FF",
        "accent_green":     "#3FB950",
        "accent_amber":     "#D29922",
        "accent_red":       "#F85149",
        "row_active_bg":    "#0D2818",
        "row_eligible_bg":  "#0C1B2E",
        "row_warn_bg":      "#1C1700",
        "row_critical_bg":  "#200D0D",
    },
    "Light": {
        "app_bg":           "#F6F8FA",
        "surface":          "#FFFFFF",
        "surface2":         "#F0F2F4",
        "border":           "#D0D7DE",
        "text_primary":     "#1F2328",
        "text_secondary":   "#636C76",
        "text_muted":       "#9198A1",
        "accent_blue":      "#0969DA",
        "accent_green":     "#1A7F37",
        "accent_amber":     "#9A6700",
        "accent_red":       "#D1242F",
        "row_active_bg":    "#DAFBE1",
        "row_eligible_bg":  "#DDF4FF",
        "row_warn_bg":      "#FFF8C5",
        "row_critical_bg":  "#FFEBE9",
    },
}

# ── Azure helpers (UI layer) ──────────────────────────────────────────────────

def list_az_subscriptions() -> list:
    """Return enabled subscriptions from `az account list --all`.

    On Windows, az.cmd lives in the shell PATH but is not a standalone
    executable, so we route through cmd /c (matching AzureCliCredential's
    own approach) instead of invoking az directly.
    """
    kwargs: dict = dict(capture_output=True, text=True)
    if sys.platform == "win32":
        cmd = ["cmd", "/c", "az account list --output json --all"]
        kwargs["creationflags"] = _WIN_NO_WINDOW
    else:
        cmd = ["az", "account", "list", "--output", "json", "--all"]
    try:
        result = subprocess.run(cmd, **kwargs)
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []
    try:
        subs = json.loads(result.stdout)
        return [s for s in subs if s.get("state", "Enabled") == "Enabled"]
    except Exception:
        return []


def _try_cli_credential(tenant_id: Optional[str]):
    """Return an AzureCliCredential if the existing `az login` session is valid."""
    cred = (
        AzureCliCredential(tenant_id=tenant_id) if tenant_id
        else AzureCliCredential()
    )
    cred.get_token(ARM_SCOPE)   # raises if no valid session
    return cred


# ── In-app device-code login dialog ──────────────────────────────────────────

class _LoginDialog(ctk.CTkToplevel):
    """Modal dialog that guides the user through the device-code auth flow.

    Lifecycle
    ---------
    1. Created by `PimApp._device_code_auth()` on the main thread.
    2. `show_code()` is called (main thread) once the device flow starts —
       displays the URL, code, and countdown.
    3. `close_success()` is called (main thread) once the background auth
       thread obtains a token — closes the dialog silently.
    4. If the user presses Cancel, `self.cancelled` is set to True; the
       background thread will detect this and abort.
    """

    def __init__(self, parent: ctk.CTk, P: dict):
        super().__init__(parent)
        self.title("Sign in to Azure")
        self.geometry("500x380")
        self.resizable(False, False)
        self.lift()
        self.focus_force()
        self.grab_set()

        self.cancelled = False
        self._P = P
        self._countdown_job = None

        self.configure(fg_color=P["surface"])
        self._build_loading_state(P)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

    # ── Phase 1: loading state ─────────────────────────────────────────────

    def _build_loading_state(self, P: dict):
        self._loading_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._loading_frame.place(relx=0.5, rely=0.5, anchor="center")

        ctk.CTkLabel(
            self._loading_frame,
            text="Starting sign-in flow…",
            font=ctk.CTkFont(size=14),
            text_color=P["text_secondary"],
        ).pack()

        self._spin_bar = ttk.Progressbar(
            self._loading_frame, mode="indeterminate", length=220
        )
        self._spin_bar.pack(pady=(14, 0))
        self._spin_bar.start(10)

    # ── Phase 2: show device code ──────────────────────────────────────────

    def show_code(self, verification_uri: str, user_code: str, expires_on: datetime):
        """Transition from loading state to the code-display state."""
        self._spin_bar.stop()
        self._loading_frame.place_forget()

        P = self._P
        self._expires_on = expires_on

        pad = {"padx": 32}

        # Title
        ctk.CTkLabel(
            self, text="Sign in to Azure",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=P["text_primary"],
        ).pack(pady=(28, 4), **pad, anchor="w")

        ctk.CTkLabel(
            self, text="Open your browser, go to the URL below, and enter the code.",
            font=ctk.CTkFont(size=12),
            text_color=P["text_secondary"],
        ).pack(**pad, anchor="w")

        # URL
        ctk.CTkLabel(
            self, text="Sign-in URL",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=P["text_muted"],
        ).pack(pady=(18, 3), **pad, anchor="w")

        url_frame = ctk.CTkFrame(
            self, fg_color=P["surface2"],
            border_width=1, border_color=P["border"], corner_radius=6,
        )
        url_frame.pack(fill="x", **pad)
        ctk.CTkLabel(
            url_frame, text=verification_uri,
            font=ctk.CTkFont(size=12),
            text_color=P["accent_blue"],
        ).pack(side="left", padx=10, pady=8)

        # Code
        ctk.CTkLabel(
            self, text="Your code",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=P["text_muted"],
        ).pack(pady=(14, 3), **pad, anchor="w")

        code_frame = ctk.CTkFrame(
            self, fg_color=P["surface2"],
            border_width=1, border_color=P["border"], corner_radius=6,
        )
        code_frame.pack(fill="x", **pad)
        ctk.CTkLabel(
            code_frame, text=user_code,
            font=ctk.CTkFont(size=26, weight="bold"),
            text_color=P["text_primary"],
        ).pack(side="left", padx=16, pady=10)

        # Buttons row
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", **pad, pady=(14, 0))

        ctk.CTkButton(
            btn_frame, text="Copy code",
            width=130, height=34,
            fg_color=P["surface2"], border_width=1, border_color=P["border"],
            text_color=P["text_primary"], hover_color=P["border"],
            font=ctk.CTkFont(size=12),
            command=lambda: self._copy_and_open(user_code, verification_uri, open_browser=False),
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_frame, text="Copy code & open browser",
            width=200, height=34,
            fg_color=P["accent_blue"], hover_color="#388BFD",
            text_color="#FFFFFF",
            font=ctk.CTkFont(size=12),
            command=lambda: self._copy_and_open(user_code, verification_uri, open_browser=True),
        ).pack(side="left")

        # Status row
        status_row = ctk.CTkFrame(self, fg_color="transparent")
        status_row.pack(fill="x", **pad, pady=(14, 0))

        self._status_lbl = ctk.CTkLabel(
            status_row, text="⟳  Waiting for sign-in…",
            font=ctk.CTkFont(size=12),
            text_color=P["text_secondary"],
        )
        self._status_lbl.pack(side="left")

        self._countdown_lbl = ctk.CTkLabel(
            status_row, text="",
            font=ctk.CTkFont(size=11),
            text_color=P["text_muted"],
        )
        self._countdown_lbl.pack(side="right")

        # Cancel
        ctk.CTkButton(
            self, text="Cancel",
            width=90, height=30,
            fg_color="transparent", border_width=1, border_color=P["border"],
            text_color=P["text_muted"], hover_color=P["surface2"],
            font=ctk.CTkFont(size=11),
            command=self._on_cancel,
        ).pack(pady=(12, 20))

        self._tick_countdown()

    # ── Helpers ────────────────────────────────────────────────────────────

    def _copy_and_open(self, code: str, uri: str, open_browser: bool):
        self.clipboard_clear()
        self.clipboard_append(code)
        if open_browser:
            webbrowser.open(uri)

    def _tick_countdown(self):
        try:
            remaining = int((self._expires_on - datetime.now(self._expires_on.tzinfo)).total_seconds())
        except Exception:
            return
        if remaining <= 0:
            self._countdown_lbl.configure(text="Code expired")
            self._status_lbl.configure(text="Code expired — please restart sign-in")
            return
        m, s = divmod(remaining, 60)
        self._countdown_lbl.configure(text=f"Expires in {m}:{s:02d}")
        self._countdown_job = self.after(1000, self._tick_countdown)

    def close_success(self):
        if self._countdown_job:
            self.after_cancel(self._countdown_job)
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.cancelled = True
        if self._countdown_job:
            self.after_cancel(self._countdown_job)
        self.grab_release()
        self.destroy()


def decode_tenant_from_token(token: str) -> str:
    try:
        _, payload_b64, _ = token.split(".", 2)
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("tid", "") or payload.get("tenant_id", "")
    except Exception:
        return ""


# ── Main application ──────────────────────────────────────────────────────────

class PimApp:
    def __init__(self, root: ctk.CTk):
        self.root = root
        self.root.title("Azure PIM Activator")
        self.root.geometry("1300x780")
        self.root.minsize(900, 600)

        # ── Runtime state ──
        self.credential = None
        self.tenant_id: Optional[str] = None
        self.user_id: Optional[str] = None
        self.user_name: str = ""
        self.group_ids: list = []
        self.row_data: dict = {}
        self.checked: dict = {}
        self.auto_renew_data: dict = {}       # iid -> justification str
        self._refresh_after_id = None
        self._subscriptions: list = []        # raw dicts from az account list
        self._sub_display_map: dict = {}      # display label -> sub dict
        self._filter_active = tk.BooleanVar(value=True)
        self._filter_eligible = tk.BooleanVar(value=True)
        self._theme_name = "Dark"

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self._build_ui()
        self.root.after(120, self.bootstrap)

    # ── Palette shorthand ──────────────────────────────────────────────────────

    def _p(self) -> dict:
        return _PALETTE[self._theme_name]

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        P = self._p()
        self.root.configure(fg_color=P["app_bg"])

        self._build_topbar(P)
        self._build_subbar(P)

        # 1-px divider
        ctk.CTkFrame(self.root, fg_color=P["border"], height=1,
                     corner_radius=0).pack(fill="x")

        self._build_content(P)

        # 1-px divider
        ctk.CTkFrame(self.root, fg_color=P["border"], height=1,
                     corner_radius=0).pack(fill="x")

        self._build_actionbar(P)
        self._build_statusbar(P)

    # -- Topbar ----------------------------------------------------------------

    def _build_topbar(self, P: dict):
        topbar = ctk.CTkFrame(
            self.root, fg_color=P["surface"], corner_radius=0, height=52
        )
        topbar.pack(fill="x", side="top")
        topbar.pack_propagate(False)

        ctk.CTkLabel(
            topbar, text="  ⬡  Azure PIM Activator",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=P["text_primary"],
        ).pack(side="left", padx=(16, 0))

        self.auth_dot = ctk.CTkLabel(
            topbar, text="⬤", text_color=P["text_muted"],
            font=ctk.CTkFont(size=9),
        )
        self.auth_dot.pack(side="left", padx=(10, 4))

        self.user_lbl = ctk.CTkLabel(
            topbar, text="Not signed in",
            text_color=P["text_secondary"], font=ctk.CTkFont(size=12),
        )
        self.user_lbl.pack(side="left")

        # Right-aligned controls
        for text, cmd, attr in [
            ("☾  Dark",    self._toggle_theme,  "theme_btn"),
            ("Sign out",   self._sign_out,       "signout_btn"),
            ("↻  Refresh", self.refresh,         "refresh_btn"),
        ]:
            btn = ctk.CTkButton(
                topbar, text=text, width=90, height=32,
                fg_color="transparent", border_width=1,
                border_color=P["border"], text_color=P["text_secondary"],
                hover_color=P["surface2"], font=ctk.CTkFont(size=12),
                command=cmd,
                state="disabled" if attr in ("signout_btn", "refresh_btn") else "normal",
            )
            btn.pack(side="right", padx=(0, 10))
            setattr(self, attr, btn)

        self.last_refresh_lbl = ctk.CTkLabel(
            topbar, text="", text_color=P["text_muted"],
            font=ctk.CTkFont(size=11),
        )
        self.last_refresh_lbl.pack(side="right", padx=(0, 8))

    # -- Subscription bar -------------------------------------------------------

    def _build_subbar(self, P: dict):
        subbar = ctk.CTkFrame(
            self.root, fg_color=P["surface2"], corner_radius=0, height=50
        )
        subbar.pack(fill="x", side="top")
        subbar.pack_propagate(False)

        ctk.CTkLabel(
            subbar, text="Subscription",
            text_color=P["text_secondary"], font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(side="left", padx=(16, 8))

        self.sub_var = tk.StringVar(value="Authenticating…")
        self.sub_combo = ctk.CTkComboBox(
            subbar,
            variable=self.sub_var,
            values=["Authenticating…"],
            width=520, height=34,
            font=ctk.CTkFont(size=13),
            fg_color=P["surface"],
            border_color=P["border"],
            text_color=P["text_primary"],
            button_color=P["border"],
            button_hover_color=P["surface2"],
            dropdown_fg_color=P["surface"],
            dropdown_text_color=P["text_primary"],
            dropdown_hover_color=P["surface2"],
            state="disabled",
            command=self._on_sub_change,
        )
        self.sub_combo.pack(side="left", padx=(0, 16))

        self.tenant_lbl = ctk.CTkLabel(
            subbar, text="",
            text_color=P["text_muted"], font=ctk.CTkFont(size=11),
        )
        self.tenant_lbl.pack(side="left")

    # -- Main content ----------------------------------------------------------

    def _build_content(self, P: dict):
        content = ctk.CTkFrame(self.root, fg_color=P["app_bg"], corner_radius=0)
        content.pack(fill="both", expand=True)

        self._build_sidebar(content, P)

        table_outer = ctk.CTkFrame(content, fg_color=P["app_bg"], corner_radius=0)
        table_outer.pack(side="left", fill="both", expand=True)

        self._build_table(table_outer, P)
        self._build_loading_overlay(table_outer, P)

    def _build_sidebar(self, parent, P: dict):
        sidebar = ctk.CTkFrame(
            parent, fg_color=P["surface"], corner_radius=0, width=210
        )
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        ctk.CTkLabel(
            sidebar, text="FILTERS",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=P["text_muted"],
        ).pack(anchor="w", padx=18, pady=(20, 8))

        self.filter_active_cb = ctk.CTkCheckBox(
            sidebar, text="Active  (0)",
            variable=self._filter_active,
            command=self._apply_filters,
            font=ctk.CTkFont(size=13),
            text_color=P["text_primary"],
            fg_color=P["accent_green"],
            hover_color=P["accent_green"],
            border_color=P["border"],
            checkmark_color="#FFFFFF",
            checkbox_width=20, checkbox_height=20,
        )
        self.filter_active_cb.pack(anchor="w", padx=18, pady=5)

        self.filter_eligible_cb = ctk.CTkCheckBox(
            sidebar, text="Eligible  (0)",
            variable=self._filter_eligible,
            command=self._apply_filters,
            font=ctk.CTkFont(size=13),
            text_color=P["text_primary"],
            fg_color=P["accent_blue"],
            hover_color=P["accent_blue"],
            border_color=P["border"],
            checkmark_color="#FFFFFF",
            checkbox_width=20, checkbox_height=20,
        )
        self.filter_eligible_cb.pack(anchor="w", padx=18, pady=5)

        ctk.CTkFrame(
            sidebar, fg_color=P["border"], height=1, corner_radius=0
        ).pack(fill="x", padx=18, pady=(18, 10))

        ctk.CTkLabel(
            sidebar, text="URGENCY",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=P["text_muted"],
        ).pack(anchor="w", padx=18, pady=(0, 6))

        for dot_color, label_text in [
            (P["accent_green"], "Active  (> 2 h)"),
            (P["accent_amber"], "Expiring  (< 2 h)"),
            (P["accent_red"],   "Critical  (< 30 m)"),
        ]:
            row_f = ctk.CTkFrame(sidebar, fg_color="transparent", corner_radius=0)
            row_f.pack(fill="x", padx=18, pady=3)
            ctk.CTkLabel(
                row_f, text="⬤", text_color=dot_color,
                font=ctk.CTkFont(size=9),
            ).pack(side="left", padx=(0, 7))
            ctk.CTkLabel(
                row_f, text=label_text,
                text_color=P["text_secondary"], font=ctk.CTkFont(size=12),
            ).pack(side="left")

        ctk.CTkFrame(
            sidebar, fg_color=P["border"], height=1, corner_radius=0
        ).pack(fill="x", padx=18, pady=(18, 10))

        ctk.CTkLabel(
            sidebar, text="TIPS",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=P["text_muted"],
        ).pack(anchor="w", padx=18, pady=(0, 6))

        for tip in ("☑  Click cell to check", "⊘  Click to toggle renew",
                    "Right-click to deactivate"):
            ctk.CTkLabel(
                sidebar, text=tip,
                text_color=P["text_muted"], font=ctk.CTkFont(size=11),
                justify="left",
            ).pack(anchor="w", padx=18, pady=2)

    # -- Table -----------------------------------------------------------------

    def _build_table(self, parent, P: dict):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "PIM.Treeview",
            background=P["surface"],
            foreground=P["text_primary"],
            fieldbackground=P["surface"],
            borderwidth=0,
            rowheight=38,
            font=("Segoe UI", 12),
        )
        style.configure(
            "PIM.Treeview.Heading",
            background=P["surface2"],
            foreground=P["text_secondary"],
            borderwidth=0,
            relief="flat",
            font=("Segoe UI", 11, "bold"),
        )
        style.map(
            "PIM.Treeview",
            background=[("selected", P["surface2"])],
            foreground=[("selected", P["text_primary"])],
        )
        style.map(
            "PIM.Treeview.Heading",
            background=[("active", P["surface2"])],
        )

        cols = ("sel", "status", "role", "resource", "via", "until", "renew")
        self.tree = ttk.Treeview(
            parent, columns=cols, show="headings",
            selectmode="none", style="PIM.Treeview",
        )

        col_cfg = [
            ("sel",      "☑",          42,  "center", False),
            ("status",   "Status",     115,  "w",     False),
            ("role",     "Role",       230,  "w",     True),
            ("resource", "Resource",   270,  "w",     True),
            ("via",      "Via",         68,  "center",False),
            ("until",    "Until",      245,  "w",     True),
            ("renew",    "Auto-renew",  94,  "center",False),
        ]
        for col, label, width, anchor, stretch in col_cfg:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor=anchor,
                             stretch=stretch, minwidth=width)

        vsb = ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        vsb.pack(side="right", fill="y", pady=6, padx=(0, 4))

        self.tree.tag_configure(
            "active",          foreground=P["accent_green"], background=P["row_active_bg"])
        self.tree.tag_configure(
            "active_warn",     foreground=P["accent_amber"], background=P["row_warn_bg"])
        self.tree.tag_configure(
            "active_critical", foreground=P["accent_red"],   background=P["row_critical_bg"])
        self.tree.tag_configure(
            "eligible",        foreground=P["accent_blue"],  background=P["row_eligible_bg"])
        self.tree.tag_configure(
            "submitting",      foreground=P["accent_amber"], background=P["row_warn_bg"])

        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Button-3>", self._on_tree_right_click)
        self._table_parent = parent

    # -- Loading overlay -------------------------------------------------------

    def _build_loading_overlay(self, parent, P: dict):
        self._overlay = ctk.CTkFrame(
            parent, corner_radius=16, width=340, height=148,
            fg_color=P["surface"], border_width=1, border_color=P["border"],
        )
        ctk.CTkLabel(
            self._overlay, text="Loading PIM Roles",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=P["text_primary"],
        ).place(relx=0.5, rely=0.26, anchor="center")

        self._overlay_lbl = ctk.CTkLabel(
            self._overlay, text="Authenticating…",
            font=ctk.CTkFont(size=12),
            text_color=P["text_secondary"],
        )
        self._overlay_lbl.place(relx=0.5, rely=0.52, anchor="center")

        self._overlay_bar = ttk.Progressbar(
            self._overlay, mode="indeterminate", length=260,
        )
        self._overlay_bar.place(relx=0.5, rely=0.76, anchor="center")

    def _show_loading(self, text: str = ""):
        if text:
            self._overlay_lbl.configure(text=text)
        self._overlay.place(relx=0.5, rely=0.5, anchor="center")
        self._overlay_bar.start(12)

    def _hide_loading(self):
        self._overlay_bar.stop()
        self._overlay.place_forget()

    def _update_loading(self, text: str):
        self.root.after(0, lambda: self._overlay_lbl.configure(text=text))

    # -- Action bar ------------------------------------------------------------

    def _build_actionbar(self, P: dict):
        actionbar = ctk.CTkFrame(
            self.root, fg_color=P["surface"], corner_radius=0, height=64
        )
        actionbar.pack(fill="x", side="bottom")
        actionbar.pack_propagate(False)

        ctk.CTkLabel(
            actionbar, text="Justification",
            text_color=P["text_secondary"], font=ctk.CTkFont(size=12),
        ).pack(side="left", padx=(16, 6))

        self.reason_var = tk.StringVar()
        ctk.CTkEntry(
            actionbar, textvariable=self.reason_var,
            width=340, height=36,
            font=ctk.CTkFont(size=13),
            fg_color=P["surface2"], border_color=P["border"],
            text_color=P["text_primary"],
            placeholder_text="Why are you activating these roles?",
        ).pack(side="left", padx=(0, 16))

        ctk.CTkLabel(
            actionbar, text="Duration",
            text_color=P["text_secondary"], font=ctk.CTkFont(size=12),
        ).pack(side="left", padx=(0, 6))

        self.duration_var = tk.StringVar(value="PT8H")
        ctk.CTkComboBox(
            actionbar,
            variable=self.duration_var,
            values=["PT1H", "PT2H", "PT4H", "PT8H"],
            width=95, height=36,
            font=ctk.CTkFont(size=13),
            fg_color=P["surface2"], border_color=P["border"],
            text_color=P["text_primary"], button_color=P["border"],
            button_hover_color=P["surface2"],
            dropdown_fg_color=P["surface"],
            dropdown_text_color=P["text_primary"],
            state="readonly",
        ).pack(side="left", padx=(0, 20))

        # Selected-count badge
        self.selected_badge = ctk.CTkFrame(
            actionbar, fg_color=P["surface2"],
            corner_radius=8, width=36, height=36,
        )
        self.selected_badge.pack(side="left", padx=(0, 5))
        self.selected_badge.pack_propagate(False)
        self.selected_lbl = ctk.CTkLabel(
            self.selected_badge, text="0",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=P["text_muted"],
        )
        self.selected_lbl.place(relx=0.5, rely=0.5, anchor="center")

        ctk.CTkLabel(
            actionbar, text="selected",
            text_color=P["text_muted"], font=ctk.CTkFont(size=12),
        ).pack(side="left", padx=(0, 16))

        self.activate_btn = ctk.CTkButton(
            actionbar, text="⚡  Activate",
            width=126, height=38,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=P["accent_green"], hover_color="#2EA043",
            text_color="#FFFFFF",
            state="disabled",
            command=self.on_activate,
        )
        self.activate_btn.pack(side="left")

    # -- Status bar ------------------------------------------------------------

    def _build_statusbar(self, P: dict):
        self.status_var = tk.StringVar(value="Initializing…")
        bar = ctk.CTkFrame(
            self.root, fg_color=P["surface2"], corner_radius=0, height=26
        )
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        ctk.CTkLabel(
            bar, textvariable=self.status_var,
            text_color=P["text_muted"], font=ctk.CTkFont(size=11),
            anchor="w",
        ).pack(fill="x", padx=12, pady=3)

    # ── Bootstrap / auth ──────────────────────────────────────────────────────

    def bootstrap(self):
        self._show_loading("Connecting to Azure CLI…")
        self.set_status("Authenticating silently via Azure CLI…")
        threading.Thread(target=self._auth_worker, args=(None,), daemon=True).start()

    def _auth_worker(self, tenant_id: Optional[str]):
        """Background: authenticate + fetch subscriptions + resolve identity."""
        try:
            self._update_loading("Signing in to Azure…")
            # 1. Try existing az-login session silently
            try:
                credential = _try_cli_credential(tenant_id)
            except Exception:
                # 2. No CLI session — show the in-app device-code dialog
                credential = self._device_code_auth(tenant_id)
                if credential is None:
                    return  # user cancelled; _auth_failed already called

            arm_token = credential.get_token(ARM_SCOPE).token
            tid = tenant_id or decode_tenant_from_token(arm_token)

            self._update_loading("Fetching available subscriptions…")
            subscriptions = list_az_subscriptions()

            self._update_loading("Resolving user identity…")
            try:
                graph_token = credential.get_token(GRAPH_SCOPE).token
                resp = requests.get(
                    "https://graph.microsoft.com/v1.0/me"
                    "?$select=id,displayName,userPrincipalName",
                    headers={"Authorization": f"Bearer {graph_token}"},
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
                user_id = data.get("id", "")
                user_name = data.get("displayName") or data.get("userPrincipalName", "")
            except Exception:
                user_id = ""
                user_name = ""

        except ClientAuthenticationError as e:
            self.root.after(0, self._auth_failed, str(e))
            return
        except Exception as e:
            self.root.after(0, self._auth_failed, f"Unexpected error: {e}")
            return

        self.root.after(
            0, self._auth_done, credential, tid, subscriptions, user_id, user_name
        )

    def _device_code_auth(self, tenant_id: Optional[str]):
        """Show an in-app device-code dialog and return a DeviceCodeCredential.

        Called from the background auth thread. Schedules the dialog onto the
        main thread via root.after(), then blocks waiting for either success
        (token acquired) or cancellation (user closed the dialog).
        """
        dialog_ref: list = [None]
        dialog_ready = threading.Event()
        cancelled = threading.Event()

        def _show_dialog():
            dlg = _LoginDialog(self.root, self._p())
            dialog_ref[0] = dlg
            dialog_ready.set()

        self.root.after(0, _show_dialog)
        dialog_ready.wait(timeout=10)

        self._update_loading("Waiting for sign-in…")
        self.root.after(0, lambda: self.set_status(
            "Sign-in required — complete the steps in the dialog."
        ))

        def _prompt_cb(verification_uri: str, user_code: str, expires_on):
            def _update():
                dlg = dialog_ref[0]
                if dlg and not dlg.cancelled:
                    dlg.show_code(verification_uri, user_code, expires_on)
            self.root.after(0, _update)

        try:
            cred = DeviceCodeCredential(
                tenant_id=tenant_id or "organizations",
                prompt_callback=_prompt_cb,
            )
            # Blocks here — DeviceCodeCredential polls until the user signs in
            # or the device code expires (~15 min by default).
            cred.get_token(ARM_SCOPE)

            # Auth succeeded — close the dialog cleanly
            def _close():
                dlg = dialog_ref[0]
                if dlg and not dlg.cancelled:
                    dlg.close_success()
            self.root.after(0, _close)
            return cred

        except Exception as e:
            dlg = dialog_ref[0]
            if dlg and dlg.cancelled:
                # User clicked Cancel — treat as graceful exit
                self.root.after(0, self._auth_failed, "Sign-in was cancelled.")
            else:
                self.root.after(0, self._auth_failed, str(e))
            return None

    def _auth_done(self, credential, tenant_id: str,
                   subscriptions: list, user_id: str, user_name: str):
        self.credential = credential
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.user_name = user_name
        self._subscriptions = subscriptions

        # Build dropdown values
        self._sub_display_map = {}
        display_values = []
        selected_label = None

        for sub in subscriptions:
            label = f"{sub.get('name', 'Unknown')}  —  {sub.get('id', '?')}"
            self._sub_display_map[label] = sub
            display_values.append(label)
            if sub.get("tenantId") == tenant_id and selected_label is None:
                selected_label = label
            if sub.get("isDefault") and selected_label is None:
                selected_label = label

        if not display_values:
            # CLI not available — synthesize a single entry for the authenticated tenant
            label = f"Tenant: {tenant_id}"
            display_values = [label]
            selected_label = label

        self.sub_combo.configure(values=display_values, state="normal")
        self.sub_var.set(selected_label or display_values[0])

        if tenant_id:
            self.tenant_lbl.configure(text=f"Tenant ID: {tenant_id}")

        # Update auth indicator
        self.auth_dot.configure(text_color="#3FB950")
        display = user_name or (f"tenant {tenant_id[:8]}…" if tenant_id else "signed in")
        self.user_lbl.configure(text=display)
        self.signout_btn.configure(state="normal")
        self.refresh_btn.configure(state="normal")
        self.set_status(f"Signed in as {display}  —  loading roles…")
        self.refresh()

    def _auth_failed(self, detail: str):
        self._hide_loading()
        messagebox.showerror(
            "Sign-in failed",
            f"Could not sign in to Azure.\n\n{detail}\n\n"
            "Tip: run  az login  in a terminal first, then restart the app.",
        )
        self.root.destroy()

    # ── Subscription change ───────────────────────────────────────────────────

    def _on_sub_change(self, selected_label: str):
        sub = self._sub_display_map.get(selected_label)
        if not sub:
            return
        new_tenant = sub.get("tenantId", "")
        if new_tenant:
            self.tenant_lbl.configure(text=f"Tenant ID: {new_tenant}")

        if new_tenant and new_tenant != self.tenant_id:
            # Different tenant — must re-authenticate before refreshing
            self._show_loading(f"Switching tenant…")
            self.set_status("Switching tenant — re-authenticating…")
            self.refresh_btn.configure(state="disabled")
            threading.Thread(
                target=self._auth_worker, args=(new_tenant,), daemon=True
            ).start()
        else:
            self.refresh()

    # ── Refresh ───────────────────────────────────────────────────────────────

    def refresh(self):
        if self._refresh_after_id:
            self.root.after_cancel(self._refresh_after_id)
            self._refresh_after_id = None
        self._show_loading("Connecting to Azure…")
        self.set_status("Loading eligibilities from Azure…")
        self.refresh_btn.configure(state="disabled")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        try:
            self._update_loading("Acquiring tokens…")
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                arm_f   = ex.submit(self.credential.get_token, ARM_SCOPE)
                graph_f = ex.submit(self.credential.get_token, GRAPH_SCOPE)
            arm_token   = arm_f.result().token
            graph_token = graph_f.result().token

            self._update_loading("Resolving identity & group memberships…")
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                user_f  = ex.submit(get_signed_in_user_id, graph_token)
                group_f = ex.submit(get_my_group_ids, graph_token)
            user_id   = user_f.result()
            group_ids = group_f.result()

            self._update_loading("Loading PIM eligibilities & active assignments…")
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                elig_f   = ex.submit(list_eligible_roles,     arm_token, group_ids)
                active_f = ex.submit(list_active_assignments, arm_token, group_ids)
            eligibilities = elig_f.result()
            actives       = active_f.result()

        except ClientAuthenticationError as e:
            self.root.after(0, self._handle_auth_failure, str(e))
            return
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                self.root.after(0, self._handle_auth_failure, "401 Unauthorized")
                return
            self.root.after(0, self._refresh_failed, str(e))
            return
        except Exception as e:
            self.root.after(0, self._refresh_failed, str(e))
            return

        self.root.after(0, self._apply_refresh, user_id, group_ids, eligibilities, actives)

    def _refresh_failed(self, msg: str):
        self._hide_loading()
        self.refresh_btn.configure(state="normal")
        self.set_status(f"Refresh failed: {msg}")
        self._schedule_next_refresh()

    def _apply_refresh(self, user_id, group_ids, eligibilities, actives):
        if user_id:
            self.user_id = user_id
        self.group_ids = group_ids
        self._populate_tree(eligibilities, actives)
        now = datetime.now().strftime("%H:%M:%S")
        self.last_refresh_lbl.configure(text=f"Updated {now}")
        active_n = sum(1 for r in self.row_data.values() if r["status"] == "Active")
        elig_n   = sum(1 for r in self.row_data.values() if r["status"] == "Eligible")
        self.set_status(
            f"{active_n} active · {elig_n} eligible · auto-refresh in 1 h"
        )
        self.refresh_btn.configure(state="normal")
        self._update_filter_counts()
        self._hide_loading()
        self._schedule_next_refresh()

    def _schedule_next_refresh(self):
        self._refresh_after_id = self.root.after(REFRESH_INTERVAL_MS, self.refresh)

    # ── Tree population ───────────────────────────────────────────────────────

    def _populate_tree(self, eligibilities: list, actives: list):
        # Map eligibility schedule ID → (endDateTime, assignment schedule ID)
        active_until: dict = {}
        active_sched_ids: dict = {}
        for a in actives:
            props = a.get("properties", {})
            link  = props.get("linkedRoleEligibilityScheduleId")
            if link:
                active_until[link]     = props.get("endDateTime") or ""
                active_sched_ids[link] = props.get("roleAssignmentScheduleId", "")

        prev_checked = dict(self.checked)
        self.checked  = {}
        self.row_data = {}
        self.tree.delete(*self.tree.get_children())

        rows = []
        for r in eligibilities:
            props = r.get("properties", {})
            ep    = props.get("expandedProperties", {})
            principal_type = ep.get("principal", {}).get("type", "")
            sched_id  = props.get("roleEligibilityScheduleId")
            is_active = sched_id in active_until
            until_iso = (
                active_until[sched_id] if is_active
                else (props.get("endDateTime") or "")
            )
            rows.append({
                "key":               sched_id or r.get("id"),
                "role":              ep.get("roleDefinition", {}).get("displayName", "?"),
                "resource":          ep.get("scope", {}).get(
                                         "displayName", props.get("scope", "?")),
                "via":               "Group" if principal_type == "Group" else "Direct",
                "status":            "Active" if is_active else "Eligible",
                "until_iso":         until_iso,
                "until":             format_until(until_iso) if until_iso else "permanent",
                "scope":             props.get("scope"),
                "role_def_id":       props.get("roleDefinitionId"),
                "assignment_sched_id": active_sched_ids.get(sched_id, ""),
            })

        rows.sort(key=lambda r: (r["status"] != "Active", r["role"], r["resource"]))

        for r in rows:
            iid = r["key"]
            self.checked[iid]  = prev_checked.get(iid, False)
            self.row_data[iid] = r
            self.tree.insert(
                "", "end", iid=iid,
                values=self._row_values(iid),
                tags=(self._row_tag(iid),),
            )

        self._update_selected_label()

    def _row_tag(self, iid: str) -> str:
        r = self.row_data[iid]
        if r["status"] == "Eligible":
            return "eligible"
        urgency = expiry_urgency(r.get("until_iso", ""))
        if urgency == "critical":
            return "active_critical"
        if urgency == "warn":
            return "active_warn"
        return "active"

    def _row_values(self, iid: str) -> tuple:
        r = self.row_data[iid]
        if r["status"] == "Eligible":
            sel    = "☑" if self.checked.get(iid) else "☐"
            status = "○  Eligible"
            until  = r["until"]
            renew  = ""
        else:
            sel    = ""
            status = "●  Active"
            urgency = expiry_urgency(r.get("until_iso", ""))
            prefix = "⚑  " if urgency == "critical" else "⚐  " if urgency == "warn" else ""
            until  = prefix + r["until"]
            renew  = "⊙  ON" if iid in self.auto_renew_data else "⊘  —"
        return (sel, status, r["role"], r["resource"], r["via"], until, renew)

    # ── Click handlers ────────────────────────────────────────────────────────

    def _on_tree_click(self, event):
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        iid = self.tree.identify_row(event.y)
        if not iid or iid not in self.row_data:
            return
        col_id = self.tree.identify_column(event.x)
        r = self.row_data[iid]

        if col_id == "#1" and r["status"] == "Eligible":
            # Toggle checkbox
            self.checked[iid] = not self.checked.get(iid, False)
            self.tree.item(iid, values=self._row_values(iid))
            self._update_selected_label()
        elif col_id == "#7" and r["status"] == "Active":
            # Toggle auto-renew inline
            self._toggle_auto_renew(iid)

    def _on_tree_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid or iid not in self.row_data:
            return
        r = self.row_data[iid]
        if r["status"] != "Active":
            return
        ctx = tk.Menu(self.root, tearoff=0)
        ctx.add_command(
            label=f"Deactivate:  {r['role']}",
            command=lambda: self._deactivate_prompt([iid]),
        )
        ctx.add_separator()
        if iid in self.auto_renew_data:
            ctx.add_command(
                label="Disable auto-renew",
                command=lambda: self._toggle_auto_renew(iid),
            )
        else:
            ctx.add_command(
                label="Enable auto-renew…",
                command=lambda: self._toggle_auto_renew(iid),
            )
        try:
            ctx.tk_popup(event.x_root, event.y_root)
        finally:
            ctx.grab_release()

    # ── Auto-renew ────────────────────────────────────────────────────────────

    def _toggle_auto_renew(self, iid: str):
        if iid in self.auto_renew_data:
            del self.auto_renew_data[iid]
            self.tree.item(iid, values=self._row_values(iid))
        else:
            r = self.row_data[iid]
            justification = simpledialog.askstring(
                "Auto-renew justification",
                f"Enter justification for auto-renewing:\n"
                f"  {r['role']}  @  {r['resource']}",
                parent=self.root,
            )
            if justification and justification.strip():
                self.auto_renew_data[iid] = justification.strip()
                self.tree.item(iid, values=self._row_values(iid))

    # ── Filters ───────────────────────────────────────────────────────────────

    def _apply_filters(self):
        show_active   = self._filter_active.get()
        show_eligible = self._filter_eligible.get()
        self.tree.delete(*self.tree.get_children())
        for iid, r in self.row_data.items():
            if r["status"] == "Active"   and not show_active:
                continue
            if r["status"] == "Eligible" and not show_eligible:
                continue
            self.tree.insert(
                "", "end", iid=iid,
                values=self._row_values(iid),
                tags=(self._row_tag(iid),),
            )

    def _update_filter_counts(self):
        active_n = sum(1 for r in self.row_data.values() if r["status"] == "Active")
        elig_n   = sum(1 for r in self.row_data.values() if r["status"] == "Eligible")
        self.filter_active_cb.configure(text=f"Active  ({active_n})")
        self.filter_eligible_cb.configure(text=f"Eligible  ({elig_n})")

    def _update_selected_label(self):
        n = sum(
            1 for iid, v in self.checked.items()
            if v and self.row_data.get(iid, {}).get("status") == "Eligible"
        )
        self.selected_lbl.configure(text=str(n))
        P = self._p()
        if n > 0:
            self.selected_badge.configure(fg_color=P["row_active_bg"])
            self.selected_lbl.configure(text_color=P["accent_green"])
            self.activate_btn.configure(state="normal")
        else:
            self.selected_badge.configure(fg_color=P["surface2"])
            self.selected_lbl.configure(text_color=P["text_muted"])
            self.activate_btn.configure(state="disabled")

    # ── Activate ──────────────────────────────────────────────────────────────

    def on_activate(self):
        to_activate = [
            iid for iid, v in self.checked.items()
            if v and self.row_data.get(iid, {}).get("status") == "Eligible"
        ]
        if not to_activate:
            messagebox.showwarning(
                "Nothing to activate",
                "Select one or more eligible rows first.",
            )
            return
        reason = self.reason_var.get().strip()
        if not reason:
            messagebox.showwarning(
                "Justification required",
                "Enter a justification message in the field at the bottom.",
            )
            return
        duration = self.duration_var.get().strip() or "PT8H"

        self.activate_btn.configure(state="disabled")
        self.refresh_btn.configure(state="disabled")
        self.set_status(f"Submitting {len(to_activate)} activation request(s)…")
        threading.Thread(
            target=self._activate_worker,
            args=(to_activate, reason, duration),
            daemon=True,
        ).start()

    def _activate_worker(self, ids, reason, duration):
        try:
            arm_token = self.credential.get_token(ARM_SCOPE).token
        except ClientAuthenticationError as e:
            self.root.after(0, self._handle_auth_failure, str(e))
            return

        results = []
        for iid in ids:
            row = self.row_data.get(iid)
            if not row:
                continue
            label = f"{row['role']} @ {row['resource']}"
            self.root.after(0, self._mark_row_status, iid, "submitting")
            try:
                resp, request_url = activate_role(
                    arm_token, row["scope"], row["role_def_id"],
                    self.user_id, duration, reason,
                )
                if resp.status_code not in (200, 201):
                    try:
                        detail = resp.json().get("error", {}).get("message", resp.text[:200])
                    except ValueError:
                        detail = resp.text[:200] or f"HTTP {resp.status_code}"
                    results.append((label, "rejected", detail))
                    continue
                status, _ = wait_for_activation(
                    arm_token, request_url, timeout_s=ACTIVATION_TIMEOUT_S
                )
                results.append((label, status, ""))
            except Exception as e:
                results.append((label, "error", str(e)))

        self.root.after(0, self._activate_done, results)

    def _activate_done(self, results):
        self.activate_btn.configure(state="normal")
        self.refresh_btn.configure(state="normal")
        ok  = [r for r in results if r[1] in _PIM_TERMINAL_OK]
        bad = [r for r in results if r[1] not in _PIM_TERMINAL_OK]
        self.set_status(
            f"Activation complete: {len(ok)} succeeded, {len(bad)} failed."
        )
        if bad:
            details = "\n".join(
                f"  • {label}: {status}"
                + (f"  —  {detail}" if detail else "")
                for label, status, detail in bad
            )
            messagebox.showwarning("Some activations did not succeed", details)
        for iid in list(self.checked):
            self.checked[iid] = False
        self.reason_var.set("")
        self.refresh()

    # ── Deactivate ────────────────────────────────────────────────────────────

    def _deactivate_prompt(self, ids: list):
        names = "\n".join(
            f"  • {self.row_data[i]['role']}  @  {self.row_data[i]['resource']}"
            for i in ids if i in self.row_data
        )
        if not messagebox.askokcancel(
            "Confirm deactivation",
            f"Deactivate the following role(s)?\n\n{names}",
        ):
            return
        self.set_status(f"Deactivating {len(ids)} role(s)…")
        self.refresh_btn.configure(state="disabled")
        threading.Thread(
            target=self._deactivate_worker, args=(ids,), daemon=True
        ).start()

    def _deactivate_worker(self, ids: list):
        try:
            arm_token = self.credential.get_token(ARM_SCOPE).token
        except ClientAuthenticationError as e:
            self.root.after(0, self._handle_auth_failure, str(e))
            return

        results = []
        for iid in ids:
            row = self.row_data.get(iid)
            if not row:
                continue
            label = f"{row['role']} @ {row['resource']}"
            self.root.after(0, self._mark_row_status, iid, "submitting")
            try:
                resp = deactivate_role(
                    arm_token, row["scope"], row["role_def_id"],
                    self.user_id, row["assignment_sched_id"],
                )
                if resp.status_code in (200, 201):
                    results.append((label, "Deactivated", ""))
                else:
                    try:
                        detail = resp.json().get("error", {}).get("message", resp.text[:200])
                    except ValueError:
                        detail = resp.text[:200]
                    results.append((label, "error", detail))
            except Exception as e:
                results.append((label, "error", str(e)))

        self.root.after(0, self._deactivate_done, results)

    def _deactivate_done(self, results):
        self.refresh_btn.configure(state="normal")
        ok  = [r for r in results if r[1] == "Deactivated"]
        bad = [r for r in results if r[1] != "Deactivated"]
        self.set_status(
            f"Deactivation: {len(ok)} succeeded, {len(bad)} failed."
        )
        if bad:
            details = "\n".join(
                f"  • {label}: {detail}" for label, _, detail in bad
            )
            messagebox.showwarning("Deactivation issues", details)
        self.refresh()

    # ── Auth failure / sign-out ───────────────────────────────────────────────

    def _handle_auth_failure(self, detail: str):
        self._hide_loading()
        self.refresh_btn.configure(state="normal")
        if not messagebox.askokcancel(
            "Session expired",
            f"Your Azure session expired.\n\n{detail}\n\nClick OK to sign in again.",
        ):
            self.root.destroy()
            return
        self._show_loading("Re-authenticating…")
        self.set_status("Re-authenticating…")
        threading.Thread(
            target=self._auth_worker, args=(self.tenant_id,), daemon=True
        ).start()

    def _sign_out(self):
        if self._refresh_after_id:
            self.root.after_cancel(self._refresh_after_id)
            self._refresh_after_id = None
        self.credential = None
        self.tenant_id  = None
        self.user_id    = None
        self.user_name  = ""
        self.row_data   = {}
        self.checked    = {}
        P = self._p()
        self.auth_dot.configure(text_color=P["text_muted"])
        self.user_lbl.configure(text="Not signed in")
        self.signout_btn.configure(state="disabled")
        self.refresh_btn.configure(state="disabled")
        self.sub_combo.configure(state="disabled", values=["Not signed in"])
        self.sub_var.set("Not signed in")
        self.tenant_lbl.configure(text="")
        self.tree.delete(*self.tree.get_children())
        self._update_filter_counts()
        self.set_status("Signed out.")

    # ── Row helpers ───────────────────────────────────────────────────────────

    def _mark_row_status(self, iid: str, status_text: str):
        if iid not in self.row_data:
            return
        vals = list(self.tree.item(iid, "values"))
        vals[1] = status_text
        self.tree.item(iid, values=vals, tags=("submitting",))

    # ── Theme toggle ──────────────────────────────────────────────────────────

    def _toggle_theme(self):
        if self._theme_name == "Dark":
            self._theme_name = "Light"
            ctk.set_appearance_mode("light")
            self.theme_btn.configure(text="☀  Light")
        else:
            self._theme_name = "Dark"
            ctk.set_appearance_mode("dark")
            self.theme_btn.configure(text="☾  Dark")

    # ── Status bar ────────────────────────────────────────────────────────────

    def set_status(self, text: str):
        self.status_var.set(text)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    root.configure(fg_color=_PALETTE["Dark"]["app_bg"])
    PimApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
