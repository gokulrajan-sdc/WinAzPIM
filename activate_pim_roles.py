#!/usr/bin/env python3
"""
Azure PIM Role Activator
Reads pim_roles.yaml and activates each enabled eligible role in sequence.

Requirements:
    pip install -r requirements.txt

Authentication:
    Relies on DefaultAzureCredential — works with:
      - `az login` (interactive, recommended for local use)
      - Managed Identity (CI/CD or VM)
      - Environment variables (AZURE_CLIENT_ID / SECRET / TENANT_ID)
"""

import sys
import json
import time
import uuid
import argparse
import logging
import subprocess
from pathlib import Path
from urllib.parse import quote

import yaml
import requests
from azure.identity import DefaultAzureCredential, AzureCliCredential

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
ARM_SCOPE   = "https://management.azure.com/.default"
ARM_BASE    = "https://management.azure.com"
API_VERSION = "2020-10-01"

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_BASE  = "https://graph.microsoft.com/v1.0"


# ── Helpers ────────────────────────────────────────────────────────────────────

def load_config(path: str = "pim_roles.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        log.error("Config file not found: %s", config_path.resolve())
        sys.exit(1)
    with open(config_path) as f:
        return yaml.safe_load(f)


def get_token(credential) -> str:
    token = credential.get_token(ARM_SCOPE)
    return token.token


_WIN_NO_WINDOW = 0x08000000


def get_az_account() -> dict:
    """Return the active `az account show` payload (tenantId, id, user, ...)."""
    kwargs: dict = dict(capture_output=True, text=True)
    if sys.platform == "win32":
        kwargs["creationflags"] = _WIN_NO_WINDOW
    result = subprocess.run(["az", "account", "show", "-o", "json"], **kwargs)
    if result.returncode != 0:
        raise RuntimeError(
            "Could not read active Azure subscription via `az account show`. "
            "Run `az login` first.\n" + result.stderr.strip()
        )
    return json.loads(result.stdout)


def build_rg_scope(subscription_id: str, resource_group: str) -> str:
    """Return the full ARM scope path for a resource group."""
    return f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"


def list_role_definitions(
    token: str, scope: str, role_name: str
) -> str | None:
    """Find the role definition ID for a given role name under a scope."""
    url = f"{ARM_BASE}{scope}/providers/Microsoft.Authorization/roleDefinitions"
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "api-version": API_VERSION,
        "$filter": f"roleName eq '{quote(role_name)}'",
    }
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json().get("value", [])
    if not data:
        return None
    return data[0]["id"]  # full ARM id, e.g. /subscriptions/.../roleDefinitions/<guid>


