# Deploy the backend on an Oracle Cloud Always-Free VM

Free **forever** (no 12-month clock). Caveat: Oracle quietly trimmed the Always-Free
ARM allowance to **2 OCPU / 12 GB** (June 2026) and can change it again without notice —
plenty for this backend, but keep Fly.io as a fallback.

**Use:** an **Ampere A1 (ARM)** instance — the app's deps (fpdf2, matplotlib, Pillow)
all have arm64 wheels and the Docker image builds fine on ARM.

---

## 1. Create the instance
1. Sign up at cloud.oracle.com (free). Pick a **home region** near your users (e.g. Mumbai/Hyderabad for India).
2. **Compute → Instances → Create instance.**
3. Image & shape:
   - Image: **Canonical Ubuntu 22.04**.
   - Shape: **Ampere / VM.Standard.A1.Flex**, set **1 OCPU + 6 GB** (safe under the 2/12 cap; bump to 2/12 if you want headroom).
4. **Add your SSH public key** (or let it generate one and download it).
5. Networking: keep "Create new VCN" + "Assign public IPv4". Create.
6. Note the instance's **public IP**.

## 2. Open the port (TWO places — the #1 gotcha)
Oracle blocks traffic at both the cloud firewall AND the OS firewall.
1. **Cloud security list:** VCN → Subnet → Security List → **Add Ingress Rule**:
   Source `0.0.0.0/0`, IP Protocol **TCP**, Destination port **443** (and **80** for TLS setup).
2. **OS firewall** (Ubuntu images ship iptables rules that drop everything): after SSH (step 3),
   ```
   sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80  -j ACCEPT
   sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
   sudo netfilter-persistent save
   ```

## 3. SSH in + install Docker
```
ssh -i /path/to/key ubuntu@<PUBLIC_IP>
sudo apt update && sudo apt install -y docker.io git
sudo systemctl enable --now docker
```

## 4. Get the code + build
```
git clone <your-repo-url> firmity && cd firmity/backend
sudo docker build -t firmity-backend .      # arm64 build, ~few min
```

## 5. Env + run (auto-restarts on reboot)
Create `backend/.env` (never commit it):
```
SUPABASE_URL=...
SUPABASE_SERVICE_KEY=...
SUPABASE_ANON_KEY=...
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash
CORS_ORIGINS=https://your-live-site.com
```
Run:
```
sudo docker run -d --name firmity --restart unless-stopped \
  --env-file .env -p 8000:8000 firmity-backend
curl localhost:8000/health        # -> {"status":"ok"}
```

## 6. HTTPS with a real domain (recommended: Caddy = auto-TLS)
Point a DNS `A` record (e.g. `api.yourdomain.com`) at the instance's public IP, then:
```
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```
Put this in `/etc/caddy/Caddyfile`:
```
api.yourdomain.com {
    reverse_proxy localhost:8000
}
```
```
sudo systemctl restart caddy      # Caddy fetches a Let's Encrypt cert automatically
```
Now `https://api.yourdomain.com/health` works with valid TLS.

> No domain? Use a **Cloudflare Tunnel** instead (free, no open ports needed):
> `cloudflared tunnel --url http://localhost:8000` gives you an https URL.

## 7. Connect the live frontend
- On Vercel: set **`NEXT_PUBLIC_API_BASE=https://api.yourdomain.com`** → redeploy.
- On the VM: ensure **`CORS_ORIGINS`** in `.env` = your live site, then
  `sudo docker restart firmity`.

## 8. Updating later
```
cd firmity && git pull
cd backend && sudo docker build -t firmity-backend . \
  && sudo docker rm -f firmity \
  && sudo docker run -d --name firmity --restart unless-stopped --env-file .env -p 8000:8000 firmity-backend
```

## Keep it healthy
- Free UptimeRobot monitor on `/health` (also alerts you if Oracle reclaims the box).
- Don't forget: run the Supabase migrations first (`report_templates`, `surveyor-location`)
  and confirm the private `survey-photos` + `reports` buckets exist.
