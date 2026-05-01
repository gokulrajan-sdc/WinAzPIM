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
import time
import uuid
import logging
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


def activate_role(
    token: str,
    scope: str,
    role_definition_id: str,
    principal_id: str,
    duration: str,
    justification: str,
) -> requests.Response:
    """Submit a PIM self-activation request."""
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
    return resp


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
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
            resp = activate_role(
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
