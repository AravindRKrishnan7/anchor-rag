"""Adding interviews and company documents to the chatbot from the browser.

/admin/add-content previews a pasted or uploaded transcript, or a company
PDF, as the chunks the chatbot would search. Nothing is saved until an admin
confirms the guest's or company's consent and publishes. Unpublishing
(consent withdrawn) deletes the chunks and keeps the row as the record.
"""

import re
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import psycopg
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from psycopg.rows import dict_row
from pydantic import BaseModel

from app.auth import require_admin_page
from app.db import DB_ERRORS, pool
from app.embeddings import EmbeddingError, embed_documents
from app.ingest import (
    INTERVIEWER, answer_chunks, content_hash, is_guest, parse_turns, pdf_text, split_header,
    store_chunks, text_chunks,
)

router = APIRouter(
    prefix="/admin", dependencies=[Depends(require_admin_page)], include_in_schema=False
)
# Called after people are added or hidden; main.py registers the chat
# profile cache reset here (importing main from this module would be circular).
directory_listeners: list = []

TXT_MAX_BYTES = 1024 * 1024
PDF_MAX_BYTES = 5 * 1024 * 1024
# Previews live in memory until published, so a restart discards them.
DRAFT_TTL = timedelta(hours=1)
PREVIEW_CHUNKS = 3
MAX_TEXT_FIELD = 200
EARLIEST_YEAR = 1950
YOUTUBE_URL = re.compile(
    r"^https://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?v=|shorts/|live/)|youtu\.be/)[\w-]{6,}\S*$"
)
# Prefix of interviews.source_file for browser uploads; other rows came
# from scripts/ingest_transcripts.py.
UPLOAD_SOURCE = "admin/"

ADD_CONTENT_HTML = Path(__file__).parent / "static" / "add-content.html"

drafts: dict[str, dict] = {}

def clean(value: str | None, field: str | None = None) -> str | None:
    value = (value or "").strip()
    if field and len(value) > MAX_TEXT_FIELD:
        raise HTTPException(status_code=422, detail=f"{field} is too long")
    return value or None

def parse_int(value: str | None, field: str) -> int | None:
    value = clean(value)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field} must be a number")

def read_upload(file: UploadFile, max_bytes: int, label: str) -> bytes:
    data = file.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"{label} is larger than {max_bytes // 1024 // 1024} MB")
    return data

def notify_directory_changed():
    for listener in directory_listeners:
        listener()

# The organization a preview names: an existing one by ID, or a new one by
# name and type (created on publish). An existing name must be chosen
# instead of re-created.
def resolve_organization(cur, organization_id: int | None, name: str | None,
                         org_type: str | None) -> dict:
    if organization_id is not None:
        org = cur.execute(
            "SELECT id, name, org_type FROM organizations WHERE id = %s", (organization_id,)
        ).fetchone()
        if org is None:
            raise HTTPException(status_code=422, detail="Organization not found")
        return {**org, "new": False}
    if name is None:
        raise HTTPException(status_code=422, detail="Choose an organization or enter a new one")
    existing = cur.execute(
        "SELECT name FROM organizations WHERE lower(name) = lower(%s)", (name,)
    ).fetchone()
    if existing:
        raise HTTPException(
            status_code=422,
            detail=f"{existing['name']} is already in the directory; choose it from the list",
        )
    if org_type is None:
        raise HTTPException(status_code=422, detail="Enter the new organization's type")
    return {"id": None, "name": name, "org_type": org_type, "new": True}

# Why publishing would add nothing new (blocked), or a warning when only
# some passages are already in the chatbot.
def duplicate_check(cur, chash: str, chunks: list[str]) -> tuple[str | None, str | None]:
    published = cur.execute(
        """
        SELECT title FROM interviews WHERE content_hash = %(h)s AND unpublished_at IS NULL
        UNION ALL
        SELECT title FROM documents WHERE content_hash = %(h)s AND unpublished_at IS NULL
        LIMIT 1
        """,
        {"h": chash},
    ).fetchone()
    if published:
        return f"Already published as “{published['title']}”", None
    distinct = set(chunks)
    found = cur.execute(
        "SELECT count(DISTINCT content) AS n FROM chunks WHERE content = ANY(%s)", (list(distinct),)
    ).fetchone()["n"]
    if found == len(distinct):
        return "Every passage is already in the chatbot from another interview or document", None
    if found:
        return None, f"{found} of {len(distinct)} passages are already in the chatbot"
    return None, None

