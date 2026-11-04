# ANchor

Deploying for free (Render + Supabase + Nomic embeddings): see [docs/DEPLOY.md](docs/DEPLOY.md).

## Admin login

Course PDF uploads (`/admin/upload`), seat reports and the change log
(`/admin/changes`) need an admin login; the chat page and `/chat` stay public.
Create an admin from the terminal (the password is asked for at a hidden
prompt, at least 12 characters, and only its scrypt hash is stored):

    python -m scripts.create_admin
    python -m scripts.create_admin --reset-password   # forgot it; logs out old sessions

Then log in at http://localhost:8000/admin/login. Sessions last 8 hours. Every
PDF update and seat report records which admin made it.

## Add interviews and company documents

At http://localhost:8000/admin/add-content (dashboard → **Add interviews &
documents**) an admin can paste or upload an interview transcript (`Speaker:
text` turns; only the guest's answers are added) or upload a company PDF. The
preview shows the detected person or organization, the passages the chatbot
will search and any duplicates. Publishing requires confirming the guest's or
company's consent and records who gave it, when, and which admin. It needs
Ollama running to embed the passages.

**Unpublish** (e.g. consent withdrawn) deletes the passages from search and
keeps a record; a person added from this page is also hidden from the chatbot
when no other interview of theirs is published. `scripts/ingest_transcripts.py`
never re-adds unpublished interviews. Demo transcript:
`samples/interview-asha-rao.txt`.

## Update a course from a PDF

Open http://localhost:8000/admin/upload, choose a course PDF and click **Read PDF**.
DeepSeek extracts the course fields (name, code, institute, duration, fee, batches)
and the page shows them next to the current values. Nothing is saved until you click
**Approve**, which applies the changes in one transaction and records the source PDF
in `course_updates`. Fields the PDF doesn't state are left unchanged and values not
found in the PDF text are dropped. 
- If there's no exact code or name match but a course at the same institute has a
  similar name or code, the draft lists it and you must choose **Update this
  existing course** (its name and code are kept) or **Create as new course**.
- A batch the PDF gives no capacity for needs one typed in before approving.
- Seats available in the PDF are recorded as seat reports with source `pdf`, timed
  at approval, so the chatbot shows them as fresh.

`course_updates`, the `pdf` seat source and the admin tables were added to `db/init/02-schema.sql`.
An existing database needs the schema re-run (it is safe to repeat):

    docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < db/init/02-schema.sql

Demo file: `samples/basic-safety-training-2027.pdf` raises the Basic Safety Training
fee to ₹19,500 and adds an 11–20 Jan 2027 batch (rebuild it with
`python scripts/make_sample_pdf.py`). Undo it with `samples/reset-demo.sql`, piped
into psql the same way.
