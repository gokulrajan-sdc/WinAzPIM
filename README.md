# AzurePIMAccessAutomator

CLI for working with Azure Privileged Identity Management (PIM) eligible role assignments without clicking through the portal. The script supports three workflows:

1. **Batch activation** — define every role you want active in a YAML file and activate them all in one shot.
2. **List** — show every PIM-eligible role you have (direct or inherited via AAD groups), with status (active vs eligible) and expiry.
3. **Single activation** — activate one specific eligibility with a custom justification, polling until Azure reports a terminal status.

## Prerequisites

- Python 3.10+
- Azure CLI installed and logged in (`az login`)
- PIM eligible assignments already configured for your account in Azure AD (directly or via group membership)

```
pip install -r requirements.txt
```

## Dev container (recommended)

A [`.devcontainer`](.devcontainer/devcontainer.json) is included. Open the repo in VS Code and choose **Reopen in Container** — it spins up a `python:3.12-slim` image with the Azure CLI and all Python dependencies pre-installed.

Inside the container:

```
az login
```

## Authentication

The script prefers the existing `az login` session (`AzureCliCredential`). If that fails it falls back to `DefaultAzureCredential`, which also supports:

- Managed Identity (CI/CD pipelines, Azure VMs)
- Environment variables: `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`

`--list` and `--activate` additionally call **Microsoft Graph** to resolve your group memberships (so group-inherited eligibilities show up). Your `az login` session needs the delegated permissions `User.Read` and `GroupMember.Read.All` — both granted by default for typical Azure CLI logins.

## Usage

### List eligible roles

Show every PIM-eligible role you have, including ones inherited via AAD groups, plus current activation status.

```
python activate_pim_roles.py --list
```

```
Found 22 eligible PIM role(s) — 3 currently active:

  STATUS    ROLE         RESOURCE                          VIA    UNTIL
  --------------------------------------------------------------------------
  ACTIVE    Contributor  ft-core-data-services-dev         Group  2026-05-06T18:00:00Z
  ACTIVE    Contributor  ft-core-services-dev              Group  2026-05-06T18:00:00Z
  ACTIVE    Reader       football_analytics_dataplatform   Group  2026-05-06T18:00:00Z
  eligible  Contributor  ft-applications-pat-dev           Group  2026-06-30T14:18:51Z
  ...
```

Columns:
- `STATUS` — `ACTIVE` if currently activated, `eligible` if assigned but not active
- `VIA` — `Direct` (assigned to your user) or `Group` (inherited via an AAD group)
- `UNTIL` — for active rows, when the activation expires; for eligible rows, when the eligibility itself expires (`permanent` if open-ended)

`--list` reads no YAML and uses your active `az` subscription's tenant. To troubleshoot empty output, add `-v` to see per-principal eligibility counts.

### Activate a single role

Activate one eligibility with a custom justification and wait for Azure to confirm.

```
python activate_pim_roles.py --activate \
    --role "Contributor" \
    --resource "ft-applications-pat-dev" \
    --reason "investigating prod ticket #1234"
```

The script:
1. Looks up your eligibilities and finds the one matching `--role` + `--resource` (case-insensitive, displayName match).
2. Submits a self-activation request (`PUT roleAssignmentScheduleRequests/{guid}`).
3. Polls the request every ~3 seconds, logging status transitions, until it hits a terminal state.

Optional flags:

| Flag | Default | Purpose |
|---|---|---|
| `--duration` | `PT8H` | ISO-8601 activation duration (e.g. `PT1H`, `PT4H`). Bounded by your PIM policy. |
| `--no-wait` | off | Submit the request and exit without polling. |
| `--timeout` | `120` | Seconds to wait for a terminal status. Increase for roles that require approval. |
| `-v` / `--verbose` | off | Debug logging. |

Terminal statuses treated as success: `Provisioned`, `Granted`, `AdminApproved`.
Treated as failure: `Failed`, `Denied`, `Canceled`, `AdminDenied`, `TimedOut`, `Revoked`.

For roles requiring admin approval the status will sit at `PendingApproval` until someone approves — bump `--timeout` so the script keeps waiting.

### Batch-activate from YAML

