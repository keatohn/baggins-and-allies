# AGENTS.md

Turn-based LOTR strategy game: FastAPI + Python backend (`backend/`), React 19 + Vite + TS frontend (`frontend/`), tests in `test/`. Root `package.json` orchestrates both — there is no other monorepo tooling.

## Commands (run from repo root)

- **Dev servers:** `npm run dev` (backend :8000 + Vite :5173). For local dev prefer `npm run dev:files` — see "Setup data" below. Scripts call `.venv/bin/python` directly, so create it first: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
- **Stuck ports:** `npm run kill-ports` or `npm run restart` (Vite is `strictPort: 5173`).
- **Tests:** `.venv/bin/pytest test/` from repo root (tests import `backend.*`, so keep CWD at root). Single test: `.venv/bin/pytest test/test_movement.py::test_name`. `pytest` is **not** in `requirements.txt` — install it into `.venv` if missing. A few test files also run standalone: `PYTHONPATH=. .venv/bin/python test/test_movement.py`.
- **Frontend:** `npm run lint` (eslint); `npm run build` runs `tsc -b && vite build` (that's the typecheck). There is no frontend test runner, no root lint/typecheck/test scripts, no Python lint/typecheck config committed, and **no CI runs tests** — run pytest yourself before pushing.
- Tests need no services: they load setup JSON from `backend/data/setups/` directly (some use an in-process FastAPI `TestClient`). Many tests `pytest.skip` when a setup lacks a unit — run the full suite, not one file, to catch regressions.
- `test/` also contains a standalone `map-graph-debug` Vite app with its own `package.json` — unrelated to `frontend/`.

## Wiring

- API entrypoint: `backend/api/main.py` (`uvicorn backend.api.main:app`; all HTTP routes live in this one large file, port 8000 in dev). Root `main.py` is only an engine demo, not the API.
- `backend/engine/` is a deterministic engine: actions go through `apply_action(state, action, ...) -> (state, events)`. AI opponent logic is in `backend/ai/`.
- **Backend routes have no `/api` prefix.** The Vite dev proxy forwards only `/api/*` → `localhost:8000` with the prefix stripped. Frontend must always call `/api/...` (`frontend/src/services/api.ts` resolves the base: `/api` in dev, `VITE_API_URL` in prod). Don't add `/api` to backend routes; don't drop it from frontend calls.
- Game data: `backend/data/setups/<setup_id>/` (manifest, units, territories, factions, camps, ports, starting_setup JSON) plus global `catalog.json` (unit specials), `signals.json`, `audio_gains.json`. Default setup id lives in `backend/config.py` (`DEFAULT_SETUP_ID`).

## Setup data: files vs DB (easy to get wrong)

- Default is `SETUP_SOURCE=db`: on startup `init_db()` seeds the DB from files **only if empty** (`backend/api/database.py`). Editing `backend/data/setups/*.json` will **not** affect an existing dev DB (`backend/api/game.db`, gitignored, recreated if deleted).
- For local dev use `SETUP_SOURCE=files` (`npm run dev:files`) so JSON edits affect newly created games. Never set it in production — startup raises if a production env var is set alongside files (`backend/api/main.py`).
- `players.is_admin` is recomputed at every startup from `ADMIN_PLAYER_EMAIL` / `ADMIN_EMAIL(S)` env vars; DB edits to it are wiped. Other env vars (`JWT_SECRET`, `CORS_ORIGINS`, `SQLITE_DATABASE_PATH`, …) are documented in `docs/PRODUCTION_DEPLOYMENT.md` §2.
- The backend refuses to start on Railway with the default (ephemeral) SQLite path unless `SQLITE_DATABASE_PATH` points at a volume.

## Deploy

- Production stack of record: Railway (API + SQLite volume) + GitHub Pages (SPA) — follow **`docs/PRODUCTION_DEPLOYMENT.md`**. `DEPLOY.md` covers alternate stacks (Vercel/Heroku/Render) and defers to it.
- `npm run deploy:backend` = `railway up`. Frontend deploys via `.github/workflows/deploy-github-pages.yml` on push to `main`; it requires repo variable `VITE_API_URL` = the Railway origin (the workflow rejects `bagginsandallies.com` and `github.io`). `npm run deploy:frontend` (Vercel) also exists; `build:frontend` copies `frontend/.env.production.example` → `.env.production` if missing.
- Never commit Railway tokens: `DEPLOY.md` uses a placeholder and the real token lives only in gitignored `.env.railway`. (An earlier real token in git history was rotated — treat any old copy as compromised.)

## Conventions (from `.cursor/rules/`, which is gitignored — preserved here)

- Use user-provided commit messages **verbatim**: no added prefix, body, or rewording.
- Stage/commit only changes produced in the current chat; if it's ambiguous whether a dirty file belongs to this chat, ask once before staging.
- Start local dev servers with `SETUP_SOURCE=files`; never apply it to build/deploy/prod commands.

## Autonomous Development Workflow

When assigned a GitHub issue:

1. Read the issue and relevant project documentation.
2. Identify the affected components before editing.
3. Implement the smallest complete solution.
4. Add or update relevant automated tests.
5. Run tests and fix failures caused by your changes.
6. Run linting/type checks where configured.
7. Review the final diff for bugs and unrelated changes.
8. Summarize the implementation and test results.

### GitHub Workflow
- Use a dedicated feature branch for each issue.
- Give the feature branch a reasonable and brief name based on the work.
- Each work session should commit and push to that feature branch so we don't lose changes.
- Commit messages should also be brief and high-level.
- Open a draft pull request linked to the issue.
- Never push directly to main.
- Never merge pull requests automatically.
- Do not modify unrelated functionality.

### Handling Problems
- Fix obvious bugs discovered in the code being modified.
- Do not undertake unrelated refactoring.
- If requirements are ambiguous, ask a specific question
  in the GitHub issue and stop implementation.
- If blocked by missing credentials, dependencies, or
  infrastructure, explain the blocker in the issue.
- Never claim tests passed unless they actually ran.
- If tests cannot run, document why.

### Autonomy
- Make reasonable implementation decisions independently.
- Do not ask for approval on routine technical decisions.
- Escalate changes affecting core game mechanics,
  database migrations, security, or public APIs.

## Efficient Issue Resolution
- Start by identifying the most relevant files.
- Avoid exploring unrelated parts of the repository.
- Prefer targeted searches over broad codebase exploration.
- For small bug fixes, aim for a minimal change.
- If investigation exceeds 30 steps without identifying
  the cause, summarize findings and report the blocker.
- Avoid repeatedly searching the same files.
