# Backend Deployment Roadmap (no cold starts, free / cheap)

Render's free tier **spins the service down after ~15 min idle**, so the first
request after idle waits 30–60s while it boots. Everything below avoids that by
keeping a machine **always running**.

Your backend is now container-ready: `Dockerfile`, `.dockerignore`, and `fly.toml`
are in this folder. Fonts (`app/assets/fonts/WorkSans-*.ttf`) ship inside the image.

---

## The short answer

| Option | Cost | Cold start? | Effort | Notes |
|---|---|---|---|---|
| **Fly.io** | Free allowance* | **No** (pin 1 machine) | Low | Best balance. Deploy in ~5 mins with the included `fly.toml`. |
| **Oracle Cloud (Always Free)** | **Free forever** | **No** | Medium | A real ARM VM (up to 4 CPU / 24 GB). Most generous, never expires. |
| **AWS Elastic Beanstalk** | Free 12 months** | **No** | Medium-High | `t3.micro` EC2, always on. AWS console is heavier. |
| **DigitalOcean App Platform** | ~$5/mo (not free) | **No** | Very low | Dead-simple Git deploy. Cheapest paid, zero ops. |
| Google Cloud Run | Free tier | **Yes** unless min-instances≥1 (costs) | Low | Scales to zero → cold start. Avoid for this use. |
| Railway / Koyeb | Small free credit | No while running | Low | Fine, but free credit is limited. |

\* Fly gives a monthly usage allowance; a single small always-on machine typically
stays within/near it. A card is required for verification.
\** AWS free tier covers 750 hrs/month of `t3.micro` for the first 12 months.

**Recommendation:** start with **Fly.io** (fastest to ship, no cold start). If you
want *permanently* free with more headroom, use an **Oracle Cloud Always-Free VM**.

---

## Option A — Fly.io (recommended, ~5 minutes)

1. Install the CLI and sign in:
   ```
   # Windows (PowerShell)
   iwr https://fly.io/install.ps1 -useb | iex
   fly auth signup      # or: fly auth login
   ```
2. From the `backend/` folder (it already has `Dockerfile` + `fly.toml`):
   ```
   fly launch --no-deploy      # accept the app name or edit fly.toml's `app`
   ```
3. Set your secrets (these are your existing env vars — never commit them):
   ```
   fly secrets set SUPABASE_URL=... SUPABASE_SERVICE_KEY=... SUPABASE_ANON_KEY=... \
                   GEMINI_API_KEY=... GEMINI_MODEL=gemini-2.5-flash \
                   CORS_ORIGINS=https://your-frontend-domain
   ```
4. Deploy:
   ```
   fly deploy
   ```
5. Your URL is `https://<app>.fly.dev`. Put that in the frontend's
   `NEXT_PUBLIC_API_BASE`, and add it to `CORS_ORIGINS`.

**No cold start:** `fly.toml` already sets `auto_stop_machines = "off"` and
`min_machines_running = 1`, so a machine is always live.
Health check: `https://<app>.fly.dev/health`.

---

## Option B — Oracle Cloud Always-Free VM (free forever)

Best if you want a permanent free box with real resources.

1. Create a free account → **Compute → Instances → Create**. Pick an **Ampere
   A1 (ARM)** shape (Always Free eligible), Ubuntu 22.04, and add your SSH key.
2. Open port 8000 (or 443): add an **ingress rule** to the subnet's security list,
   or run behind nginx/Caddy on 443.
3. SSH in and run the container (Docker is simplest):
   ```
   sudo apt update && sudo apt install -y docker.io
   git clone <your-repo> && cd .../backend
   sudo docker build -t firmity-backend .
   sudo docker run -d --restart unless-stopped -p 8000:8000 --env-file .env firmity-backend
   ```
   `--restart unless-stopped` keeps it running across reboots (no cold start).
4. (Recommended) Put Caddy in front for automatic HTTPS, or use a Cloudflare
   Tunnel so you don't expose the VM directly.

---

## Option C — AWS Elastic Beanstalk (free 12 months)

1. Install the EB CLI: `pip install awsebcli`.
2. From `backend/`:
   ```
   eb init -p docker firmity-backend        # choose a region
   eb create firmity-backend-env --single   # --single = 1 instance, no load balancer (cheaper)
   ```
3. Set env vars in the EB console (**Configuration → Software → Environment
   properties**) or `eb setenv KEY=value ...`.
4. `eb deploy`. EB serves on an always-on `t3.micro` (free tier) — no cold start.
   Health check path: `/health`.

---

## Option D — DigitalOcean App Platform (simplest paid, ~$5/mo)

1. Push the repo to GitHub.
2. DO → **Apps → Create** → pick the repo → it detects the `Dockerfile`.
3. Set **HTTP port = 8000**, add env vars, choose the **Basic ($5/mo)** instance.
4. Deploy. Always-on, auto-HTTPS, zero server management. No cold start.

---

## After deploying (any option)

- Point the frontend: set `NEXT_PUBLIC_API_BASE` to the backend URL and redeploy the site.
- Add the frontend origin to `CORS_ORIGINS` on the backend, then restart/redeploy.
- Run the DB migrations in Supabase (see `DEPLOYMENT.md`) before first real use.
- Smoke test: open `<backend>/health` → `{"status":"ok"}`, then generate one report.

**Keeping it warm as a backstop:** even with an always-on machine, you can add a
free uptime pinger (e.g. UptimeRobot) hitting `/health` every 5 minutes — this also
gives you free downtime alerts.
