# Production deployment (cheap Railway + SQLite + GitHub Pages)

This guide targets a **low-cost** setup: **FastAPI on Railway**, **SQLite on a persistent volume**, and **Serverless (sleep when idle)**. The **frontend is a static build** deployed to **GitHub Pages**; `VITE_API_URL` must be the Railway API URL (see §4), not the Pages URL.

For **Postgres** (multi-instance, heavier traffic), use `DATABASE_URL` from Railway Postgres instead—omit `SQLITE_DATABASE_PATH` and do **not** enable Serverless if you need a always-on DB service.

---

## Architecture

| Piece | Where | Notes |
|--------|--------|--------|
| API | Railway (1 service) | `uvicorn backend.api.main:app`, see `railway.toml` |
| Database | SQLite file on a **Railway volume** | Path e.g. `/data/game.db` via `SQLITE_DATABASE_PATH` |
| Idle behavior | **Serverless** (`sleepApplication` in `railway.toml`) | Cold start on first request after ~10 minutes idle |
| SPA | **GitHub Pages** | Publish `frontend/dist` (see §4); `VITE_API_URL` = Railway API origin |

---

## Deployment phases (order of operations)

Work through these in sequence; details are only in the sections below—this list avoids duplicating them.

1. **Prerequisites** — Following section (Railway; GitHub account for Pages).
2. **Pre-release UI** — See `docs/DEPLOYMENT.md` (dev-only controls).
3. **Railway service + volume** — §1.
4. **Railway variables** — §2 (include every GitHub Pages origin you will use in `CORS_ORIGINS`).
5. **API deploy** — §3; confirm `GET /`.
6. **Frontend build + GitHub Pages** — §4 (`VITE_API_URL` = API from step 5).
7. **Domains** — §5 (optional custom domain on Railway and/or GitHub; update `CORS_ORIGINS` if origins change).
8. **Operations** — §6–§7 (cold starts, backups).

---

## Prerequisites