def save_draft(draft: dict) -> str:
    now = datetime.now(timezone.utc)
    for draft_id in [k for k, d in drafts.items() if now - d["created_at"] > DRAFT_TTL]:
        drafts.pop(draft_id, None)
    draft_id = uuid.uuid4().hex
    drafts[draft_id] = {**draft, "created_at": now}
    return draft_id

def preview_response(draft_id: str, draft: dict, extra: dict) -> dict:
    return {
        "draft_id": draft_id,
        "kind": draft["kind"],
        "title": draft["title"],
        "chunk_count": len(draft["chunks"]),
        "chunks_preview": draft["chunks"][:PREVIEW_CHUNKS],
        "warnings": draft["warnings"],
        "blocked_reason": draft["blocked_reason"],
        "can_publish": draft["blocked_reason"] is None,
        "consent_default": draft["consent_default"],
        **extra,
    }

@router.get("/add-content")
def add_content_page():
    return FileResponse(ADD_CONTENT_HTML)

@router.get("/api/content/options")
def content_options():
    try:
        with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            people = cur.execute(
                "SELECT id, name, role, chat_hidden_at IS NOT NULL AS hidden "
                "FROM people ORDER BY name"
            ).fetchall()
            organizations = cur.execute(
                "SELECT id, name, org_type FROM organizations ORDER BY name"
            ).fetchall()
    except DB_ERRORS:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {
        "people": people,
        "organizations": organizations,
        "org_types": sorted({o["org_type"] for o in organizations}),
    }

@router.post("/api/content/preview-interview")
def preview_interview(
    transcript: str | None = Form(None),
    transcript_file: UploadFile | None = File(None),
    person_id: str | None = Form(None),
    new_person_name: str | None = Form(None),
    new_person_role: str | None = Form(None),
    organization_id: str | None = Form(None),
    new_org_name: str | None = Form(None),
    new_org_type: str | None = Form(None),
    since_year: str | None = Form(None),
    title: str | None = Form(None),
    video_url: str | None = Form(None),
    interviewed_on: str | None = Form(None),
):
    if transcript_file is not None and transcript_file.filename:
        try:
            text = read_upload(transcript_file, TXT_MAX_BYTES, "Transcript").decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=422, detail="Transcript must be a UTF-8 .txt file")
        source_name = Path(transcript_file.filename).name
    else:
        text = transcript or ""
        if len(text.encode()) > TXT_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Transcript is larger than 1 MB")
        source_name = "pasted transcript"
    if not text.strip():
        raise HTTPException(status_code=422, detail="Paste a transcript or choose a .txt file")
    # A transcript file from transcripts/ may start with its header lines.
    header, body = split_header(text)
    try:
        turns = parse_turns(body)
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail=f"{e}. Each turn should start with the speaker's name and a colon, "
            "e.g. “Interviewer: …” or “Asha Rao: …”",
        )

    title = clean(title, "Title") or header.get("title")
    if not title:
        raise HTTPException(status_code=422, detail="Enter the interview title")
    video_url = clean(video_url, "YouTube URL")
    if video_url and not YOUTUBE_URL.match(video_url):
        raise HTTPException(status_code=422, detail="Enter a YouTube link (https://www.youtube.com/watch?v=… or https://youtu.be/…)")
    interviewed_on = clean(interviewed_on) or header.get("date")
    if interviewed_on:
        try:
            interviewed_on = date.fromisoformat(interviewed_on)
        except ValueError:
            raise HTTPException(status_code=422, detail="Interview date must be a date (YYYY-MM-DD)")
        if interviewed_on > date.today():
            raise HTTPException(status_code=422, detail="Interview date is in the future")

    person_id = parse_int(person_id, "Person")
    since_year = parse_int(since_year, "Working there since")
    warnings = []
    try:
        with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            if person_id is not None:
                person = cur.execute(
                    "SELECT id, name, role, chat_hidden_at IS NOT NULL AS hidden "
                    "FROM people WHERE id = %s", (person_id,)
                ).fetchone()
                if person is None:
                    raise HTTPException(status_code=422, detail="Person not found")
                if person["hidden"]:
                    warnings.append(f"{person['name']} is hidden from the chatbot (consent was "
                                    "withdrawn); publishing shows them again")
                subject = {**person, "new": False, "organization": None}
                org = None
            else:
                name = clean(new_person_name, "Name")
                role = clean(new_person_role, "Role")
                if not name or not role:
                    raise HTTPException(status_code=422, detail="Choose a person, or enter a new person's name and role")
                taken = cur.execute(
                    "SELECT name FROM people WHERE lower(name) = lower(%s)", (name,)
                ).fetchone()
                if taken:
                    raise HTTPException(status_code=422, detail=f"{taken['name']} is already in the directory; choose them from the list")
                org = resolve_organization(cur, parse_int(organization_id, "Company"),
                                           clean(new_org_name, "Company name"),
                                           clean(new_org_type, "Company type"))
                if since_year is not None and not EARLIEST_YEAR <= since_year <= date.today().year:
                    raise HTTPException(status_code=422, detail=f"Working there since must be between {EARLIEST_YEAR} and {date.today().year}")
                subject = {"id": None, "name": name, "role": role, "new": True,
                           "organization": org["name"]}

            guest = subject["name"]
            chunks = answer_chunks(turns, guest)
            counts = Counter(speaker for speaker, _ in turns)
            if not chunks:
                found = ", ".join(counts) or "none"
                raise HTTPException(
                    status_code=422,
                    detail=f"No answers from {guest} in this transcript (speakers found: {found}). "
                    f"Their turns should start with “{guest}:”",
                )
            speakers = [{"speaker": s, "turns": n, "indexed": is_guest(s, guest)}
                        for s, n in counts.items()]
            others = [s for s in counts if s != INTERVIEWER and not is_guest(s, guest)]
            if others:
                warnings.append(f"Not indexed, treated as questions: {', '.join(others)}")
            # Who the guest's speaker labels match in the directory.
            labels = [s for s in counts if is_guest(s, guest)]
            detected = cur.execute(
                """
                SELECT DISTINCT name FROM people
                WHERE EXISTS (SELECT 1 FROM unnest(%s::text[]) AS label
                              WHERE lower(people.name) LIKE '%%' || lower(label) || '%%')
                ORDER BY name
                """,
                (labels,),
            ).fetchall()
            detected = [r["name"] for r in detected]
            if subject["new"] and detected:
                warnings.append(f"The transcript's speaker matches {', '.join(detected)} "
                                "already in the directory; choose them instead of a new person "
                                "if it's the same person")
            chash = content_hash(chunks)
            blocked, duplicate_warning = duplicate_check(cur, chash, chunks)
    except DB_ERRORS:
        raise HTTPException(status_code=503, detail="Database unavailable")
    if duplicate_warning:
        warnings.append(duplicate_warning)

    draft = {
        "kind": "interview", "title": title, "chunks": chunks, "content_hash": chash,
        "subject": subject, "organization": org, "since_year": since_year,
        "interviewed_on": interviewed_on, "video_url": video_url, "source_name": source_name,
        "warnings": warnings, "blocked_reason": blocked, "consent_default": guest,
    }
    draft_id = save_draft(draft)
    return preview_response(draft_id, draft, {
        "subject": subject, "detected": detected, "speakers": speakers,
        "interviewed_on": interviewed_on, "video_url": video_url, "source_name": source_name,
    })

