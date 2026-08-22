#!/usr/bin/env bash
#
# Run the whole app behind ONE https tunnel, with every OAuth setting repointed at it.
#
# Instagram Login refuses plaintext redirect URIs, so it cannot be exercised on
# http://localhost. The tunnel therefore fronts the **frontend** (port 3000), and Next
# rewrites /api/* to the backend on 8000 -- see next.config.ts.
#
# Why not tunnel the API directly and leave the frontend on localhost? Because that makes
# the browser talk cross-site, and Chrome now blocks third-party cookies outright: the
# session cookie is set on the API host and then silently stripped from every subsequent
# request, so the app bounces back to sign-in forever. SameSite=None does not help -- the
# cross-site hop itself is what browsers are removing. One origin for everything is the
# only setup that keeps the cookie first-party.
#
# Nothing here edits .env. Pydantic-settings ranks real environment variables above the
# .env file, so these overrides live only for this process -- plain `make dev` is
# unaffected the moment you stop this.
#
# Usage:  make dev-tunnel                                    ngrok, domain from .env
#         NGROK_DOMAIN=x.ngrok-free.app make dev-tunnel      ngrok, explicit domain
#         TUNNEL=cloudflared make dev-tunnel                 cloudflared quick tunnel
#                                                            (random URL -- re-register
#                                                             OAuth URIs every restart)
#         TUNNEL=cloudflared CLOUDFLARE_TUNNEL=recall-dev \
#           CLOUDFLARE_HOSTNAME=dev.example.com make dev-tunnel     stable, no interstitial
#
# One-time setup for the stable cloudflared option (needs a domain on Cloudflare):
#   cloudflared tunnel login
#   cloudflared tunnel create recall-dev
#   cloudflared tunnel route dns recall-dev dev.example.com
set -euo pipefail

PORT="${PORT:-3000}"   # the tunnel fronts Next; Next proxies /api to the backend
API_PORT="${API_PORT:-8000}"
TUNNEL="${TUNNEL:-ngrok}"
API_PREFIX="/api/v1"
LOG="$(mktemp -t recall-tunnel.XXXXXX)"
TUNNEL_PID=""

cleanup() {
  if [ -n "$TUNNEL_PID" ]; then
    # Children first: cloudflared and ngrok are single processes, but a wrapper script
    # on PATH would otherwise leave the real tunnel orphaned.
    pkill -P "$TUNNEL_PID" 2>/dev/null || true
    kill "$TUNNEL_PID" 2>/dev/null || true
  fi
  rm -f "$LOG"
}
trap cleanup EXIT INT TERM

start_ngrok() {
  command -v ngrok >/dev/null 2>&1 || { echo "error: ngrok not installed" >&2; exit 1; }
  if [ -z "${NGROK_DOMAIN:-}" ]; then
    NGROK_DOMAIN="$(./scripts/ngrok_domain.sh)"
    [ -n "$NGROK_DOMAIN" ] && echo "domain    $NGROK_DOMAIN (found in .env)"
  fi
  if [ -z "${NGROK_DOMAIN:-}" ]; then
    echo "warning   NGROK_DOMAIN unset and none found in .env -- this URL dies on"
    echo "          restart, and every OAuth console needs the new one."
  fi
  # shellcheck disable=SC2086
  ngrok http "$PORT" ${NGROK_DOMAIN:+--domain=$NGROK_DOMAIN} --log=stdout >"$LOG" 2>&1 &
  TUNNEL_PID=$!
  for _ in $(seq 1 40); do
    URL="$(curl -sf http://127.0.0.1:4040/api/tunnels 2>/dev/null \
      | python3 -c 'import sys,json; print(next((t["public_url"] for t in json.load(sys.stdin).get("tunnels",[]) if t["public_url"].startswith("https")), ""))' 2>/dev/null || true)"
    [ -n "${URL:-}" ] && return 0
    sleep 0.4
  done
  return 1
}