Define every role you want active in `pim_roles.yaml` and activate them all at once. Useful as a "start of day" script.

```
python activate_pim_roles.py
```

#### Setup

```
cp pim_roles.example.yaml pim_roles.yaml
```

`pim_roles.yaml` is git-ignored so your IDs stay off version control.

Find your IDs:

| Field | Azure CLI command |
|---|---|
| `tenant_id` | `az account show --query tenantId -o tsv` |
| `subscription_id` | `az account show --query id -o tsv` |
| `principal_id` | `az ad signed-in-user show --query id -o tsv` |

> If you manage multiple subscriptions, run `az account list` and switch with `az account set --subscription <name-or-id>` before reading those values.

#### Settings

```yaml
settings:
  tenant_id: "..."           # AAD Directory (tenant) ID
  subscription_id: "..."     # Azure Subscription ID
  principal_id: "..."        # Your user Object ID in AAD

  default_duration: "PT8H"   # ISO-8601 duration — check your PIM policy max
  default_justification: "Daily work activation"
  delay_between_requests_seconds: 2  # rate-limit buffer between API calls
```

#### Roles

Each entry activates one eligible PIM role on one resource group:

```yaml
roles:
  - resource: "my-resource-group-dev"
    role: "Contributor"
    resource_type: "ResourceGroup"   # only ResourceGroup is currently supported
    enabled: true

  - resource: "my-resource-group-dev"
    role: "Reader"
    resource_type: "ResourceGroup"
    enabled: true
```

Per-role overrides are supported:

```yaml
  - resource: "sensitive-rg-prod"
    role: "Contributor"
    resource_type: "ResourceGroup"
    duration: "PT1H"                          # override default_duration
    justification: "Production deployment"    # override default_justification
    enabled: true
```

Set `enabled: false` to keep an entry around without activating it.

The batch run prints progress as it goes and a final summary:

```
============================================================
  SUMMARY
============================================================
  ✓  Activated : 18
  ↩  Skipped   : 4
  ✗  Failed    : 0
============================================================
```

`Skipped` means the role was already active or pending — not an error.

> Note: the batch flow does **not** poll for activation completion (it just checks the PUT response code). Use `--activate` for single roles when you need to wait for the terminal status.

## How it discovers group-inherited eligibilities

The Azure portal's "My roles" page shows eligibilities you inherit through AAD group membership (the `Membership: Group` column). To match that, `--list` and `--activate`:

1. Call Microsoft Graph `/me/transitiveMemberOf` to enumerate every group you belong to.
2. Query `roleEligibilityScheduleInstances` at the **tenant root** scope with `$filter=asTarget()` (your direct eligibilities) plus `$filter=principalId eq '<groupId>'` per group.
3. Dedupe by instance ID.

For activation status, the same pattern is applied to `roleAssignmentScheduleInstances`. Each active assignment carries a `linkedRoleEligibilityScheduleId` pointing back to its source eligibility, which is how the `STATUS` column is computed.

When self-activating an eligibility inherited via a group, the `principalId` on the request is your **user** object ID (resolved from `/me`), not the group's — Azure correlates the activation against your transitive group membership.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Config file not found` | You ran the batch flow without `pim_roles.yaml`. Either copy `pim_roles.example.yaml`, or use `--list` / `--activate` (neither needs the YAML). |
| `--list` returns empty but the portal shows eligibilities | Re-run with `-v` to see per-principal counts. If group queries return 0, your `az` session may lack `GroupMember.Read.All`. Re-run `az login` or check with your tenant admin. |
| `No eligibility found matching role=… resource=…` | The role/resource names must match the displayName shown in `--list` exactly (whitespace and special characters included). |
| `Activation did not reach a terminal status within 120s` | The role likely needs admin approval — re-run with a longer `--timeout`, or approve the request in the portal under PIM → My requests. |
| `Failed (403)` | Your principal does not have an eligible assignment for that role/resource, or the active subscription is wrong. |
| `Failed (400)` | `duration` exceeds the maximum allowed by your PIM policy — reduce `--duration` or `default_duration`. |
| `Already active or pending (409)` (batch flow) | Not an error — the role is already active for the session. |