@router.post("/api/content/preview-document")
def preview_document(
    file: UploadFile = File(...),
    organization_id: str | None = Form(None),
    new_org_name: str | None = Form(None),
    new_org_type: str | None = Form(None),
    title: str | None = Form(None),
):
    data = read_upload(file, PDF_MAX_BYTES, "PDF")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(status_code=422, detail="File is not a PDF")
    title = clean(title, "Title")
    if not title:
        raise HTTPException(status_code=422, detail="Enter the document title")
    text = pdf_text(data)
    if not text:
        raise HTTPException(status_code=422, detail="PDF has no text to read (it may be a scanned image)")
    chunks = text_chunks(text)
    warnings = []
    try:
        with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            org = resolve_organization(cur, parse_int(organization_id, "Organization"),
                                       clean(new_org_name, "Organization name"),
                                       clean(new_org_type, "Organization type"))
            # Organizations the PDF names, to catch a document filed under the
            # wrong one.
            detected = [r["name"] for r in cur.execute(
                "SELECT name FROM organizations WHERE position(lower(name) IN lower(%s)) > 0 "
                "ORDER BY name", (text,)
            ).fetchall()]
            if detected and org["name"] not in detected:
                warnings.append(f"The PDF mentions {', '.join(detected)} but not {org['name']}; "
                                "check the organization is right")
            chash = content_hash(chunks)
            blocked, duplicate_warning = duplicate_check(cur, chash, chunks)
    except DB_ERRORS:
        raise HTTPException(status_code=503, detail="Database unavailable")
    if duplicate_warning:
        warnings.append(duplicate_warning)
    source_name = Path(file.filename or "document.pdf").name
    draft = {
        "kind": "document", "title": title, "chunks": chunks, "content_hash": chash,
        "organization": org, "source_name": source_name,
        "warnings": warnings, "blocked_reason": blocked, "consent_default": org["name"],
    }
    draft_id = save_draft(draft)
    return preview_response(draft_id, draft, {
        "subject": {"name": org["name"], "org_type": org["org_type"], "new": org["new"]},
        "detected": detected, "source_name": source_name,
    })