- [Railway](https://railway.com) account (Free or Hobby).
- GitHub account and repo with **Pages** enabled for this project (see §4).
- Railway CLI optional: from repo root, `railway link` then **`npm run deploy:backend`** (`railway up`).
- Repo root must remain the **working directory** for Python imports (`backend.*`).

---

## 1. Create the Railway service

1. **New project** → **Deploy from GitHub** (or CLI) using this repository.
2. Railway picks up **`railway.toml`** (start command, health check, **sleep when idle**).
3. **Add a volume** on the service:
   - Mount path: **`/data`**
   - Size: smallest tier is enough for a friends-and-family SQLite file.
4. Do **not** add the Railway **Postgres** plugin unless you are switching to Postgres.

---

## 2. Environment variables (Railway dashboard)

Variable **names** are listed in **`.env.railway.example`** (copy into the Railway UI; never commit secrets).

Set these on the **API** service:

| Variable | Required | Description |
|----------|----------|-------------|
| `JWT_SECRET` | **Yes** | Long random string. Never commit. Used to sign auth tokens (`backend/api/auth.py`). |
| `CORS_ORIGINS` | **Yes (prod)** | Comma-separated list of **exact** browser origins that will load the SPA (every GitHub Pages URL and custom domain you use—see §4). Must include `https://` (no trailing slash). Local dev defaults remain allowed if you append this list. |
| `SQLITE_DATABASE_PATH` | **Yes (SQLite prod)** | Absolute path on the volume, e.g. **`/data/game.db`**. Parent directory is created if missing. |
| `DATABASE_URL` | **No** | **Leave unset** for SQLite. If set (e.g. Postgres URL), the app uses that instead of SQLite. |

Optional:

| Variable | Description |
|----------|-------------|
| `BCRYPT_ROUNDS` | Defaults to `10`; increase only if you want slower hashing. |

**Security:** Rotate `JWT_SECRET` if leaked; existing sessions invalidate.

---

## 3. Deploy

- Push to the connected branch, or deploy from the Railway UI / `railway up`.
- First deploy: ensure the **volume** exists at `/data` **before** relying on persistent data (without a volume, the container filesystem is ephemeral).

Health check: `GET /` returns JSON `{"message":"Baggins & Allies API",...}`.

---

## 4. Build and host the frontend (GitHub Pages)

From repo root:

```bash
cd frontend
cp .env.production.example .env.production
# Edit: VITE_API_URL=https://YOUR-RAILWAY-PUBLIC-URL  (no trailing slash)
npm ci
npm run build
```

**GitHub Pages**

- Enable Pages on the repo (**Settings → Pages**): source is either **GitHub Actions** (recommended) or a branch/folder you populate with `dist` contents at the site root.
- **`CORS_ORIGINS`** on Railway must list the exact origins users open in the browser, e.g. `https://<user>.github.io`, `https://<custom-domain>`, and `https://www.<custom-domain>` if you use both.
- **Vite `base`:** leave default `/` if the site is served at the domain root (typical for a **custom domain** on GitHub Pages or a `*.github.io` user/org site). If you publish as a **project** site at `https://<user>.github.io/<repo>/`, set `base: '/<repo>/'` in `frontend/vite.config.ts` and rebuild (see [Vite base](https://vite.dev/config/shared-options.html#base)).
- **Owned domain:** In the repo’s Pages settings, set the custom domain; add the **DNS records** GitHub shows (often `CNAME` for `www`, or their apex records). Turn on **Enforce HTTPS** once DNS validates.
- **Automating deploy:** Add a workflow that checks out the repo, runs the commands above in `frontend/`, and deploys the `dist` output via the official [`actions/upload-pages-artifact`](https://github.com/actions/upload-pages-artifact) + [`actions/deploy-pages`](https://github.com/actions/deploy-pages) flow (configure **Settings → Pages → Build and deployment → GitHub Actions**). Alternatively, push `dist` to a `gh-pages` branch or use another CI step you prefer—Pages only needs the built assets at the published root.

---

## 5. Custom domain (optional)

- **API:** Railway service → **Settings → Networking → Custom domain**; follow DNS instructions.
- **Frontend:** GitHub Pages custom domain (§4); add every live **`https://…`** frontend origin to `CORS_ORIGINS`.

---

## 6. Serverless / cold starts

With **`sleepApplication = true`**, Railway may stop the container after roughly **10 minutes** with **no outbound traffic** (see [App sleeping / Serverless](https://docs.railway.com/reference/app-sleeping)). The **first request** after sleep can be slow while the container starts. SQLite on a **volume** survives sleep and redeploys.

Avoid **uptime pingers** to the API if the goal is to stay idle and minimize usage—they keep the app awake.

---

## 7. Backups

SQLite on a volume is durable **for the platform**, not a backup strategy. Periodically download or snapshot `game.db` if games matter.

---

## 8. Local vs production database

| Environment | Typical config |
|-------------|----------------|
| Local dev | No `DATABASE_URL`, no `SQLITE_DATABASE_PATH` → `backend/api/game.db` next to the API module |
| Railway SQLite | `SQLITE_DATABASE_PATH=/data/game.db`, volume at `/data`, **no** `DATABASE_URL` |
| Railway Postgres | `DATABASE_URL` from Postgres plugin; remove or ignore SQLite path |

---

## 9. Heroku / other

`Procfile` is still valid for Heroku-style hosts:

```text
web: uvicorn backend.api.main:app --host 0.0.0.0 --port $PORT
```

On Heroku, set `DATABASE_URL` for Postgres; SQLite on Heroku’s ephemeral disk is **not** recommended unless you accept data loss on restart.

---

## Reference: config files

- `railway.toml` — build/deploy/start, health check, **sleep when idle** (`sleepApplication`)
- `.env.railway.example` — names only for Railway dashboard variables
- `requirements.txt` — Python deps (includes `psycopg2-binary` for optional Postgres)
- `backend/api/database.py` — `DATABASE_URL` vs `SQLITE_DATABASE_PATH` resolution
- `frontend/.env.production.example` — `VITE_API_URL` (Railway API URL for production builds)
- GitHub repo **Settings → Pages** — GitHub Actions source + custom domain for the SPA
