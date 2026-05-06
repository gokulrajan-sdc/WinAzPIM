# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Desktop UI (`pim_ui.py`)** — Tk-based app that lists every PIM eligibility
  in a checkable table, accepts a single justification message, and submits
  one activation request per ticked row.
  - Auto-refreshes the eligibility / active-assignment state every hour.
  - Re-prompts the user to run `az login` if the underlying credential
    expires (HTTP 401 or `ClientAuthenticationError`).
  - Renders the `Until` column as a localised wall-clock time plus a
    relative remaining-time hint (e.g. `2026-05-07 04:00 (in 26h 0m)`).
  - Sorts active rows above eligible rows; tags active rows green and
    in-flight rows orange.
- **CLI: `--list` mode** — prints every PIM eligibility for the signed-in
  user, including ones inherited through AAD groups, alongside an
  activation status column and the activation/eligibility expiry.
- **CLI: `--activate` mode** — submits a self-activation request for a
  specific role + resource with a custom justification and polls
  `roleAssignmentScheduleRequests` until a terminal status
  (`Provisioned`, `Granted`, `AdminApproved`, `Failed`, `Denied`,
  `Canceled`, `AdminDenied`, `TimedOut`, `Revoked`).
  - Companion flags: `--duration` (defaults to `PT8H`), `--no-wait`,
    `--timeout` (defaults to 120s, increase for approval-gated roles),
    `-v`/`--verbose`.
- **Group-inherited eligibility resolution** — the script now calls
  Microsoft Graph (`/me`, `/me/transitiveMemberOf`) to enumerate the
  signed-in user's transitive group membership, then queries
  `roleEligibilityScheduleInstances` and `roleAssignmentScheduleInstances`
  per principal at the tenant root scope. This matches the Azure portal's
  *My roles* view, which the previous `asTarget()`-only path missed.
- **Status correlation** — active assignments are correlated back to their
  source eligibility via `linkedRoleEligibilityScheduleId`, so the UI/CLI
  can show whether each eligible role is currently active and when the
  current activation expires.
- **`CHANGELOG.md`** — this file.
- **Packaging** — `pim_activator.spec` (PyInstaller) plus `build.py`
  produce a standalone, installable desktop app per OS:
  - Windows: `AzurePIMActivator-windows.zip`
  - macOS: `AzurePIMActivator-macos.dmg`
  - Linux: `AzurePIMActivator-linux.tar.gz`
- **`requirements-dev.txt`** pinning PyInstaller for build environments.
- **GitHub Actions release workflow** (`.github/workflows/release.yml`) — builds
  all three platform packages in a parallel matrix on tag push (`v*.*.*`) or
  manual dispatch, then creates a GitHub Release with the three artifacts
  attached. Pre-release tags (`-alpha`, `-beta`, `-rc`) are automatically
  marked as pre-releases.

### Changed

- **README** rewritten around the three workflows (Desktop UI / CLI list +
  activate / batch YAML activation) with a platform-support matrix, build
  instructions, and troubleshooting table.
- **Desktop UI auth flow** reworked. Authentication now runs on a worker
  thread so the UI never freezes while signing in. The flow tries an
  existing `az login` session silently first; on failure it falls back to
  an `InteractiveBrowserCredential` (system-browser MSAL flow) which works
  even when the Azure CLI isn't installed — required for the packaged
  distributable to run on machines without `az`. The previous subprocess
  call to `az login` from inside the UI was removed.
- **Auth bootstrap** — `--list` and `--activate` no longer require
  `pim_roles.yaml`; tenant and subscription are read from the active
  `az account show` session. Batch activation still uses the YAML file.
- **`activate_role()`** now returns `(response, request_url)` so callers
  can poll the request URL after submission. The existing batch-activation
  call site was updated accordingly.

### Notes

- No new runtime dependencies. The desktop UI relies on Tk (bundled with
  Python on Windows and macOS; on Debian/Ubuntu install
  `python3-tk`, on Fedora/RHEL `python3-tkinter`, on Arch `tk`).
- The Microsoft Graph calls require the delegated permissions `User.Read`
  and `GroupMember.Read.All` on the `az` token — both are granted by
  default for typical Azure CLI logins.