class Publish(BaseModel):
    draft_id: str
    consent: bool = False
    consent_given_by: str = ""

def insert_organization(cur, org: dict, admin_id: int) -> int:
    if org["id"] is not None:
        return org["id"]
    return cur.execute(
        "INSERT INTO organizations (name, org_type, created_by_admin_id) "
        "VALUES (%s, %s, %s) RETURNING id",
        (org["name"], org["org_type"], admin_id),
    ).fetchone()["id"]

@router.post("/api/content/publish")
def publish_content(request: Publish, admin: dict = Depends(require_admin_page)):
    # The consent box is checked here, not just in the page.
    if not request.consent:
        raise HTTPException(status_code=422, detail="Confirm that the guest or company has consented")
    consent_given_by = clean(request.consent_given_by, "Consent given by")
    if not consent_given_by:
        raise HTTPException(status_code=422, detail="Enter who gave consent")
    draft = drafts.get(request.draft_id)
    if draft is None or datetime.now(timezone.utc) - draft["created_at"] > DRAFT_TTL:
        raise HTTPException(status_code=404, detail="Preview not found or expired; preview it again")
    if draft["blocked_reason"]:
        raise HTTPException(status_code=422, detail=draft["blocked_reason"])
    if drafts.pop(request.draft_id, None) is None:
        raise HTTPException(status_code=404, detail="Preview not found or expired; preview it again")

    # Embedded before anything is written, so a provider failure saves nothing.
    try:
        vectors = embed_documents(draft["chunks"])
    except EmbeddingError as e:
        drafts[request.draft_id] = draft
        raise HTTPException(status_code=503, detail=f"{e}; nothing was saved, try again")

    source_file = f"{UPLOAD_SOURCE}{request.draft_id}/{draft['source_name']}"
    try:
        with pool.connection() as conn, conn.transaction(), \
                conn.cursor(row_factory=dict_row) as cur:
            # Checked again: the same content may have been published since.
            blocked, _ = duplicate_check(cur, draft["content_hash"], draft["chunks"])
            if blocked:
                raise HTTPException(status_code=409, detail=blocked)
            if draft["kind"] == "interview":
                subject = draft["subject"]
                if subject["new"]:
                    org_id = insert_organization(cur, draft["organization"], admin["id"])
                    person_id = cur.execute(
                        "INSERT INTO people (name, role, created_by_admin_id) "
                        "VALUES (%s, %s, %s) RETURNING id",
                        (subject["name"], subject["role"], admin["id"]),
                    ).fetchone()["id"]
                    # Only the year is asked for; the profile shows only the year.
                    since = date(draft["since_year"], 1, 1) if draft["since_year"] else None
                    cur.execute(
                        "INSERT INTO employment (person_id, organization_id, title, start_date) "
                        "VALUES (%s, %s, %s, %s)",
                        (person_id, org_id, subject["role"], since),
                    )
                else:
                    person_id = subject["id"]
                    # New consent: show a previously hidden person again.
                    cur.execute("UPDATE people SET chat_hidden_at = NULL WHERE id = %s", (person_id,))
                item_id = cur.execute(
                    """
                    INSERT INTO interviews
                        (person_id, title, interviewed_on, video_url, source_file, content_hash,
                         consent_given_by, consent_recorded_at, consent_recorded_by_admin_id,
                         published_at, published_by_admin_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, now(), %s, now(), %s)
                    RETURNING id
                    """,
                    (person_id, draft["title"], draft["interviewed_on"], draft["video_url"],
                     source_file, draft["content_hash"], consent_given_by, admin["id"], admin["id"]),
                ).fetchone()["id"]
                store_chunks(cur, draft["chunks"], vectors, interview_id=item_id, person_id=person_id)
                subject_name = subject["name"]
            else:
                org_id = insert_organization(cur, draft["organization"], admin["id"])
                item_id = cur.execute(
                    """
                    INSERT INTO documents
                        (organization_id, title, source_file, content_hash, consent_given_by,
                         consent_recorded_at, consent_recorded_by_admin_id,
                         published_at, published_by_admin_id)
                    VALUES (%s, %s, %s, %s, %s, now(), %s, now(), %s)
                    RETURNING id
                    """,
                    (org_id, draft["title"], source_file, draft["content_hash"],
                     consent_given_by, admin["id"], admin["id"]),
                ).fetchone()["id"]
                store_chunks(cur, draft["chunks"], vectors, document_id=item_id, organization_id=org_id)
                subject_name = draft["organization"]["name"]
    except psycopg.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="That person or organization was added meanwhile; preview again")
    except DB_ERRORS:
        drafts[request.draft_id] = draft
        raise HTTPException(status_code=503, detail="Database unavailable")
    notify_directory_changed()
    return {"kind": draft["kind"], "id": item_id, "title": draft["title"],
            "subject": subject_name, "chunks": len(draft["chunks"])}

