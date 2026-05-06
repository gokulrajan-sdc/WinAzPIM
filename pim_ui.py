#!/usr/bin/env python3
"""
Azure PIM Role Activator — desktop UI

Tk-based UI that:
  1. Ensures the user is signed in via Azure CLI; offers to launch `az login` if not.
  2. Loads every PIM eligibility (direct + group-inherited) and the matching active
     assignments, displayed in a checkable, sortable table.
  3. Lets the user tick multiple eligibilities and submit a single activation request
     with a shared justification message.
  4. Polls Azure every hour to keep STATUS / UNTIL columns fresh.
  5. Prompts re-login if the underlying credential expires.
"""

import sys
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime

import requests
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import AzureCliCredential, DefaultAzureCredential

from activate_pim_roles import (
    ARM_SCOPE, GRAPH_SCOPE,
    get_az_account, get_signed_in_user_id, get_my_group_ids,
    list_eligible_roles, list_active_assignments,
    activate_role, wait_for_activation,
    _PIM_TERMINAL_OK,
)


REFRESH_INTERVAL_MS = 60 * 60 * 1000  # 1 hour
ACTIVATION_TIMEOUT_S = 180


# ── Auth helpers ──────────────────────────────────────────────────────────────

def is_logged_in() -> bool:
    try:
        get_az_account()
        return True
    except RuntimeError:
        return False


def run_az_login(tenant_id: str | None = None) -> bool:
    """Launch `az login` (opens a browser). Blocks until the CLI returns."""
    cmd = ["az", "login"]
    if tenant_id:
        cmd += ["--tenant", tenant_id]
    result = subprocess.run(cmd, shell=(sys.platform == "win32"))
    return result.returncode == 0


# ── Formatting ────────────────────────────────────────────────────────────────

def format_until(iso: str) -> str:
    """Render an ISO UTC timestamp as 'YYYY-MM-DD HH:MM (in 4h 23m)' in local time."""
    if not iso or iso == "permanent":
        return iso or "permanent"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        delta = dt - datetime.now(tz=dt.tzinfo)
        secs = int(delta.total_seconds())
        local = dt.strftime("%Y-%m-%d %H:%M")
        if secs <= 0:
            return f"{local} (expired)"
        h, rem = divmod(secs, 3600)
        m = rem // 60
        rel = f"{h}h {m}m" if h else f"{m}m"
        return f"{local} (in {rel})"
    except Exception:
        return iso


# ── App ───────────────────────────────────────────────────────────────────────

class PimApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Azure PIM Activator")
        self.root.geometry("1180x680")

        self.credential = None
        self.tenant_id: str | None = None
        self.user_id: str | None = None
        self.group_ids: list[str] = []
        self.eligibilities: list[dict] = []
        self.actives: list[dict] = []
        self.checked: dict[str, bool] = {}
        self.row_data: dict[str, dict] = {}
        self._refresh_after_id: str | None = None

        self._build_ui()
        self.root.after(50, self.bootstrap)

    # ---------------------------------------------------------------- UI build
    def _build_ui(self):
        # Top bar
        topbar = ttk.Frame(self.root, padding=(10, 6))
        topbar.pack(fill="x")
        self.tenant_lbl = ttk.Label(topbar, text="Not signed in.")
        self.tenant_lbl.pack(side="left")
        self.last_refresh_lbl = ttk.Label(topbar, text="", foreground="#666")
        self.last_refresh_lbl.pack(side="left", padx=12)
        self.refresh_btn = ttk.Button(topbar, text="Refresh now", command=self.refresh)
        self.refresh_btn.pack(side="right")

        # Tree
        tree_frame = ttk.Frame(self.root, padding=(10, 0))
        tree_frame.pack(fill="both", expand=True)
        cols = ("check", "status", "role", "resource", "via", "until")
        self.tree = ttk.Treeview(
            tree_frame, columns=cols, show="headings", selectmode="none"
        )
        for col, label, width, anchor, stretch in [
            ("check",    "✓",        36,  "center", False),
            ("status",   "Status",   90,  "w",      False),
            ("role",     "Role",     200, "w",      True),
            ("resource", "Resource", 320, "w",      True),
            ("via",      "Via",      70,  "w",      False),
            ("until",    "Until",    300, "w",      True),
        ]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor=anchor, stretch=stretch)
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.tag_configure("active",      foreground="#1c7c00")
        self.tree.tag_configure("submitting",  foreground="#b07000")

        # Bottom action bar
        bottom = ttk.Frame(self.root, padding=(10, 8))
        bottom.pack(fill="x")
        ttk.Label(bottom, text="Justification:").grid(row=0, column=0, sticky="w")
        self.reason_var = tk.StringVar()
        ttk.Entry(bottom, textvariable=self.reason_var).grid(
            row=0, column=1, sticky="ew", padx=6
        )
        ttk.Label(bottom, text="Duration:").grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.duration_var = tk.StringVar(value="PT8H")
        ttk.Combobox(
            bottom, textvariable=self.duration_var,
            values=["PT1H", "PT2H", "PT4H", "PT8H"],
            width=8, state="readonly",
        ).grid(row=0, column=3, padx=6)
        self.activate_btn = ttk.Button(bottom, text="Activate selected", command=self.on_activate)
        self.activate_btn.grid(row=0, column=4, padx=6)
        bottom.columnconfigure(1, weight=1)

        # Status bar
        self.status_var = tk.StringVar(value="Starting up…")
        ttk.Label(
            self.root, textvariable=self.status_var, anchor="w", padding=(10, 4),
            relief="sunken",
        ).pack(fill="x", side="bottom")

    # --------------------------------------------------------------- Bootstrap
    def bootstrap(self):
        if not is_logged_in():
            ok = messagebox.askokcancel(
                "Sign in to Azure",
                "You're not signed in. Click OK to launch `az login` in your browser.",
            )
            if not ok:
                self.root.destroy()
                return
            self.set_status("Waiting for `az login` to complete…")
            self.root.update_idletasks()
            if not run_az_login() or not is_logged_in():
                messagebox.showerror("Sign-in failed", "Could not sign in via Azure CLI.")
                self.root.destroy()
                return

        try:
            account = get_az_account()
        except RuntimeError as e:
            messagebox.showerror("Azure CLI error", str(e))
            self.root.destroy()
            return
        self.tenant_id = account["tenantId"]
        self.tenant_lbl.config(
            text=f"Tenant: {self.tenant_id}   |   Subscription: {account.get('name', '?')}"
        )

        try:
            self.credential = AzureCliCredential(tenant_id=self.tenant_id)
            self.credential.get_token(ARM_SCOPE)
        except ClientAuthenticationError:
            self.credential = DefaultAzureCredential()

        self.refresh()

    # ----------------------------------------------------------------- Refresh
    def refresh(self):
        if self._refresh_after_id is not None:
            self.root.after_cancel(self._refresh_after_id)
            self._refresh_after_id = None
        self.set_status("Loading eligibilities from Azure…")
        self.refresh_btn.config(state="disabled")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        try:
            arm_token = self.credential.get_token(ARM_SCOPE).token
            graph_token = self.credential.get_token(GRAPH_SCOPE).token
            user_id = get_signed_in_user_id(graph_token)
            group_ids = get_my_group_ids(graph_token)
            eligibilities = list_eligible_roles(arm_token, group_ids)
            actives = list_active_assignments(arm_token, group_ids)
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

        self.root.after(
            0, self._apply_refresh, user_id, group_ids, eligibilities, actives
        )

    def _refresh_failed(self, msg: str):
        self.refresh_btn.config(state="normal")
        self.set_status(f"Refresh failed: {msg}")
        self._schedule_next_refresh()

    def _apply_refresh(self, user_id, group_ids, eligibilities, actives):
        self.user_id = user_id
        self.group_ids = group_ids
        self.eligibilities = eligibilities
        self.actives = actives
        self._populate_tree()
        self.last_refresh_lbl.config(
            text="Last refreshed: " + datetime.now().strftime("%H:%M:%S")
        )
        active_count = sum(1 for r in self.row_data.values() if r["status"] == "ACTIVE")
        self.set_status(
            f"{len(self.row_data)} eligibilit(y/ies) — {active_count} currently active. "
            f"Auto-refresh every 1h."
        )
        self.refresh_btn.config(state="normal")
        self._schedule_next_refresh()

    def _schedule_next_refresh(self):
        self._refresh_after_id = self.root.after(REFRESH_INTERVAL_MS, self.refresh)

    def _populate_tree(self):
        # Map eligibilitySchedule -> active endDateTime
        active_until: dict[str, str] = {}
        for a in self.actives:
            props = a.get("properties", {})
            link = props.get("linkedRoleEligibilityScheduleId")
            if link:
                active_until[link] = props.get("endDateTime") or ""

        prev_checked = self.checked
        self.checked = {}
        self.row_data = {}
        self.tree.delete(*self.tree.get_children())

        rows = []
        for r in self.eligibilities:
            props = r.get("properties", {})
            ep = props.get("expandedProperties", {})
            principal_type = ep.get("principal", {}).get("type", "")
            sched_id = props.get("roleEligibilityScheduleId")
            is_active = sched_id in active_until
            until_iso = active_until[sched_id] if is_active else (props.get("endDateTime") or "")
            rows.append({
                "key":         sched_id or r.get("id"),
                "role":        ep.get("roleDefinition", {}).get("displayName", "?"),
                "resource":    ep.get("scope", {}).get("displayName", props.get("scope", "?")),
                "via":         "Group" if principal_type == "Group" else "Direct",
                "status":      "ACTIVE" if is_active else "eligible",
                "until_iso":   until_iso,
                "until":       format_until(until_iso) if until_iso else "permanent",
                "scope":       props.get("scope"),
                "role_def_id": props.get("roleDefinitionId"),
            })

        rows.sort(key=lambda r: (r["status"] != "ACTIVE", r["role"], r["resource"]))

        for r in rows:
            iid = r["key"]
            checked = prev_checked.get(iid, False)
            self.checked[iid] = checked
            self.row_data[iid] = r
            tags = ("active",) if r["status"] == "ACTIVE" else ()
            self.tree.insert(
                "", "end", iid=iid,
                values=(
                    "☑" if checked else "☐",
                    r["status"], r["role"], r["resource"], r["via"], r["until"],
                ),
                tags=tags,
            )

    # --------------------------------------------------------------- Row click
    def _on_tree_click(self, event):
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.checked[row] = not self.checked.get(row, False)
        vals = list(self.tree.item(row, "values"))
        vals[0] = "☑" if self.checked[row] else "☐"
        self.tree.item(row, values=vals)

    # ---------------------------------------------------------------- Activate
    def on_activate(self):
        selected = [iid for iid, v in self.checked.items() if v]
        # Filter out already-active rows (Azure rejects duplicate activations)
        to_activate = [iid for iid in selected if self.row_data[iid]["status"] != "ACTIVE"]
        skipped_active = len(selected) - len(to_activate)

        if not to_activate:
            messagebox.showwarning(
                "Nothing to activate",
                "Tick one or more eligible (not already active) rows first.",
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

        self.activate_btn.config(state="disabled")
        self.refresh_btn.config(state="disabled")
        msg = f"Submitting {len(to_activate)} activation request(s)…"
        if skipped_active:
            msg += f" ({skipped_active} already-active row(s) skipped.)"
        self.set_status(msg)
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

        results: list[tuple[str, str, str]] = []
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

    def _mark_row_status(self, iid, status):
        if iid not in self.row_data:
            return
        vals = list(self.tree.item(iid, "values"))
        vals[1] = status
        tags = ("submitting",) if status == "submitting" else ()
        self.tree.item(iid, values=vals, tags=tags)

    def _activate_done(self, results):
        self.activate_btn.config(state="normal")
        self.refresh_btn.config(state="normal")
        ok = [r for r in results if r[1] in _PIM_TERMINAL_OK]
        bad = [r for r in results if r[1] not in _PIM_TERMINAL_OK]
        self.set_status(f"Activation complete: {len(ok)} succeeded, {len(bad)} failed.")
        if bad:
            details = "\n".join(
                f"  • {label}: {status}{(' — ' + detail) if detail else ''}"
                for label, status, detail in bad
            )
            messagebox.showwarning("Some activations did not succeed", details)
        # Clear selection and re-fetch so STATUS / UNTIL reflect the new state
        for iid in list(self.checked.keys()):
            self.checked[iid] = False
        self.reason_var.set("")
        self.refresh()

    # ----------------------------------------------------------------- Re-auth
    def _handle_auth_failure(self, detail: str):
        self.refresh_btn.config(state="normal")
        self.activate_btn.config(state="normal")
        if not messagebox.askokcancel(
            "Session expired",
            f"Your Azure session expired ({detail}). Click OK to sign in again.",
        ):
            self.root.destroy()
            return
        self.set_status("Re-authenticating…")
        self.root.update_idletasks()
        if not run_az_login(self.tenant_id):
            messagebox.showerror("Sign-in failed", "Could not sign in via Azure CLI.")
            self.root.destroy()
            return
        try:
            self.credential = AzureCliCredential(tenant_id=self.tenant_id)
            self.credential.get_token(ARM_SCOPE)
        except Exception as e:
            messagebox.showerror("Auth failed", str(e))
            self.root.destroy()
            return
        self.refresh()

    # ----------------------------------------------------------------- Helpers
    def set_status(self, text: str):
        self.status_var.set(text)


def main():
    root = tk.Tk()
    style = ttk.Style()
    if "vista" in style.theme_names():
        style.theme_use("vista")
    PimApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
