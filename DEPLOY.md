# Deploying

## On your own droplet (simplest, no Docker)

`deploy/droplet.sh` sets everything up under `~/portfolio-projects/flood-insurance-map` on the server:

- clones the repo, or updates it if it's already there
- installs Python 3.12 into a virtual environment, using `uv`
- creates `deploy/.env` with a random admin token
- installs a systemd service that serves the map at `/` and the API at `/api` on port 8080, and restarts it if it crashes
- opens port 8080 if the `ufw` firewall is on

CI runs this same script as root on a fresh Ubuntu machine on every push, and checks the map, the API and a restart.

From any terminal on your Mac:

```bash
curl -fsSL https://raw.githubusercontent.com/martin473/flood-insurance-map/main/deploy/droplet.sh | ssh root@<droplet-ip> 'bash -s'
```

SSH asks for your key's passphrase as usual. When it finishes, open `http://<droplet-ip>:8080`. Run the same command again to update to the latest commit.

- **Logs:** `journalctl -u flood-insurance-map -f` on the droplet
- **Another port:** put `PORT=9000` before `bash -s`, as in `ssh root@<ip> 'PORT=9000 bash -s'`
- **A domain with HTTPS:** point the domain at the droplet and have your existing web server proxy to `localhost:8080`

## On DigitalOcean App Platform

One DigitalOcean App Platform app runs both parts:

- **map**: the static page at `/`, from `map/`
- **api**: the FastAPI service at `/api`, built from the `Dockerfile`

Because they share one address, the map finds the API at `/api` on its own and switches to it. Opened as a local file, the map calls the public data services directly instead.

## First deploy

1. **Install and log in to `doctl`** (DigitalOcean's command line):
   ```bash
   brew install doctl
   doctl auth init          # paste an API token from cloud.digitalocean.com/account/api/tokens
   ```
2. **Let DigitalOcean read the repo:** in the DigitalOcean dashboard, Apps > Create App > GitHub, and authorize access to `martin473/flood-insurance-map`. You only need to get as far as authorizing; then cancel and use the spec below.
3. **Create the app from the spec:**
   ```bash
   cd ~/flood-rules
   doctl apps create --spec .do/app.yaml
   ```
4. **Set the admin token.** `/extract` calls your model quota, so it stays disabled until a token is set:
   App > Settings > api > Environment variables > `FLOODRULES_ADMIN_TOKEN`, value from `openssl rand -hex 24`, and tick **Encrypt**.
5. **Wait for the deploy**, then open the app's URL. Check:
   - `<app-url>/api/health` shows `"ok": true`
   - the map loads, and tapping a spot shows "Checked ... UTC" under the answers (that line only appears when the answer came from the API)

After this, every push to `main` redeploys both parts. CI builds the same Docker image and checks that it starts, so a broken image fails in CI before it reaches DigitalOcean.

## Operating it

| What | Where |
|---|---|
| Is it up? | `GET /api/health`, also used by DigitalOcean's health check |
| Per-source health | `GET /api/metrics`: calls, errors, cache hits and latency for FEMA, URI, USFWS and Nominatim, plus request counts by route and status |
| Logs | App > Runtime Logs. One JSON line per request, with a request id that's also returned in the `x-request-id` header |
| Insurer rules | `data/rules/*.json`, shipped in the image. Extract with `floodrules extract ... --save`, commit, push |

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `FLOODRULES_ADMIN_TOKEN` | unset | Required for `/extract` on the server |
| `FLOODRULES_REQUIRE_ADMIN` | `1` in the image | Keeps `/extract` disabled while no token is set |
| `FLOODRULES_RATE_LIMIT` | `30` in the spec | `/property` requests per minute per client |
| `FLOODRULES_TRUST_PROXY` | `1` in the spec | Read the client address from `x-forwarded-for`. Only set behind a proxy you control |
| `CORS_ORIGINS` | the app's URL | Origins allowed to call the API from a browser |
| `FLOODRULES_RULES_DIR` | `/app/data/rules` | Where verified rules are read from |
| `OLLAMA_API_KEY` | unset | Only needed if you use `/extract` on the server |
