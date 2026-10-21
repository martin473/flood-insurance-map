#!/usr/bin/env bash
# Deploys the map and API on an Ubuntu droplet under ~/portfolio-projects, without Docker:
# a Python virtual environment plus a systemd service that serves the map at / and the API at /api.
#
# From your Mac:
#   curl -fsSL https://raw.githubusercontent.com/martin473/flood-insurance-map/main/deploy/droplet.sh | ssh root@<droplet-ip> 'bash -s'
#
# Run it again any time to update to the latest commit on main.
# Optional: PORT (default 8080), REF (a commit or branch to deploy, default main).
set -euo pipefail

REPO="https://github.com/martin473/flood-insurance-map.git"
BASE="$HOME/portfolio-projects"
APP="$BASE/flood-insurance-map"
SERVICE="flood-insurance-map"
PORT="${PORT:-8080}"
REF="${REF:-main}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root (ssh root@...), since it installs a systemd service." >&2
  exit 1
fi

echo "==> Installing system packages (git, curl)"
export DEBIAN_FRONTEND=noninteractive
command -v git >/dev/null && command -v curl >/dev/null || { apt-get update -qq && apt-get install -y -qq git curl ca-certificates >/dev/null; }

echo "==> Getting the code into $APP"
mkdir -p "$BASE"
if [ ! -d "$APP/.git" ]; then
  git clone --quiet "$REPO" "$APP"
fi
git -C "$APP" fetch --quiet origin "$REF"
git -C "$APP" reset --quiet --hard FETCH_HEAD
echo "    at commit $(git -C "$APP" log --oneline -1)"

echo "==> Setting up Python"
# uv installs its own Python 3.12, so this works whatever version the droplet's Ubuntu ships
if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
fi
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
cd "$APP"
"$UV" venv --quiet --allow-existing --python 3.12 .venv
"$UV" pip install --quiet --python .venv/bin/python .

if [ ! -f deploy/.env ]; then
  echo "==> Creating deploy/.env (kept on the server, never committed)"
  umask 077
  cat > deploy/.env <<EOF
# Required to use POST /api/extract. Send it as: Authorization: Bearer <token>
FLOODRULES_ADMIN_TOKEN=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
FLOODRULES_REQUIRE_ADMIN=1
FLOODRULES_RATE_LIMIT=30
EOF
  umask 022
fi

echo "==> Installing the $SERVICE service on port $PORT"
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Rhode Island Flood Outlook (map and API)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$APP
EnvironmentFile=$APP/deploy/.env
Environment=FLOODRULES_RULES_DIR=$APP/data/rules
Environment=FLOODRULES_MAP_DIR=$APP/map
ExecStart=$APP/.venv/bin/uvicorn floodrules.site:app --host 0.0.0.0 --port $PORT --no-access-log
Restart=always
RestartSec=3
# Hardening: the app can read the code but can't write outside its own folder or gain privileges
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$APP/data/rules
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --quiet "$SERVICE"
systemctl restart "$SERVICE"

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  echo "==> Opening port $PORT in the firewall"
  ufw allow "$PORT/tcp" >/dev/null
fi

echo "==> Waiting for it to answer"
for i in $(seq 1 30); do
  if curl -fsS "localhost:$PORT/api/health" >/dev/null 2>&1; then break; fi
  sleep 2
done
if ! curl -fsS "localhost:$PORT/api/health"; then
  echo "The service didn't come up. Recent logs:" >&2
  journalctl -u "$SERVICE" -n 40 --no-pager >&2
  exit 1
fi
echo

ip="$(curl -fsS -4 --max-time 5 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
echo
echo "Done. Open http://$ip:$PORT in a browser."
echo "Logs:    journalctl -u $SERVICE -f"
echo "Status:  systemctl status $SERVICE"
echo "Metrics: curl localhost:$PORT/api/metrics"
