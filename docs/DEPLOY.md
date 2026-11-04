# Deploying Anchor for free

The app runs on a **Render free web service**, the database on a **Supabase
free project** (Postgres with pgvector), and search embeddings come from
**Nomic's hosted API** (`nomic-embed-text-v1.5`, the same model as local
Ollama). Chat uses the DeepSeek API as before.

Run all commands from the repo root.

## 1. Supabase database

1. At https://supabase.com create a project. Note the database password.
   Pick a region close to your users; Render's nearest region to Asia is
   Singapore (set in `render.yaml`).
2. In the project, click **Connect** → **Connection string** → **Session
   pooler** and copy the URI. It looks like
   `postgresql://postgres.<project-ref>:[YOUR-PASSWORD]@aws-0-<region>.pooler.supabase.com:5432/postgres`.
   Put your password in place of `[YOUR-PASSWORD]`; if it contains
   characters like `@ : / ? #`, URL-encode them (e.g. `@` → `%40`).
   - Use the **session pooler** (port 5432), not the direct connection:
     the direct one is IPv6-only on the free plan and Render can't reach it.
3. Add it to your local `.env`, with your embeddings settings:

       DATABASE_URL=postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
       EMBEDDINGS_PROVIDER=nomic
       NOMIC_API_KEY=nk-...

   While `DATABASE_URL` is set, your local app and scripts use Supabase
   instead of the Docker database (see "Working locally" below).
4. Create the tables. You don't need `psql` installed; the Docker database
   container has it (start it with `docker compose up -d db`):

       export DATABASE_URL="$(.venv/bin/python -c 'from dotenv import dotenv_values; print(dotenv_values(".env")["DATABASE_URL"])')"
       for f in db/init/0*.sql; do
         echo "$f"
         docker compose exec -T -e PGURL="$DATABASE_URL" -e PGSSLMODE=require db \
           sh -c 'psql "$PGURL" -q -v ON_ERROR_STOP=1' < "$f"
       done

   (`.env` is read with python-dotenv, like the app does, rather than
   `source .env`: a shell would misread passwords containing characters
   such as `<`, `&` or `$`, and can print parts of them in its errors.)

   This enables pgvector (`01`), creates the schema (`02`), loads the sample
   directory (`03`) and turns on row-level security (`04`). `04` stops
   Supabase's public REST API from reading the tables (including admin
   password hashes); the app isn't affected because it connects as the
   tables' owner. Re-run `04` if you ever add a table.

   (Alternative without Docker: paste each file into Supabase's **SQL
   Editor** and run it, in order.)
5. Load the interview transcripts (embedded with Nomic):

       python -m scripts.ingest_transcripts

6. Create your admin account (asks for the password at a hidden prompt):

       python -m scripts.create_admin

7. Optional check: `python -m uvicorn app.main:app` and open
   http://localhost:8000. Ask the chatbot something, and log in at
   `/admin/login`. (The `Secure` login cookie works on http://localhost in
   Chrome and Firefox.)

## 2. Render web service

1. Push this repo to GitHub (`git push`). Render deploys from it.
2. At https://dashboard.render.com click **New** → **Blueprint**, connect
   GitHub and choose this repository. Render reads `render.yaml` and shows
   one free web service, `anchor`.
3. It asks for the secret values (they are never stored in git):

   | Variable | Value |
   | --- | --- |
   | `DATABASE_URL` | The Supabase session pooler URI from step 1.2 |
   | `DEEPSEEK_API_KEY` | Your DeepSeek API key |
   | `NOMIC_API_KEY` | Your Nomic API key |

   These are already set by `render.yaml` (change them in the dashboard if
   needed):

   | Variable | Value | Why |
   | --- | --- | --- |
   | `EMBEDDINGS_PROVIDER` | `nomic` | Ollama can't run on Render's free tier |
   | `ADMIN_COOKIE_SECURE` | `true` | Login cookie only sent over https |
   | `PYTHON_VERSION` | `3.14.6` | Same Python as local development |

   Optional: `DEEPSEEK_MODEL` (default `deepseek-chat`) and `DEEPSEEK_URL`.
   Not used on Render: `POSTGRES_*`, `OLLAMA_*`, `GEMINI_API_KEY`.
4. Click **Apply**. The first build takes a few minutes. Then open
   `https://<your-service>.onrender.com`:
   - `/health` → `{"status":"ok"}`
   - `/db-health` → shows Postgres 17 and the pgvector version
   - `/` → landing page; **Start chatting** and ask a question
   - **Admin** → log in with the account from step 1.6
5. Every `git push` to the main branch redeploys automatically.

The start command runs **one** uvicorn process on purpose: drafts, previews
and login lockouts are kept in memory. `--proxy-headers` makes the app see
each visitor's real IP (for the login lockout) behind Render's proxy.

## 3. What the free tiers do

**Render free web service** spins down after 15 minutes without traffic and
takes about a minute to wake up (the browser shows a loading page). It also
restarts on every deploy and may restart at any time. Each restart clears
what the app keeps in memory:

| Lost on sleep / restart | Effect |
| --- | --- |
| PDF course drafts (`/admin/upload`) | An unapproved draft must be uploaded again |
| Interview / document previews (`/admin/add-content`) | Preview again before publishing |
| Failed-login counters | Lockouts reset early |
| Chat profile cache | Reloaded from the database on the next chat (no visible effect) |

Kept, because they live in the database: admin login sessions (8 hours),
courses, seats, published content, consent records and the change log.
Render gives 750 free instance hours a month, enough for one service that's
always on; sleeping hours don't count.

**Supabase free project** is paused after about a week without database
activity (Supabase emails you first). Opening the project dashboard or using
the app counts as activity. A paused project can be restored from the
dashboard.

**Nomic API**: check your usage and any free allowance on the Nomic Atlas
dashboard; the API itself doesn't report remaining credit. If it stops being
free, set `EMBEDDINGS_PROVIDER=gemini` with a `GEMINI_API_KEY` (free tier;
Google may use free-tier content to improve its products) and re-embed:

    python -m scripts.reembed

with your `.env` pointing at Supabase. Search only compares vectors from the
current model, so re-embed straight after switching (it's quick and can be
stopped and re-run).

## Working locally

- **Against the local Docker database**: comment out `DATABASE_URL` in
  `.env` and set `EMBEDDINGS_PROVIDER=ollama` (or keep `nomic`; it gives the
  same vectors but chunks are labelled by provider, so run
  `python -m scripts.reembed` after switching).
- **Against Supabase**: keep `DATABASE_URL` set. Changes you make locally
  are live on the deployed site.
