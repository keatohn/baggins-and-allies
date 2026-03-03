# Deploy Baggins & Allies to [bagginsandallies.com](http://bagginsandallies.com)

You own **bagginsandallies.com** on GoDaddy. Use this guide to get the app live.

**Backend: pick one** — Heroku, Railway, or Render. You do **not** use Railway if you choose Heroku (and vice versa). Frontend can stay on Vercel for any backend choice.

---

## Deploy now: Vercel (frontend) + your choice of backend

### Option 1: Backend on **Heroku**

1. Install the [Heroku CLI](https://devcenter.heroku.com/articles/heroku-cli) and run `heroku login`.
2. From the repo root:

```bash
cd /Users/keaton/projects/baggins-and-allies
heroku create your-app-name   # or leave blank for a generated name
heroku config:set CORS_ORIGINS="https://bagginsandallies.com,https://www.bagginsandallies.com"
git push heroku main
```

3. Copy the backend URL (e.g. `https://your-app-name.herokuapp.com`). Use it as `VITE_API_URL` when deploying the frontend (see Frontend step below).
4. The repo includes a **Procfile** so Heroku runs `uvicorn backend.api.main:app`. No Railway or Render config is used when you host on Heroku.

**Note:** Heroku’s free tier was discontinued. Use an Eco/Basic dyno; the app uses SQLite by default (ephemeral on Heroku unless you add persistent storage or Postgres).

---

### Option 2: Backend on **Railway**

**1. Backend (Railway)** — use a token instead of `railway login` (avoids browserless login errors):

1. In your browser go to **[https://railway.com/account/tokens](https://railway.com/account/tokens)**.
2. Create an **Account token** (leave workspace as “No workspace” for full account access). Copy the token immediately (it's shown only once). If you get "Unauthorized" later, create a new Account token and use that.
3. In your terminal:

```bash
cd /Users/keaton/projects/baggins-and-allies
# Install Railway CLI once: npm i -g @railway/cli
export RAILWAY_API_TOKEN="95c3398f-b8c7-4bc3-9354-32dcc5b9df59"
railway link     # pick an existing project, or create one when prompted
# If new project: add a service (Dashboard → New → Empty Project, then link again; or use railway init to create from CLI)
railway variables set CORS_ORIGINS="https://bagginsandallies.com,https://www.bagginsandallies.com"
railway up
```

Copy the deployed backend URL (e.g. `https://baggins-and-allies-production.up.railway.app`).

**If you see "Unauthorized" with RAILWAY_API_TOKEN:** Create a **new** Account token at [https://railway.com/account/tokens](https://railway.com/account/tokens) (scope: **No workspace**). Set `export RAILWAY_API_TOKEN="new_token"` and run your command again. Don’t use a project- or workspace-scoped token for `link` / `init` / `up`.

Copy the deployed backend URL (e.g. `https://baggins-and-allies-production.up.railway.app`).

---

### Frontend (Vercel) and DNS — after backend is live (Heroku or Railway)

**Frontend:** Set `VITE_API_URL` to your **backend** URL (Heroku app URL or Railway URL), then build and deploy:

```bash
cd /Users/keaton/projects/baggins-and-allies/frontend
# Use your actual backend URL from Heroku or Railway
echo "VITE_API_URL=https://YOUR-BACKEND-URL" > .env.production
npm run build
npx vercel --prod
```

When prompted, link to a Vercel project and add your domain **bagginsandallies.com** in the Vercel dashboard (Project → Settings → Domains).

**GoDaddy DNS:**

- Add **CNAME** `www` → `cname.vercel-dns.com` (or the value Vercel shows).
- For apex `bagginsandallies.com`, use Vercel’s recommended DNS (often A record to Vercel’s IP or CNAME flattening).

**After domain is live:** You can set `VITE_API_URL=https://bagginsandallies.com/api` and redeploy the frontend only if you later put the API behind the same domain (e.g. with a serverless rewrite). Until then, the frontend calls your Heroku or Railway backend URL directly.

---

## Option A: Single VPS (recommended for simplicity)

Use a small Linux server (DigitalOcean, Linode, Vultr, etc.) and run everything there.

### 1. Server setup

- Create a droplet/instance (Ubuntu 22.04).
- Point your domain at the server:
  - **GoDaddy DNS**: Add an **A record** for `@` (and optionally `www`) with the server’s **public IP**.
  - Optional: add a CNAME `www` → `bagginsandallies.com` if you want both.

### 2. On the server

```bash
# Install Docker (or use Node + Python directly)
curl -fsSL https://get.docker.com | sh

# Clone your repo (or upload files)
git clone <your-repo-url> /opt/baggins-and-allies
cd /opt/baggins-and-allies
```

### 3. Backend (API)

```bash
cd /opt/baggins-and-allies
# If using Docker:
docker build -f Dockerfile.backend -t baggins-api .
# Persist SQLite DB (create dir: mkdir -p data)
docker run -d --name api -p 127.0.0.1:8000:8000 \
  -e CORS_ORIGINS="https://bagginsandallies.com,https://www.bagginsandallies.com" \
  -e DATABASE_URL="sqlite:////app/data/game.db" \
  -v $(pwd)/data:/app/data \
  baggins-api

# Or without Docker (use a venv):
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export CORS_ORIGINS="https://bagginsandallies.com,https://www.bagginsandallies.com"
uvicorn backend.api.main:app --host 0.0.0.0 --port 8000
# Run under systemd or screen for production
```

### 4. Frontend (build + serve)

```bash
cd /opt/baggins-and-allies/frontend
# Point API at your domain (same server: use /api proxy, or full URL)
echo 'VITE_API_URL=https://bagginsandallies.com/api' > .env.production
npm ci && npm run build
# Serve the contents of dist/ with nginx (see below)
```

### 5. Nginx (reverse proxy + SSL)

Install nginx and certbot, then use a config like this so the site is served over HTTPS and the API is under `/api`:

```nginx
# /etc/nginx/sites-available/bagginsandallies.com
server {
    listen 80;
    server_name bagginsandallies.com www.bagginsandallies.com;
    return 301 https://$host$request_uri;
}
server {
    listen 443 ssl http2;
    server_name bagginsandallies.com www.bagginsandallies.com;

    ssl_certificate     /etc/letsencrypt/live/bagginsandallies.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bagginsandallies.com/privkey.pem;

    root /opt/baggins-and-allies/frontend/dist;
    index index.html;
    location / {
        try_files $uri $uri/ /index.html;
    }
    location /api {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Then:

```bash
sudo certbot --nginx -d bagginsandallies.com -d www.bagginsandallies.com
sudo nginx -t && sudo systemctl reload nginx
```

Set **VITE_API_URL** to `https://bagginsandallies.com/api` so the frontend calls your API at `/api`.

---

## Option B: Frontend on Vercel, Backend on Railway/Render

- **Vercel**: Connect the repo, set root to `frontend`, build command `npm run build`, output `dist`. Add env **VITE_API_URL** = `https://your-backend-url.up.railway.app` (or your Render URL).
- **Railway / Render**: Deploy the backend (see Dockerfile.backend or use their Python detection). Set **CORS_ORIGINS** = `https://bagginsandallies.com,https://www.bagginsandallies.com` (and your Vercel URL if different).
- **GoDaddy**: CNAME `bagginsandallies.com` (or `www`) to Vercel’s target (e.g. `cname.vercel-dns.com`) if you use a custom domain there.

---

## Database: SQLite vs Postgres

**You do not need Postgres for production.** The app uses **SQLite** by default (single file `game.db`). That’s fine for production and is simpler to run.

Use **Postgres** only if you want it (e.g. you already have a Postgres host, or you expect very high concurrent writes). Set `DATABASE_URL=postgresql://...` and the app will use it; otherwise leave it unset and SQLite is used.

---

## Environment summary


| Where              | Variable     | Example                                                         |
| ------------------ | ------------ | --------------------------------------------------------------- |
| Frontend build     | VITE_API_URL | `https://bagginsandallies.com/api` (or full backend URL)        |
| Backend            | CORS_ORIGINS | `https://bagginsandallies.com,https://www.bagginsandallies.com` |
| Backend (optional) | DATABASE_URL | Omit for SQLite; or `postgresql://...` for Postgres             |


---

## Quick checklist

1. [ ] Domain A record (or CNAME) in GoDaddy points at your host.
2. [ ] Backend running with CORS_ORIGINS including your domain.
3. [ ] Frontend built with VITE_API_URL pointing at your API.
4. [ ] HTTPS (e.g. Let’s Encrypt) and nginx (or host) serving the app and proxying `/api` to the backend.

