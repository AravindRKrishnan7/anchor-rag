# ANchor

## Update a course from a PDF

Open http://localhost:8000/admin/upload, choose a course PDF and click **Read PDF**.
DeepSeek extracts the course fields (name, code, institute, duration, fee, batches)
and the page shows them next to the current values. Nothing is saved until you click
**Approve**, which applies the changes in one transaction and records the source PDF
in `course_updates`. Fields the PDF doesn't state are left unchanged and values not
found in the PDF text are dropped. The page has no login, so keep it local.

- If there's no exact code or name match but a course at the same institute has a
  similar name or code, the draft lists it and you must choose **Update this
  existing course** (its name and code are kept) or **Create as new course**.
- A batch the PDF gives no capacity for needs one typed in before approving.
- Seats available in the PDF are recorded as seat reports with source `pdf`, timed
  at approval, so the chatbot shows them as fresh.

`course_updates` and the `pdf` seat source were added to `db/init/02-schema.sql`.
An existing database needs the schema re-run (it is safe to repeat):

    docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < db/init/02-schema.sql

Demo file: `samples/basic-safety-training-2027.pdf` raises the Basic Safety Training
fee to ₹19,500 and adds an 11–20 Jan 2027 batch (rebuild it with
`python scripts/make_sample_pdf.py`). Undo it with `samples/reset-demo.sql`, piped
into psql the same way.