start_cloudflared() {
  command -v cloudflared >/dev/null 2>&1 || {
    echo "error: cloudflared not installed (brew install cloudflared)" >&2; exit 1; }

  # Named tunnel: a hostname you own, stable across restarts, so the OAuth redirect URIs
  # registered with Google and Meta stay valid. Requires `cloudflared tunnel login` and a
  # domain on Cloudflare -- see the header of this file.
  if [ -n "${CLOUDFLARE_HOSTNAME:-}" ]; then
    : "${CLOUDFLARE_TUNNEL:?set CLOUDFLARE_TUNNEL to the tunnel name when using CLOUDFLARE_HOSTNAME}"
    cloudflared tunnel run --url "http://localhost:$PORT" "$CLOUDFLARE_TUNNEL" \
      >"$LOG" 2>&1 &
    TUNNEL_PID=$!
    URL="https://$CLOUDFLARE_HOSTNAME"
    # The hostname is known up front, but the tunnel still needs to answer before the
    # OAuth providers start redirecting into it.
    for _ in $(seq 1 40); do
      curl -sf -o /dev/null --max-time 3 "$URL" 2>/dev/null && return 0
      # A tunnel that is up but fronting a not-yet-started Next still counts as ready.
      grep -q "Registered tunnel connection" "$LOG" 2>/dev/null && return 0
      sleep 0.5
    done
    return 1
  fi

  # Quick tunnel: no account needed, but the hostname is random and dies with the process.
  echo "warning   cloudflared quick tunnel -- the URL below is NEW every run, so all four"
  echo "          OAuth redirect URIs must be re-registered in Google and Meta each time."
  echo "          Set CLOUDFLARE_TUNNEL + CLOUDFLARE_HOSTNAME for a stable hostname."
  cloudflared tunnel --url "http://localhost:$PORT" >"$LOG" 2>&1 &
  TUNNEL_PID=$!
  for _ in $(seq 1 40); do
    URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" 2>/dev/null | head -1 || true)"
    [ -n "${URL:-}" ] && return 0
    sleep 0.5
  done
  return 1
}

case "$TUNNEL" in
  ngrok)       start_ngrok       || { echo "error: tunnel did not come up"; cat "$LOG" >&2; exit 1; } ;;
  cloudflared) start_cloudflared || { echo "error: tunnel did not come up"; cat "$LOG" >&2; exit 1; } ;;
  *) echo "error: TUNNEL must be 'ngrok' or 'cloudflared' (got '$TUNNEL')" >&2; exit 1 ;;
esac

cb() { printf '%s%s/auth/%s/callback' "$URL" "$API_PREFIX" "$1"; }

# Overrides. These outrank .env for this process only.
export FRONTEND_URL="$URL"
export BACKEND_URL="$URL"
export CORS_ORIGINS="[\"$URL\"]"
export GOOGLE_REDIRECT_URI="$(cb google)"
export FACEBOOK_REDIRECT_URI="$(cb facebook)"
export INSTAGRAM_LOGIN_REDIRECT_URI="$(cb instagram)"
export INSTAGRAM_CONNECT_REDIRECT_URI="${URL}${API_PREFIX}/integrations/instagram/callback"
# Everything now shares one origin, so the cookie is first-party: Lax is correct and
# strictly safer than None. Secure is required because the origin is https.
export COOKIE_SECURE=true
export SESSION_COOKIE_SAMESITE=lax

cat <<EOF

  tunnel      $URL  (via $TUNNEL)

  Register these redirect URIs, then restart the frontend:

    Google      $GOOGLE_REDIRECT_URI
    Facebook    $FACEBOOK_REDIRECT_URI
    Instagram   $INSTAGRAM_LOGIN_REDIRECT_URI
    IG connect  $INSTAGRAM_CONNECT_REDIRECT_URI

    cd ../realll-ai-frontend && NEXT_PUBLIC_API_URL= pnpm dev

  NEXT_PUBLIC_API_URL must be EMPTY so the browser calls this app's own origin and Next
  proxies /api to the backend. An absolute URL there puts the cookie back in third-party
  territory, where Chrome drops it. NEXT_PUBLIC_* is inlined at startup, so the frontend
  must be restarted -- editing .env.local while it runs will not take.

  Open $URL (not localhost:3000).
EOF

if [ "$TUNNEL" = "ngrok" ] && [ -z "${NGROK_DOMAIN:-}" ]; then
  cat <<'EOF'
  note        ngrok's free interstitial page can intercept the OAuth redirect, which
              shows up as a hung or failed callback. If that happens, use a reserved
              domain or run: TUNNEL=cloudflared make dev-tunnel

EOF
fi

uv run uvicorn app.main:app --reload --port "$API_PORT"
