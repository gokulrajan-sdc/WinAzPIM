# AzurePIMAccessAutomator

Tools for working with Azure Privileged Identity Management (PIM) eligible role assignments without clicking through the portal. Two interfaces ship in this repo:

- **Desktop UI** (`pim_ui.py`) — a Tk-based app that lists every PIM-eligible role you have (direct or inherited via AAD groups), shows current activation status, and lets you tick multiple roles and activate them with a single justification.
- **CLI** (`activate_pim_roles.py`) — supports listing (`--list`), single-role activation with polling (`--activate`), and batch activation from a YAML file. Useful for scripting and "start of day" automation.

## Prerequisites

- Python 3.10+
- Azure CLI installed and logged in (`az login`)
- PIM eligible assignments already configured for your account in Azure AD (directly or via group membership)

```
pip install -r requirements.txt
```

## Platform support

Both the CLI and the desktop UI run on Windows, macOS and Linux. The only platform-specific gotcha is Tk on Linux:

| Platform | Notes |
|---|---|
| Windows | Works out of the box. `az` (which is `az.cmd`) is invoked through the shell internally. |
| macOS | Works out of the box if you use Python from python.org or Homebrew (`brew install python-tk` if your Python doesn't already include Tk). |
| Linux  | Tk is not bundled with the system Python on most distros. Install it before running the UI: <br>• Debian / Ubuntu: `sudo apt install python3-tk` <br>• Fedora / RHEL: `sudo dnf install python3-tkinter` <br>• Arch: `sudo pacman -S tk` |

You can confirm Tk is available with `python -c "import tkinter; tkinter.Tk().destroy()"` — that should exit silently. The CLI flows (`--list`, `--activate`, batch) don't need Tk.

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

## Desktop UI

```
python pim_ui.py
```

On launch the app will:

1. **Authenticate.** It first tries to reuse an existing `az login` session silently — no prompt if you already ran `az login`. If that fails (no Azure CLI installed, no active session, expired refresh token), it falls back to a system-browser sign-in via MSAL. This means **the packaged distributable works on machines without the Azure CLI**.
2. Pull your eligibilities + active assignments and display them in a table:

| ✓ | Status | Role | Resource | Via | Until |
|---|---|---|---|---|---|
| ☑ | ACTIVE | Contributor | ft-core-data-services-dev | Group | 2026-05-06 18:00 (in 4h 12m) |
| ☐ | eligible | Contributor | ft-applications-pat-dev | Group | 2026-06-30 14:18 (in 55d 3h) |

3. The leftmost column is a checkbox — click anywhere in a row to toggle.
4. Type a justification at the bottom, pick a duration (PT1H / PT2H / PT4H / PT8H), click **Activate selected**. The same justification is used for every ticked row. Already-active rows are skipped automatically.
5. While each request is in flight its row shows `submitting`. When the worker finishes, the table re-fetches so STATUS / UNTIL reflect the latest state.
6. The app auto-refreshes every hour. Click **Refresh now** to pull on demand.
7. If the Azure session expires (401 from ARM, or `AzureCliCredential` fails), a modal asks you to re-run `az login`.

The status bar at the bottom always shows the most recent message (loading, refresh complete, errors).

## CLI

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

## Building a standalone desktop app

The desktop UI can be packaged into a self-contained executable so end users don't need a Python environment installed. PyInstaller is used; cross-compilation isn't supported, so you build on each target OS.

```
pip install -r requirements-dev.txt
python build.py
```

`build.py` runs PyInstaller against [pim_activator.spec](pim_activator.spec) and then packages the result for the host platform:

| Host | Output (in `artifacts/`) |
|---|---|
| Windows | `AzurePIMActivator-windows.zip` (contains `AzurePIMActivator.exe` + bundled runtime) |
| macOS | `AzurePIMActivator-macos.dmg` (drag-to-Applications, `AzurePIMActivator.app` inside) |
| Linux | `AzurePIMActivator-linux.tar.gz` (extract anywhere, run the `AzurePIMActivator` binary) |

**Linux note:** PyInstaller bundles the Python interpreter but not Tk's shared libs from your distro by default. Make sure `python3-tk` is installed at *build time* — PyInstaller will then pull the necessary `libtk*.so` and `libtcl*.so` into the bundle. Recipients of the tarball don't need Tk installed on their own machines.

**macOS note:** the produced `.app` is unsigned. Recipients will need to right-click → Open the first time (or run `xattr -cr AzurePIMActivator.app`) to bypass Gatekeeper. For a notarised build, sign with `codesign` and submit to Apple's notarisation service after `build.py` completes.

If you want to run PyInstaller manually instead of using `build.py`:

```
pyinstaller --noconfirm pim_activator.spec
```

The output lands in `dist/`; package as you wish.

## Releasing a new version

Push a version tag and the [release workflow](.github/workflows/release.yml) does the rest:

```bash
git tag v1.0.0
git push origin v1.0.0
```

The workflow:

1. **Runs in parallel** on `windows-latest`, `macos-latest` (Apple Silicon), and `ubuntu-latest`.
2. Installs deps (`pip install -r requirements-dev.txt`) and runs `python build.py` on each runner.
3. Uploads the three platform artifacts as workflow artifacts.
4. Once all three succeed, the `release` job creates a **GitHub Release** named `v1.0.0` with auto-generated notes and attaches:
   - `AzurePIMActivator-windows.zip`
   - `AzurePIMActivator-macos.dmg`
   - `AzurePIMActivator-linux.tar.gz`

Pre-release tags (`v1.0.0-alpha.1`, `v1.0.0-beta.2`, `v1.0.0-rc.1`, etc.) are automatically marked as pre-releases on GitHub.

If a build fails and you need to re-run without pushing a new tag, go to **Actions → Release → Run workflow** and supply the existing tag in the input field. The job will upload assets to the existing release (clobbering any previously uploaded files for that release).

The workflow requires no secrets beyond the default `GITHUB_TOKEN` — no external credentials needed.

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
