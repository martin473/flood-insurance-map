#!/usr/bin/env bash
# Deploys the map and API on a fresh or existing Ubuntu droplet, under ~/portfolio-projects.
# Run it from your Mac (it prompts for your SSH key's passphrase as usual):
#
#   ssh root@<droplet-ip> 'bash -s' < deploy/droplet.sh
#
# Run it again any time to update to the latest commit on main.
set -euo pipefail

REPO="https://github.com/martin473/flood-insurance-map.git"
BASE="$HOME/portfolio-projects"
APP="$BASE/flood-insurance-map"

echo "==> Checking Docker"
if ! command -v docker >/dev/null 2>&1; then
  echo "    Docker isn't installed; installing it with Docker's official script"
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null

echo "==> Getting the code into $APP"
mkdir -p "$BASE"
if [ -d "$APP/.git" ]; then
  git -C "$APP" fetch --quiet origin main
  git -C "$APP" reset --quiet --hard origin/main
else
  git clone --quiet "$REPO" "$APP"
fi
cd "$APP"
echo "    at commit $(git log --oneline -1)"

if [ ! -f deploy/.env ]; then
  echo "==> Creating deploy/.env (kept on the server, never committed)"
  token="$(openssl rand -hex 24)"
  umask 077
  cat > deploy/.env <<EOF
# ":80" serves HTTP on the droplet's IP. Change to your domain name for automatic HTTPS, then rerun this script.
SITE_ADDRESS=:80
# Required to use POST /api/extract. Send it as: Authorization: Bearer <token>
FLOODRULES_ADMIN_TOKEN=$token
FLOODRULES_RATE_LIMIT=30
EOF
  echo "    Admin token saved in $APP/deploy/.env"
fi

echo "==> Building and starting"
docker compose -f deploy/docker-compose.yml up -d --build --remove-orphans

echo "==> Waiting for the API"
for i in $(seq 1 30); do
  if curl -fsS localhost/api/health >/dev/null 2>&1; then break; fi
  sleep 2
done
curl -fsS localhost/api/health && echo

ip="$(curl -fsS -4 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
echo
echo "Done. Open http://$ip in a browser."
echo "Logs:    cd $APP && docker compose -f deploy/docker-compose.yml logs -f"
echo "Metrics: curl localhost/api/metrics"