def get_signed_in_user_id(graph_token: str) -> str:
    """Return the AAD object ID of the signed-in user via Microsoft Graph."""
    resp = requests.get(
        f"{GRAPH_BASE}/me?$select=id",
        headers={"Authorization": f"Bearer {graph_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def get_my_group_ids(graph_token: str) -> list[str]:
    """Return AAD object IDs of every group the signed-in user belongs to (transitively)."""
    ids: list[str] = []
    url = f"{GRAPH_BASE}/me/transitiveMemberOf?$select=id&$top=999"
    headers = {"Authorization": f"Bearer {graph_token}"}
    while url:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        ids.extend(item["id"] for item in data.get("value", []) if "id" in item)
        url = data.get("@odata.nextLink")
    return ids


def _list_pim_instances(
    arm_token: str, resource_path: str, principal_ids: list[str]
) -> list[dict]:
    """Generic tenant-root PIM listing for either eligibility or active-assignment instances.

    `resource_path` is e.g. "roleEligibilityScheduleInstances" or
    "roleAssignmentScheduleInstances". Queries asTarget() for the user plus
    `principalId eq` for each group, deduping by instance id.
    """
    seen: set[str] = set()
    out: list[dict] = []
    url = f"{ARM_BASE}/providers/Microsoft.Authorization/{resource_path}"
    headers = {"Authorization": f"Bearer {arm_token}"}

    queries = [("asTarget()", "signed-in user")]
    queries += [(f"principalId eq '{pid}'", f"group {pid}") for pid in principal_ids]

    for filt, label in queries:
        params = {"api-version": API_VERSION, "$filter": filt}
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
        except requests.HTTPError as e:
            log.debug("Skipping %s on %s: %s", label, resource_path, e)
            continue
        items = resp.json().get("value", [])
        log.debug("  %-22s %s → %d", resource_path, label, len(items))
        for item in items:
            key = item.get("id") or item.get("name")
            if key and key not in seen:
                seen.add(key)
                out.append(item)
    return out


def list_eligible_roles(arm_token: str, principal_ids: list[str]) -> list[dict]:
    return _list_pim_instances(arm_token, "roleEligibilityScheduleInstances", principal_ids)


def list_active_assignments(arm_token: str, principal_ids: list[str]) -> list[dict]:
    return _list_pim_instances(arm_token, "roleAssignmentScheduleInstances", principal_ids)


def print_eligible_roles(eligibilities: list[dict], actives: list[dict]) -> None:
    """Pretty-print the eligible role list, marking which ones are currently activated."""
    if not eligibilities:
        print("No eligible PIM roles found for this principal.")
        return

    # An active assignment links back to the eligibility schedule it was activated from.
    # Map: eligibilityScheduleId -> active endDateTime (when the activation expires).
    active_until: dict[str, str] = {}
    for a in actives:
        props = a.get("properties", {})
        link = props.get("linkedRoleEligibilityScheduleId")
        if link:
            active_until[link] = props.get("endDateTime") or ""

    rows = []
    for r in eligibilities:
        props = r.get("properties", {})
        ep = props.get("expandedProperties", {})
        principal_type = ep.get("principal", {}).get("type", "")
        sched_id = props.get("roleEligibilityScheduleId")
        is_active = sched_id in active_until
        rows.append({
            "role":   ep.get("roleDefinition", {}).get("displayName", "?"),
            "scope":  ep.get("scope", {}).get("displayName", props.get("scope", "?")),
            "via":    "Group" if principal_type == "Group" else "Direct",
            "status": "ACTIVE" if is_active else "eligible",
            "until":  active_until[sched_id] if is_active else (props.get("endDateTime") or "permanent"),
        })

    rows.sort(key=lambda r: (r["status"] != "ACTIVE", r["role"], r["scope"]))

    role_w   = max(len(r["role"])   for r in rows + [{"role":   "ROLE"}])
    scope_w  = max(len(r["scope"])  for r in rows + [{"scope":  "RESOURCE"}])
    via_w    = max(len(r["via"])    for r in rows + [{"via":    "VIA"}])
    status_w = max(len(r["status"]) for r in rows + [{"status": "STATUS"}])

    active_count = sum(1 for r in rows if r["status"] == "ACTIVE")
    print(f"\nFound {len(rows)} eligible PIM role(s) — {active_count} currently active:\n")
    header = f"  {'STATUS':<{status_w}}  {'ROLE':<{role_w}}  {'RESOURCE':<{scope_w}}  {'VIA':<{via_w}}  UNTIL"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in rows:
        print(f"  {r['status']:<{status_w}}  {r['role']:<{role_w}}  {r['scope']:<{scope_w}}  {r['via']:<{via_w}}  {r['until']}")
    print()


def activate_role(
    token: str,
    scope: str,
    role_definition_id: str,
    principal_id: str,
    duration: str,
    justification: str,
) -> tuple[requests.Response, str]:
    """Submit a PIM self-activation request. Returns (response, request_url)."""
    request_id = str(uuid.uuid4())
    url = (
        f"{ARM_BASE}{scope}/providers/Microsoft.Authorization"
        f"/roleAssignmentScheduleRequests/{request_id}"
        f"?api-version={API_VERSION}"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {
        "properties": {
            "principalId": principal_id,
            "roleDefinitionId": role_definition_id,
            "requestType": "SelfActivate",
            "justification": justification,
            "scheduleInfo": {
                "startDateTime": None,
                "expiration": {
                    "type": "AfterDuration",
                    "duration": duration,
                },
            },
        }
    }
    resp = requests.put(url, headers=headers, json=body, timeout=30)
    return resp, url


def deactivate_role(
    token: str,
    scope: str,
    role_definition_id: str,
    principal_id: str,
    assignment_schedule_id: str,
) -> requests.Response:
    """Submit a PIM self-deactivation request."""
    request_id = str(uuid.uuid4())
    url = (
        f"{ARM_BASE}{scope}/providers/Microsoft.Authorization"
        f"/roleAssignmentScheduleRequests/{request_id}"
        f"?api-version={API_VERSION}"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {
        "properties": {
            "principalId": principal_id,
            "roleDefinitionId": role_definition_id,
            "requestType": "SelfDeactivate",
            "linkedRoleAssignmentScheduleId": assignment_schedule_id,
        }
    }
    return requests.put(url, headers=headers, json=body, timeout=30)


# Terminal statuses on a roleAssignmentScheduleRequest, per Azure PIM docs.
_PIM_TERMINAL_OK    = {"Provisioned", "Granted", "AdminApproved"}
_PIM_TERMINAL_FAIL  = {"Failed", "Denied", "Canceled", "AdminDenied", "TimedOut", "Revoked"}


def wait_for_activation(
    token: str, request_url: str, timeout_s: int = 120, interval_s: float = 3.0
) -> tuple[str, dict]:
    """Poll a roleAssignmentScheduleRequests URL until it reaches a terminal status.

    Returns (status, last_payload). Raises TimeoutError if no terminal status within timeout.
    """
    headers = {"Authorization": f"Bearer {token}"}
    deadline = time.monotonic() + timeout_s
    last_status = ""
    while time.monotonic() < deadline:
        resp = requests.get(request_url, headers=headers, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        status = payload.get("properties", {}).get("status", "")
        if status != last_status:
            log.info("  status: %s", status or "<pending>")
            last_status = status
        if status in _PIM_TERMINAL_OK or status in _PIM_TERMINAL_FAIL:
            return status, payload
        time.sleep(interval_s)
    raise TimeoutError(f"Activation did not reach a terminal status within {timeout_s}s (last: {last_status!r})")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Activate or list Azure PIM eligible roles.")
    parser.add_argument(
        "--list",
        action="store_true",
        help="List eligible PIM roles for the signed-in user and exit (no activation).",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging (per-principal eligibility counts, etc.).",
    )
    parser.add_argument(
        "--activate",
        action="store_true",
        help="Activate a single eligibility identified by --role and --resource.",
    )
    parser.add_argument("--role",     help="Role name (e.g. 'Contributor'). Used with --activate.")
    parser.add_argument("--resource", help="Resource (e.g. resource-group name). Used with --activate.")
    parser.add_argument("--reason",   help="Justification text for the activation. Used with --activate.")
    parser.add_argument("--duration", default="PT8H", help="ISO-8601 duration (default PT8H).")
    parser.add_argument("--no-wait",  action="store_true",
                        help="Don't poll for completion — submit and return immediately.")
    parser.add_argument("--timeout",  type=int, default=120,
                        help="Seconds to wait for activation to reach a terminal status (default 120).")
    args = parser.parse_args()
    if args.verbose:
        log.setLevel(logging.DEBUG)

    # --list mode: no YAML required — read tenant from the active az session
    if args.list:
        try:
            account = get_az_account()
        except RuntimeError as exc:
            log.error(str(exc))
            sys.exit(1)
        tenant_id = account["tenantId"]
        log.info("Tenant: %s  (active subscription: %s)", tenant_id, account.get("name", "?"))

        try:
            credential = AzureCliCredential(tenant_id=tenant_id)
            arm_token   = credential.get_token(ARM_SCOPE).token
            graph_token = credential.get_token(GRAPH_SCOPE).token
        except Exception as exc:
            log.debug("AzureCliCredential failed (%s); trying DefaultAzureCredential.", exc)
            credential = DefaultAzureCredential()
            arm_token   = credential.get_token(ARM_SCOPE).token
            graph_token = credential.get_token(GRAPH_SCOPE).token

        group_ids = get_my_group_ids(graph_token)
        log.info("Resolved %d group membership(s); querying PIM eligibilities tenant-wide...", len(group_ids))

        eligible = list_eligible_roles(arm_token, group_ids)
        active   = list_active_assignments(arm_token, group_ids)
        print_eligible_roles(eligible, active)
        return

    # --activate mode: pick one eligibility from the live list and submit it
    if args.activate:
        missing = [n for n, v in [("--role", args.role), ("--resource", args.resource), ("--reason", args.reason)] if not v]
        if missing:
            log.error("--activate requires: %s", ", ".join(missing))
            sys.exit(2)

        try:
            account = get_az_account()
        except RuntimeError as exc:
            log.error(str(exc))
            sys.exit(1)
        tenant_id = account["tenantId"]
        log.info("Tenant: %s", tenant_id)

        try:
            credential = AzureCliCredential(tenant_id=tenant_id)
            arm_token   = credential.get_token(ARM_SCOPE).token
            graph_token = credential.get_token(GRAPH_SCOPE).token
        except Exception as exc:
            log.debug("AzureCliCredential failed (%s); trying DefaultAzureCredential.", exc)
            credential = DefaultAzureCredential()
            arm_token   = credential.get_token(ARM_SCOPE).token
            graph_token = credential.get_token(GRAPH_SCOPE).token

        user_id   = get_signed_in_user_id(graph_token)
        group_ids = get_my_group_ids(graph_token)
        eligibilities = list_eligible_roles(arm_token, group_ids)

        # Match on role + resource displayName (case-insensitive)
        want_role     = args.role.casefold()
        want_resource = args.resource.casefold()
        matches = []
        for r in eligibilities:
            ep = r.get("properties", {}).get("expandedProperties", {})
            role_name = ep.get("roleDefinition", {}).get("displayName", "")
            scope_name = ep.get("scope", {}).get("displayName", "")
            if role_name.casefold() == want_role and scope_name.casefold() == want_resource:
                matches.append(r)

        if not matches:
            log.error("No eligibility found matching role=%r resource=%r. Run --list to see available entries.",
                      args.role, args.resource)
            sys.exit(1)
        if len(matches) > 1:
            log.error("Ambiguous: %d eligibilities match role=%r resource=%r.", len(matches), args.role, args.resource)
            sys.exit(1)

        elig = matches[0]
        props = elig["properties"]
        scope = props["scope"]
        role_def_id = props["roleDefinitionId"]
        log.info("Activating %s @ %s for %s ...", args.role, args.resource, args.duration)
        log.info("  scope: %s", scope)
        log.info("  reason: %s", args.reason)

        try:
            resp, request_url = activate_role(
                arm_token, scope, role_def_id, user_id, args.duration, args.reason
            )
        except requests.RequestException as e:
            log.error("Request failed: %s", e)
            sys.exit(1)

        if resp.status_code not in (200, 201):
            try:
                detail = resp.json().get("error", {}).get("message", resp.text[:300])
            except ValueError:
                detail = resp.text[:300]
            log.error("Activation request rejected (%d): %s", resp.status_code, detail)
            sys.exit(1)

        log.info("Request accepted (HTTP %d).", resp.status_code)

        if args.no_wait:
            print(f"\nRequest submitted. Track it at:\n  {request_url}")
            return

        try:
            status, payload = wait_for_activation(arm_token, request_url, timeout_s=args.timeout)
        except TimeoutError as e:
            log.error(str(e))
            sys.exit(1)

        if status in _PIM_TERMINAL_OK:
            end_time = payload.get("properties", {}).get("scheduleInfo", {}).get("expiration", {})
            log.info("✓ Activated. Final status: %s", status)
            print(f"\n  ✓  {args.role} @ {args.resource} is now active (status={status}).")
            return
        else:
            err = payload.get("properties", {}).get("status", status)
            msg = payload.get("properties", {}).get("statusDetail") or payload.get("properties", {}).get("justification", "")
            log.error("✗ Activation did not succeed. Final status: %s. %s", err, msg)
            sys.exit(1)

    config   = load_config()
    settings = config["settings"]
    roles    = config["roles"]

    tenant_id       = settings["tenant_id"]
    subscription_id = settings["subscription_id"]
    principal_id    = settings["principal_id"]
    default_duration      = settings.get("default_duration", "PT8H")
    default_justification = settings.get("default_justification", "Daily activation")
    delay           = settings.get("delay_between_requests_seconds", 2)

    # Authenticate — prefers `az login` session, falls back to env vars / MI
    try:
        credential = AzureCliCredential(tenant_id=tenant_id)
        token = get_token(credential)
        log.info("Authenticated via Azure CLI.")
    except Exception as exc:
        log.debug("AzureCliCredential failed (%s); trying DefaultAzureCredential.", exc)
        credential = DefaultAzureCredential()
        token = get_token(credential)
        log.info("Authenticated via DefaultAzureCredential.")

    enabled_roles = [r for r in roles if r.get("enabled", True)]
    log.info("Found %d enabled role(s) to activate.", len(enabled_roles))

    results = {"success": [], "skipped": [], "failed": []}

    for idx, entry in enumerate(enabled_roles, start=1):
        resource      = entry["resource"]
        role_name     = entry["role"]
        duration      = entry.get("duration", default_duration)
        justification = entry.get("justification", default_justification)

        label = "[%d/%d] %s @ %s" % (idx, len(enabled_roles), role_name, resource)
        log.info("Processing  %s ...", label)

        scope = build_rg_scope(subscription_id, resource)

        # Refresh token periodically (token valid ~1hr)
        if idx % 20 == 0:
            token = get_token(credential)
            log.info("Token refreshed.")

        # Resolve role definition ID
        try:
            role_def_id = list_role_definitions(token, scope, role_name)
        except requests.HTTPError as e:
            log.warning("  Could not resolve role definition: %s", e)
            results["failed"].append(label)
            continue

        if not role_def_id:
            log.warning("  Role definition '%s' not found at scope — skipping.", role_name)
            results["skipped"].append(label)
            continue

        # Submit activation
        try:
            resp, _ = activate_role(
                token, scope, role_def_id, principal_id, duration, justification
            )
            if resp.status_code in (200, 201):
                log.info("  ✓  Activated successfully.")
                results["success"].append(label)
            elif resp.status_code == 409:
                # Already active — not an error
                detail = resp.json().get("error", {}).get("message", "")
                log.info("  ↩  Already active or pending: %s", detail)
                results["skipped"].append(label)
            else:
                detail = resp.json().get("error", {}).get("message", resp.text[:200])
                log.warning("  ✗  Failed (%d): %s", resp.status_code, detail)
                results["failed"].append(label)
        except requests.RequestException as e:
            log.error("  ✗  Request error: %s", e)
            results["failed"].append(label)

        time.sleep(delay)

    # ── Summary ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"  SUMMARY")
    print("=" * 60)
    print(f"  ✓  Activated : {len(results['success'])}")
    print(f"  ↩  Skipped   : {len(results['skipped'])}")
    print(f"  ✗  Failed    : {len(results['failed'])}")
    if results["failed"]:
        print("\n  Failed roles:")
        for r in results["failed"]:
            print(f"    - {r}")
    print("=" * 60)


if __name__ == "__main__":
    main()
