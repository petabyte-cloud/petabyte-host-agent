#!/usr/bin/env bash
# Build the native Gio GUI from Linux/WSL. The result is UNSIGNED.
# Sign and timestamp the final EXE before running publish.sh.
set -euo pipefail
cd "$(dirname "$0")"

VERSION="${VERSION:-0.2.0}"
[[ "$VERSION" =~ ^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$ ]] || { echo 'VERSION must be X.Y.Z (0-9999 per component)'; exit 2; }
# Pin the resource generator and keep it local to this build directory.
mkdir -p .tools
GOBIN="$PWD/.tools" go install github.com/josephspurrier/goversioninfo/cmd/goversioninfo@v1.5.0
# Remove the old resource generator's output to avoid duplicate Windows resources.
rm -f rsrc_windows.syso
.tools/goversioninfo -64 -o resource_windows_amd64.syso \
  -file-version "$VERSION" -product-version "$VERSION" -propagate-ver-strings versioninfo.json

CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -trimpath \
  -ldflags="-H=windowsgui -s -w -X main.version=$VERSION" -o petabyte-connect.exe .
echo "Built Petabyte Connect $VERSION — UNSIGNED; sign and timestamp before distribution."
