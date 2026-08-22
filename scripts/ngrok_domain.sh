#!/usr/bin/env bash
#
# Print the reserved ngrok domain to pin the tunnel to, or nothing if there isn't one.
#
# Pinning matters: without --domain, ngrok mints a fresh random hostname on every start,
# and every OAuth redirect URI registered with Google and Meta becomes wrong. With a
# reserved domain the URL survives restarts and nothing needs re-registering.
#
# Order: $NGROK_DOMAIN, else a reserved domain already referenced by a redirect URI in
# .env (that URI is registered with the providers, so it is the one to reuse).
set -euo pipefail

if [ -n "${NGROK_DOMAIN:-}" ]; then
  echo "$NGROK_DOMAIN"
  exit 0
fi

[ -f .env ] || exit 0
grep -hoE 'https://[A-Za-z0-9.-]+\.ngrok(-free)?\.(dev|app|io)' .env 2>/dev/null \
  | head -1 | sed 's|https://||'