@router.get("/api/content")
def list_content():
    try:
        with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            # Wrapped so the union can be sorted by an expression: published
            # first, newest first.
            items = cur.execute(
                f"""
                SELECT * FROM (
                SELECT 'interview' AS kind, i.id, i.title, p.name AS subject,
                       i.interviewed_on, i.video_url, i.consent_given_by, i.consent_recorded_at,
                       coalesce(i.published_at, i.created_at) AS published_at,
                       pa.name AS published_by, i.unpublished_at, ua.name AS unpublished_by,
                       (SELECT count(*) FROM chunks c WHERE c.interview_id = i.id) AS chunks,
                       i.source_file NOT LIKE '{UPLOAD_SOURCE}%' AS from_script
                FROM interviews i
                JOIN people p ON p.id = i.person_id
                LEFT JOIN admins pa ON pa.id = i.published_by_admin_id
                LEFT JOIN admins ua ON ua.id = i.unpublished_by_admin_id
                UNION ALL
                SELECT 'document', d.id, d.title, o.name, NULL, NULL, d.consent_given_by,
                       d.consent_recorded_at, d.published_at, pa.name, d.unpublished_at, ua.name,
                       (SELECT count(*) FROM chunks c WHERE c.document_id = d.id), false
                FROM documents d
                JOIN organizations o ON o.id = d.organization_id
                LEFT JOIN admins pa ON pa.id = d.published_by_admin_id
                LEFT JOIN admins ua ON ua.id = d.unpublished_by_admin_id
                ) items
                ORDER BY unpublished_at IS NOT NULL, published_at DESC
                """
            ).fetchall()
    except DB_ERRORS:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {"items": items}

class Unpublish(BaseModel):
    kind: Literal["interview", "document"]
    id: int

@router.post("/api/content/unpublish")
def unpublish_content(request: Unpublish, admin: dict = Depends(require_admin_page)):
    table, chunk_key = (("interviews", "interview_id") if request.kind == "interview"
                        else ("documents", "document_id"))
    hidden_person = False
    try:
        with pool.connection() as conn, conn.transaction(), \
                conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                f"SELECT id, title, unpublished_at FROM {table} WHERE id = %s FOR UPDATE",
                (request.id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Not found")
            if row["unpublished_at"] is not None:
                raise HTTPException(status_code=409, detail="Already unpublished")
            removed = cur.execute(
                f"DELETE FROM chunks WHERE {chunk_key} = %s", (request.id,)
            ).rowcount
            cur.execute(
                f"UPDATE {table} SET unpublished_at = now(), unpublished_by_admin_id = %s "
                "WHERE id = %s",
                (admin["id"], request.id),
            )
            if request.kind == "interview":
                # A person an admin added appears in the chatbot only through
                # their interviews; with none left published, hide them.
                # People from the seed data are never hidden.
                hidden_person = cur.execute(
                    """
                    UPDATE people p SET chat_hidden_at = now()
                    FROM interviews i
                    WHERE i.id = %s AND p.id = i.person_id
                      AND p.created_by_admin_id IS NOT NULL AND p.chat_hidden_at IS NULL
                      AND NOT EXISTS (SELECT 1 FROM interviews other
                                      WHERE other.person_id = p.id AND other.id <> i.id
                                        AND other.unpublished_at IS NULL)
                    RETURNING p.id
                    """,
                    (request.id,),
                ).fetchone() is not None
    except DB_ERRORS:
        raise HTTPException(status_code=503, detail="Database unavailable")
    notify_directory_changed()
    return {"title": row["title"], "chunks_removed": removed, "hidden_person": hidden_person}
