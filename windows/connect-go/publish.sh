#!/usr/bin/env bash
# publish.sh - ship a new petabyte-connect.exe to /download/windows, which lights up the
# "Get the Windows app" button on /install. Reuses the EXISTING signed desktop-release system
# (POST /admin/desktop/release -> served by web_routes.download_windows, gated by
# _desktop_app_available). No new hosting code: this just publishes THROUGH it.
#
# The update workflow, once you have the EV cert:
#   1. ./build.sh                                   # produce petabyte-connect.exe
#   2. EV-sign it  (SSL.com eSigner / signtool)     # do this on Windows or in CI, with the cert
#   3. ./publish.sh --key <release_ed25519.pem> --version <X.Y.Z>
#
# TWO different signatures, BOTH required:
#   - Authenticode     -> authenticates the publisher and file integrity. [step 2, EV cert]
#   - Ed25519 manifest -> the SERVER agrees to serve the build.            [step 3, offline key]
# The offline release key never goes in CI; run step 3 on a trusted machine.
set -euo pipefail
cd "$(dirname "$0")"

EXE=petabyte-connect.exe
KEY=""; VERSION=""; API="${PETABYTE_API_URL:-https://petabyte.market}"
while [ $# -gt 0 ]; do case "$1" in
  --key)     KEY="$2";     shift 2;;
  --version) VERSION="$2"; shift 2;;
  --exe)     EXE="$2";     shift 2;;
  --api)     API="$2";     shift 2;;
  *) echo "unknown arg: $1"; exit 2;;
esac; done

[ -f "$EXE" ] || { echo "no $EXE - run ./build.sh first"; exit 1; }
[ -n "$KEY" ] && [ -n "$VERSION" ] || { echo "usage: ./publish.sh --key <release_ed25519.pem> --version <X.Y.Z>"; exit 2; }

# 1. Verify the exact staged bytes that will be signed and uploaded. Never publish
# an unsigned build. This verifies Authenticode, not whether the cert is EV.
REPO=$(cd ../../.. && pwd)
mkdir -p dist
STAGED="dist/petabyte-connect.exe"
if [ "$(realpath "$EXE")" != "$(realpath -m "$STAGED")" ]; then cp -f "$EXE" "$STAGED"; fi
command -v osslsigncode >/dev/null 2>&1 || { echo 'Install osslsigncode to verify Authenticode before publishing.'; exit 1; }
osslsigncode verify -in "$STAGED" || { echo 'Publishing blocked: Authenticode verification failed. Sign and timestamp the EXE first.'; exit 1; }
echo 'Authenticode verified. Signing the release manifest next.'
python3 "$REPO/scripts/sign_release.py" --key "$KEY" --version "$VERSION" \
  --exe "$STAGED" --out-dir dist
MAN="dist/PetabyteAgent.exe.manifest.json"
[ -f "$MAN" ] || { echo "manifest not produced by sign_release.py"; exit 1; }
echo "manifest: $MAN"

# 2. Publish. Browser form is the no-terminal path; curl works with an admin session JWT.
if [ -n "${PETABYTE_ADMIN_JWT:-}" ]; then
  echo "uploading to $API/admin/desktop/release ..."
  curl -fsS -H "Authorization: Bearer $PETABYTE_ADMIN_JWT" \
    -F "exe=@$STAGED;type=application/vnd.microsoft.portable-executable" \
    -F "manifest=@$MAN;type=application/json" \
    "$API/admin/desktop/release" && echo "  published -> $API/download/windows"
else
  echo
  echo "No PETABYTE_ADMIN_JWT set - finish in the browser (no terminal needed):"
  echo "  1. open  $API/admin/desktop   (signed in as an admin)"
  echo "  2. upload:  $STAGED   and   $MAN"
  echo "The /install 'Get the Windows app' button lights up automatically once it's served."
fi
