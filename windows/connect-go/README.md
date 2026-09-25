# Petabyte Connect for Windows

Native Go/Gio setup assistant. This is the `petabyte-connect.exe` source; it is separate
from the Python application in `desktop-app/`.

## User experience

- Resizable dark interface with a setup rail, GPU details, readable status, and persistent actions.
- Real Windows version, NVIDIA driver and WSL 2 checks. No simulated hardware checks in normal mode.
- Requires an initialized **Ubuntu-24.04** WSL 2 distribution before automated installation.
  Use Setup help to prepare WSL; the connector does not hide interactive first-run Ubuntu prompts.
- Opens as a normal user. When needed, offers a clearly labeled restart through the Windows UAC prompt.
- Explicit consent explains Docker, the background agent, Windows sign-in startup, GPU/electricity
  usage and the WSL restart. Cancel is available before installation begins.
- Browser sign-in has a five-minute timeout, reopen action, nonce validation and single-use callback.
- A successful installer exit means **installation finished**. Online state, rentals and earnings
  must be checked in the account dashboard; this app does not monitor them.
- Idle mining is forced off through PowerShell into the Linux service configuration, including
  when reinstalling over a previously enabled setting. Deploy the accompanying installer fixes too.
- Closing Connect does not stop an installed agent. Use the management instructions on `/install`
  to pause or remove it. Do not close the window during installation; an interruption is not a rollback.

## Build and preview

Requires Go 1.26. Linux/WSL cross-compilation uses no cgo or MinGW:

```sh
VERSION=0.2.0 ./build.sh
```

The pinned `goversioninfo` tool embeds the existing icon, DPI manifest, product name and file
version. The same version appears in the UI. `CompanyName` is a brand label, not a verified
publisher identity; review it against the legal signing entity before public release.

The result is **unsigned**. Building a native GUI or signing it does not guarantee antivirus
acceptance or SmartScreen reputation.

Run `petabyte-connect.exe --selftest` (or `--dryrun`) to simulate setup. Both flags now mean
fully offline preview: no real hardware checks, browser sign-in, account linking or installation.
The UI labels the simulation. A separate preview executable can be built after `build.sh`:

```sh
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath \
  -ldflags='-H=windowsgui -s -w -X main.previewMode=true' -o petabyte-connect-preview.exe .
```

## Validation

```sh
GOOS=windows GOARCH=amd64 go vet .
GOOS=windows GOARCH=amd64 go test -c -o connect-tests.exe
python3 installer_policy_test.py
bash -n build.sh publish.sh ../../install.sh ../../../lumaris_api/installers/install.sh
```

Run `connect-tests.exe -test.v` on Windows. To capture actual Gio screens, set
`PETABYTE_RENDER_DIR` to an output directory. The opt-in render test covers welcome, consent,
sign-in, installation, completion, error, minimum window size and 200% DPI. It needs a GPU
backend, and never installs anything.

Tests cover hostile/replayed callback requests, token validation, localized WSL output, consent,
cancelled sign-in, and mining opt-out over fresh/existing service configuration. Native preview
interaction should also be checked with mouse and keyboard. Gio's custom-drawn content has
limited Windows screen-reader exposure; do not claim full UI Automation accessibility.

Before public release, validate actual enrollment, UAC, installation, service startup, interruption,
reinstallation, pause and removal in a disposable Windows NVIDIA/WSL machine. The local preview
tests do not establish that the production installer or account flow works end to end.

## Signing and publishing

Two separate signatures are required, in this order:

1. Build the final EXE, then Authenticode-sign and timestamp it with the issued certificate
   using SSL.com eSigner/CodeSignTool or a configured Windows signing tool.
2. Run `./publish.sh --key /secure/release_ed25519.pem --version 0.2.0` to create the Ed25519
   release manifest over those final signed bytes.

`publish.sh` stages the EXE, requires `osslsigncode`, and refuses failed Authenticode verification.
It verifies Authenticode validity, not EV certificate classification or SmartScreen reputation.
There is no unsigned-publish bypass. Keep the offline release key out of CI. Certificate credentials
and eSigner configuration are intentionally not fabricated or bundled in the application.

With `PETABYTE_ADMIN_JWT` set, publishing uploads the verified staged EXE and signed manifest to
`/admin/desktop/release`. Otherwise it prints instructions for `/admin/desktop`. The existing
`/download/windows` and `/install` download button continue to use that release system.

The GUI is native Go; the agent installer still uses the existing HTTPS-delivered PowerShell/WSL
bootstrap. The enrollment token is validated and passed through the child environment, never
interpolated into PowerShell command text or emitted in the callback response. Child processes
have timeouts and do not open console windows. The connector does not change Defender settings.

Reference: [Microsoft SmartScreen reputation guidance](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation).
