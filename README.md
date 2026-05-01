# AzurePIMAccessAutomator

Batch-activates Azure Privileged Identity Management (PIM) eligible role assignments in one shot. Instead of clicking through the Azure Portal for each resource group and role individually, you define all your roles in a YAML file and run the script once.

## How it works

The script reads `pim_roles.yaml`, iterates over every enabled role entry, and calls the Azure ARM API (`roleAssignmentScheduleRequests`) to self-activate each eligible assignment. Results are summarised at the end with counts of activated / already-active / failed roles.

## Prerequisites

- Python 3.10+
- Azure CLI installed and logged in (`az login`)
- PIM eligible assignments already configured for your account in Azure AD

```
pip install -r requirements.txt
```

## Dev container (recommended)

A [`.devcontainer`](.devcontainer/devcontainer.json) is included. Open the repo in VS Code and choose **Reopen in Container** — it will spin up a `python:3.12-slim` image with the Azure CLI and all Python dependencies pre-installed.

Once inside the container, authenticate with:

```
az login
```

Then run the script as normal.

## Setup

### 1. Copy the example config

```
cp pim_roles.example.yaml pim_roles.yaml
```

`pim_roles.yaml` is git-ignored so your IDs are never committed.

### 2. Fill in the `settings` block

Open `pim_roles.yaml` and replace the placeholder UUIDs with your own values.

#### Finding your IDs

| Field | Azure CLI command |
|---|---|
| `tenant_id` | `az account show --query tenantId -o tsv` |
| `subscription_id` | `az account show --query id -o tsv` |
| `principal_id` | `az ad signed-in-user show --query id -o tsv` |

> **Note:** if you manage multiple subscriptions, run `az account list` and set the right one with `az account set --subscription <name or id>` before running the commands above.

#### Settings reference

```yaml
settings:
  tenant_id: "..."           # AAD Directory (tenant) ID
  subscription_id: "..."     # Azure Subscription ID
  principal_id: "..."        # Your user Object ID in AAD

  default_duration: "PT8H"   # ISO 8601 duration — check your PIM policy max
  default_justification: "Daily work activation"
  delay_between_requests_seconds: 2  # rate-limit buffer between API calls
```

### 3. Define your roles

Each entry in the `roles` list activates one eligible PIM role on one resource group.

```yaml
roles:
  - resource: "my-resource-group-dev"   # exact resource group name in Azure
    role: "Contributor"                  # exact built-in or custom role name
    resource_type: "ResourceGroup"       # currently only ResourceGroup is supported
    enabled: true                        # set false to skip without deleting the entry

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

## Running

```
python activate_pim_roles.py
```

The script prints progress as it runs and a final summary:

```
============================================================
  SUMMARY
============================================================
  ✓  Activated : 18
  ↩  Skipped   : 4
  ✗  Failed    : 0
============================================================
```

Skipped means the role was already active or pending — that is not an error.

## Authentication

The script prefers an existing `az login` session (`AzureCliCredential`). If that fails it falls back to `DefaultAzureCredential`, which also supports:

- Managed Identity (CI/CD pipelines, Azure VMs)
- Environment variables: `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Config file not found` | Make sure `pim_roles.yaml` exists next to the script |
| `Role definition '…' not found at scope` | The role name doesn't match an eligible assignment — check spelling and that the assignment exists in PIM |
| `Failed (403)` | The principal doesn't have an eligible assignment for that role/resource |
| `Failed (400)` | Duration exceeds the maximum allowed by your PIM policy — reduce `default_duration` |
| `Already active or pending (409)` | Not an error — the role is already active for the session |
