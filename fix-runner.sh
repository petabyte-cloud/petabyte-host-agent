#!/usr/bin/env bash
# fix-runner.sh — run a Petabyte support fix on THIS node, as root. Started by
# petabyte-agent-fix.path when the agent drops a delivered fix into the inbox (fixes.py).
#
# SECURITY. This is the boundary between "the API server says run this" and root on the seller's
# machine, so a fix runs ONLY if every check passes; otherwise it is rejected and the reason is
# reported. Mirrors update.sh: the Ed25519 key that signs fixes lives in GitHub, never on the API
# server, so a compromised server can queue a fix but cannot make this script run it.
#   * the owner opted in: PB_ALLOW_REMOTE_FIXES=true in agent.env (`main.py fixes enable`)
#   * the signature over the exact payload verifies against the PINNED release key
#   * the payload is printable ASCII (+tab/newline) with EXACTLY this 7-line header, so no parser
#     (this one, sign-node-fix.yml, scripts/node_fix.py, what the owner saw) can read it differently:
#       pb-node-fix-v1 / node: <this node> / fix: <id> / nonce: <32 hex> / expires: <unix, <=25h> /
#       sha256: <of the script> / ---
#   * it never ran here before (ids are recorded BEFORE running, so a crash can't run one twice)
# It verifies, parses and runs a PRIVATE COPY, so nothing can swap the file between check and run.
set -uo pipefail
STATE="${PETABYTE_FIX_STATE:-/var/lib/petabyte-agent}"
IN="$STATE/fix-inbox"; OUT="$STATE/fix-outbox"; DONE="$STATE/fixes-done"
PUB="${PETABYTE_RELEASE_PUBKEY:-/etc/petabyte/release_ed25519.pub}"
ENVF="${PETABYTE_AGENT_ENV:-/etc/petabyte/agent.env}"
PREFIX=pb-node-fix-v1
umask 077
mkdir -p "$IN" "$OUT"; touch "$DONE"

envval() { grep -E "^$1=" "$ENVF" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"'"; }
finish() {  # finish <id> <rc-or-reason>: publish the result for the agent, clear the inbox
  printf '%s\n' "$2" > "$OUT/.$1.rc" && mv "$OUT/.$1.rc" "$OUT/$1.rc"
  rm -f "$IN/$1.payload" "$IN/$1.sig"
}
reject() { echo "rejected: $2" > "$OUT/$1.out"; finish "$1" "rejected: $2"; }

process() {  # process <id> <private dir holding copies: payload, sig>
  local id="$1" f="$2/payload" s="$2/sig" exp now want
  [ "$(envval PB_ALLOW_REMOTE_FIXES)" = "true" ] || { reject "$id" "support fixes are not enabled on this machine"; return; }
  [ -s "$s" ] || { reject "$id" "no signature"; return; }
  [ -f "$PUB" ] || { reject "$id" "no pinned release key at $PUB"; return; }
  openssl pkeyutl -verify -pubin -inkey "$PUB" -rawin -in "$f" -sigfile "$s" >/dev/null 2>&1 \
    || { reject "$id" "signature did not verify ($(openssl version 2>&1 | head -1))"; return; }
  [ "$(LC_ALL=C tr -d '\11\12\40-\176' < "$f" | wc -c)" -eq 0 ] || { reject "$id" "payload has non-printable bytes"; return; }
  [ "$(sed -n 1p "$f")" = "$PREFIX" ] || { reject "$id" "not a node-fix payload"; return; }
  [[ "$(sed -n 2p "$f")" =~ ^node:\ ([0-9]+)$ ]] && [ -n "$SPEC" ] && [ "${BASH_REMATCH[1]}" = "$SPEC" ] \
    || { reject "$id" "fix is for another machine"; return; }
  [ "$(sed -n 3p "$f")" = "fix: $id" ] || { reject "$id" "fix id mismatch"; return; }
  [[ "$(sed -n 4p "$f")" =~ ^nonce:\ [0-9a-f]{32}$ ]] || { reject "$id" "bad header (nonce)"; return; }
  [[ "$(sed -n 5p "$f")" =~ ^expires:\ ([0-9]{1,11})$ ]] || { reject "$id" "bad header (expires)"; return; }
  exp="${BASH_REMATCH[1]}"; now="$(date +%s)"
  [ "$exp" -gt "$now" ] || { reject "$id" "expired"; return; }
  # fixes live <= 24h: a long-lived signature could be replayed after a reinstall wipes fixes-done
  [ "$exp" -le $(( now + 90000 )) ] || { reject "$id" "expiry too far in the future"; return; }
  [[ "$(sed -n 6p "$f")" =~ ^sha256:\ ([0-9a-f]{64})$ ]] || { reject "$id" "bad header (sha256)"; return; }
  want="${BASH_REMATCH[1]}"
  [ "$(sed -n 7p "$f")" = "---" ] || { reject "$id" "bad header (separator)"; return; }
  tail -n +8 "$f" > "$2/script"
  [ "$(sha256sum < "$2/script" | cut -d' ' -f1)" = "$want" ] || { reject "$id" "script hash mismatch"; return; }
  echo "$id" >> "$DONE"
  ( cd / && timeout 600 bash "$2/script" ) > "$OUT/$id.out" 2>&1
  finish "$id" "$?"
}

SPEC="$(envval PETABYTE_SPEC_ID)"
for p in "$IN"/*.payload; do
  [ -e "$p" ] || continue
  id="$(basename "$p" .payload)"
  sig="$IN/$id.sig"
  if ! [[ "$id" =~ ^[0-9]{1,12}$ ]] || [ -L "$p" ] || [ -L "$sig" ] || [ ! -f "$p" ]; then
    rm -f "$p" "$sig"; continue
  fi
  if grep -qx "$id" "$DONE"; then rm -f "$p" "$sig"; continue; fi    # never twice; keep its result
  W="$(mktemp -d)"
  cp -- "$p" "$W/payload"; cp -- "$sig" "$W/sig" 2>/dev/null
  process "$id" "$W"
  rm -rf "$W"; rm -f "$p" "$sig"
done
