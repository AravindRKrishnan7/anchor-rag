import hashlib
import io
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import psycopg
import pypdf
from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, RedirectResponse
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.auth import LoginRequired, require_admin, require_admin_page
from app.auth import router as auth_router
from app.db import DB_ERRORS, pool
from app.embeddings import EMBED_MODEL, embed_query, to_pgvector

# Seat counts older than these are flagged so clients can warn users.
SEATS_FRESH_FOR = timedelta(days=1)
SEATS_STALE_AFTER = timedelta(days=7)

@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open(wait=False)
    try:
        get_chat_profiles()
    except DB_ERRORS:
        pass  # loaded on the first chat instead
    yield
    pool.close()

app = FastAPI(lifespan=lifespan)
app.include_router(auth_router)

# Everything under /admin needs a logged-in admin, so routes added to this
# router later are protected too. It is included at the end of this file.
admin_router = APIRouter(
    prefix="/admin", dependencies=[Depends(require_admin_page)], include_in_schema=False
)

@app.exception_handler(LoginRequired)
def redirect_to_login(request: Request, exc: LoginRequired):
    return RedirectResponse(f"/admin/login?next={quote(exc.next_path, safe='')}", status_code=303)

STATIC = Path(__file__).parent / "static"

# Pages are read per request so edits show up on refresh.
@app.get("/", include_in_schema=False)
def home():
    return FileResponse(STATIC / "home.html")

# Public chat page; it calls POST /chat.
@app.get("/ask", include_in_schema=False)
def ask_page():
    return FileResponse(STATIC / "ask.html")

@admin_router.get("")
def dashboard():
    return FileResponse(STATIC / "admin.html")

@admin_router.get("/upload")
def upload_page():
    return FileResponse(STATIC / "upload.html")

@admin_router.get("/changes")
def changes_page():
    return FileResponse(STATIC / "changes.html")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/db-health")
def db_health():
    try:
        with pool.connection() as conn:
            postgres = conn.execute("SELECT version()").fetchone()[0]
            row = conn.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()
    except DB_ERRORS:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {"status": "ok", "postgres": postgres, "pgvector": row[0] if row else None}

def fetch_person(person_id: int) -> dict | None:
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        person = cur.execute(
            "SELECT id, name, role, services, location, bio FROM people WHERE id = %s",
            (person_id,),
        ).fetchone()
        if person is not None:
            # status spells out what a NULL end_date means; models misread it.
            person["employment"] = cur.execute(
                """
                SELECT e.title,
                       CASE WHEN e.end_date IS NULL THEN 'current' ELSE 'former' END AS status,
                       e.start_date, e.end_date,
                       o.id AS organization_id, o.name AS organization, o.org_type
                FROM employment e
                JOIN organizations o ON o.id = e.organization_id
                WHERE e.person_id = %s
                ORDER BY e.end_date IS NOT NULL, e.start_date DESC
                """,
                (person_id,),
            ).fetchall()
    return person

@app.get("/people/{person_id}")
def get_person(person_id: int):
    try:
        person = fetch_person(person_id)
    except DB_ERRORS:
        raise HTTPException(status_code=503, detail="Database unavailable")
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return person

def seat_freshness(age: timedelta | None) -> str:
    if age is None:
        return "unknown"
    if age <= SEATS_FRESH_FOR:
        return "fresh"
    if age <= SEATS_STALE_AFTER:
        return "aging"
    return "stale"

# Seats show only the count, when it was reported and how fresh it is. Who
# reported it stays in seat_availability for admins and never reaches the
# chat model or the public API.
def fetch_course(course_id: int) -> dict | None:
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        course = cur.execute(
            """
            SELECT c.id, c.code, c.name, c.duration_days, c.fee_inr, c.description,
                   o.id AS organization_id, o.name AS institute
            FROM courses c
            JOIN organizations o ON o.id = c.organization_id
            WHERE c.id = %s
            """,
            (course_id,),
        ).fetchone()
        if course is not None:
            batches = cur.execute(
                """
                SELECT b.id, b.start_date, b.end_date, b.capacity, b.location,
                       s.seats_available, s.reported_at,
                       now() - s.reported_at AS age
                FROM course_batches b
                LEFT JOIN LATERAL (
                    SELECT seats_available, reported_at
                    FROM seat_availability
                    WHERE batch_id = b.id
                    ORDER BY reported_at DESC
                    LIMIT 1
                ) s ON true
                WHERE b.course_id = %s
                ORDER BY b.start_date
                """,
                (course_id,),
            ).fetchall()
    if course is None:
        return None
    course["batches"] = [
        {
            "id": b["id"],
            "start_date": b["start_date"],
            "end_date": b["end_date"],
            "capacity": b["capacity"],
            "location": b["location"],
            "seats": {
                "available": b["seats_available"],
                "updated_at": b["reported_at"],
                "age_hours": (
                    round(b["age"].total_seconds() / 3600, 1) if b["age"] is not None else None
                ),
                "freshness": seat_freshness(b["age"]),
            },
        }
        for b in batches
    ]
    return course

@app.get("/courses/{course_id}")
def get_course(course_id: int):
    try:
        course = fetch_course(course_id)
    except DB_ERRORS:
        raise HTTPException(status_code=503, detail="Database unavailable")
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found")
    return course

class SeatReport(BaseModel):
    seats_available: int = Field(ge=0)
    source: Literal["manual", "form", "sync"]
    reported_by_person_id: int | None = None

@app.post("/batches/{batch_id}/seat-reports", status_code=201)
def create_seat_report(batch_id: int, report: SeatReport,
                       admin: dict = Depends(require_admin)):
    try:
        with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            # FOR SHARE stops capacity changing between the check and the insert.
            batch = cur.execute(
                "SELECT capacity FROM course_batches WHERE id = %s FOR SHARE",
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise HTTPException(status_code=404, detail="Batch not found")
            if report.seats_available > batch["capacity"]:
                raise HTTPException(
                    status_code=422,
                    detail=f"seats_available ({report.seats_available}) exceeds "
                    f"batch capacity ({batch['capacity']})",
                )
            if report.reported_by_person_id is not None and cur.execute(
                "SELECT 1 FROM people WHERE id = %s", (report.reported_by_person_id,)
            ).fetchone() is None:
                raise HTTPException(status_code=422, detail="Reporting person not found")
            return cur.execute(
                """
                INSERT INTO seat_availability
                    (batch_id, seats_available, source, reported_by_person_id,
                     recorded_by_admin_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, batch_id, seats_available, source, reported_at,
                          reported_by_person_id, recorded_by_admin_id
                """,
                (batch_id, report.seats_available, report.source,
                 report.reported_by_person_id, admin["id"]),
            ).fetchone()
    except DB_ERRORS:
        raise HTTPException(status_code=503, detail="Database unavailable")

# Chat model, via DeepSeek's OpenAI-compatible API. Embeddings stay on Ollama.
DEEPSEEK_URL = os.getenv("DEEPSEEK_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
CHAT_TIMEOUT = 60  # seconds per model call
CHAT_MODEL_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError)
# Upper bound on model -> tool -> model round trips for one question.
CHAT_MAX_TOOL_ROUNDS = 5
# Earlier turns kept for follow-up questions (3 question/answer pairs), each
# cut to this many characters so long answers don't slow the model down.
CHAT_HISTORY_MESSAGES = 6
CHAT_HISTORY_CHARS = 2000

# Most matches a search tool returns.
CHAT_SEARCH_LIMIT = 5
# Candidates each of vector and full-text search feed into rank fusion, and
# the standard Reciprocal Rank Fusion constant (dampens the gap between ranks).
DOCUMENT_CANDIDATES = 20
RRF_K = 60

CHAT_SYSTEM_PROMPT = """You are the assistant for ANchor, a directory of maritime \
professionals and training courses. Answer only from the profiles below and from \
tool results; never invent people, courses, dates or seat numbers. If neither has \
the answer, say exactly: I don't have that information.

The profiles list everyone in the directory with their jobs, each marked \
"current" (they still hold it) or "former" (it has ended). For courses, seats \
and what people said in interviews, use the tools. When the user gives no ID, \
search first with a few short keywords (a name, role, service, course name or \
code), then look up details by the ID you found. The directory records which institute offers each course, \
not who teaches it. Seat counts include how fresh they are: say so when one is \
"aging" or "stale", and say seats are unknown when there is no report. \
For what people have said about their work, experience or advice, use \
search_documents and attribute each point to the person who said it. \
Give the answer itself, not where it came from: don't mention the profiles, \
searches, tools or databases, and don't comment on whether a search was useful. \
Today is {today}.

Profiles:
{profiles}"""

# Rules the model doesn't follow reliably are enforced on its answer instead:
# quotes must appear in this turn's search_documents results, and database
# IDs are removed.
# A quote is "..." or curly-quoted text of at least this many words (shorter
# ones are usually terms like "aging"), or a Markdown block quote line.
QUOTE_MIN_WORDS = 3
INLINE_QUOTE = re.compile(r'"([^"\n]+)"|“([^”\n]+)”')
BLOCK_QUOTE = re.compile(r"^(>\s?)(.*)$", re.M)
UNVERIFIED_QUOTE = "[quote removed: not found in the interview transcripts]"
# "(ID 1)", "(person ID: 1)", then any remaining "course ID 456".
ID_IN_PARENS = re.compile(r"\s*\((?:person |course |batch )?IDs? ?:? ?#?\d+\)", re.I)
ID_MENTION = re.compile(r"\s*\b(?:person |course |batch )?IDs? ?:? ?#?\d+\b", re.I)
# Characters of the previous exchange used to search transcripts on a follow-up.
CHAT_FOLLOW_UP_CONTEXT_CHARS = 500

CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_people",
            "description": "Find maritime professionals by name, role or service "
            f"(e.g. 'chief engineer', 'hull inspection'). Returns up to {CHAT_SEARCH_LIMIT} "
            "best matches with IDs; call get_person for full details.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search keywords"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_courses",
            "description": "Find training courses by name or code (e.g. 'fire fighting', "
            f"'ECDIS'). Returns up to {CHAT_SEARCH_LIMIT} best matches with IDs and the "
            "institute; call get_course_batches for batches and seats.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search keywords"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": "Search interview transcripts for what maritime professionals "
            "said about a topic: experiences, opinions, advice. Matches by meaning, so "
            "describe the topic in a short phrase or question. Returns up to "
            f"{CHAT_SEARCH_LIMIT} passages with the speaker's name and interview title.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Topic or question"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_person",
            "description": "Look up a maritime professional by ID: role, services, "
            "location, bio and employment history.",
            "parameters": {
                "type": "object",
                "properties": {"person_id": {"type": "integer", "description": "Person ID"}},
                "required": ["person_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_course_batches",
            "description": "Look up a training course by ID: institute, fee, duration "
            "and its batches with dates, capacity, latest seat count, when it was "
            "reported and how fresh it is.",
            "parameters": {
                "type": "object",
                "properties": {"course_id": {"type": "integer", "description": "Course ID"}},
                "required": ["course_id"],
            },
        },
    },
]

# Matches rows containing any of the query's words (plainto_tsquery joins them
# with AND, which is too strict for model-written queries); ts_rank puts rows
# matching more of them first.
SEARCH_ANY_WORD = "CAST(replace(plainto_tsquery('english', %s)::text, '&', '|') AS tsquery)"

def search_people(query: str) -> dict:
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        matches = cur.execute(
            f"""
            SELECT p.id, p.name, p.role, p.services, p.location
            FROM people p, {SEARCH_ANY_WORD} q
            WHERE p.search @@ q
            ORDER BY ts_rank(p.search, q) DESC, p.id
            LIMIT %s
            """,
            (query, CHAT_SEARCH_LIMIT),
        ).fetchall()
    return {"matches": matches}

def search_courses(query: str) -> dict:
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        matches = cur.execute(
            f"""
            SELECT c.id, c.code, c.name, o.id AS organization_id, o.name AS institute
            FROM courses c
            JOIN organizations o ON o.id = c.organization_id,
                 {SEARCH_ANY_WORD} q
            WHERE c.search @@ q
            ORDER BY ts_rank(c.search, q) DESC, c.id
            LIMIT %s
            """,
            (query, CHAT_SEARCH_LIMIT),
        ).fetchall()
    return {"matches": matches}

# Hybrid search: vector similarity finds passages with the same meaning,
# full-text finds exact terms (codes, names). Reciprocal Rank Fusion scores
# each passage by 1/(RRF_K + rank) in each list, so ones both agree on win.
# Full-text requires every word here (unlike SEARCH_ANY_WORD): in long
# passages, matching any one word of a question ("vessel", "watch") is noise
# that would outvote the vector ranking.
def search_documents(query: str) -> dict:
    vector = to_pgvector(embed_query(query))
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        matches = cur.execute(
            """
            WITH vec AS (
                SELECT id, row_number() OVER (ORDER BY embedding <=> %(vector)s::vector) AS rank
                FROM chunks
                WHERE embedding_model = %(model)s
                ORDER BY embedding <=> %(vector)s::vector
                LIMIT %(candidates)s
            ),
            fts AS (
                SELECT c.id, row_number() OVER (ORDER BY ts_rank(c.search, q) DESC) AS rank
                FROM chunks c, websearch_to_tsquery('english', %(query)s) q
                WHERE c.search @@ q
                ORDER BY ts_rank(c.search, q) DESC
                LIMIT %(candidates)s
            ),
            fused AS (
                SELECT coalesce(vec.id, fts.id) AS id,
                       coalesce(1.0 / (%(k)s + vec.rank), 0)
                           + coalesce(1.0 / (%(k)s + fts.rank), 0) AS score
                FROM vec FULL OUTER JOIN fts ON fts.id = vec.id
            )
            SELECT c.id AS chunk_id, p.id AS person_id, p.name AS person_name,
                   i.title AS interview_title, i.interviewed_on, c.content AS text,
                   round(f.score, 4) AS score
            FROM fused f
            JOIN chunks c ON c.id = f.id
            JOIN people p ON p.id = c.person_id
            JOIN interviews i ON i.id = c.interview_id
            ORDER BY f.score DESC, c.id
            LIMIT %(limit)s
            """,
            {"vector": vector, "model": EMBED_MODEL, "query": query,
             "candidates": DOCUMENT_CANDIDATES, "k": RRF_K, "limit": CHAT_SEARCH_LIMIT},
        ).fetchall()
    return {"matches": matches}

# Tool name -> (query function, argument name, argument type, not-found message).
CHAT_TOOL_QUERIES = {
    "get_person": (fetch_person, "person_id", int, "Person not found"),
    "get_course_batches": (fetch_course, "course_id", int, "Course not found"),
    "search_people": (search_people, "query", str, None),
    "search_courses": (search_courses, "query", str, None),
    "search_documents": (search_documents, "query", str, None),
}

def run_chat_tool(name: str, arguments: dict | None) -> dict:
    # Bad calls go back to the model as errors so it can retry or explain.
    if name not in CHAT_TOOL_QUERIES:
        return {"error": f"Unknown tool: {name}"}
    if not isinstance(arguments, dict):
        return {"error": "Arguments must be a JSON object"}
    fetch, arg, arg_type, not_found = CHAT_TOOL_QUERIES[name]
    value = arguments.get(arg)
    try:
        key = int(value) if arg_type is int else value.strip()
    except (TypeError, ValueError, AttributeError):
        key = None
    if key is None or key == "":
        kind = "an integer" if arg_type is int else "a non-empty string"
        return {"error": f"{name} needs {kind} {arg}"}
    result = fetch(key)
    return result if result is not None else {"error": not_found}

def deepseek_chat(
    messages: list[dict], tools: list[dict] | None = CHAT_TOOLS,
    response_format: dict | None = None, temperature: float | None = None,
) -> dict:
    body = {"model": DEEPSEEK_MODEL, "messages": messages}
    if temperature is not None:
        body["temperature"] = temperature
    if tools:
        body["tools"] = tools
    if response_format:
        body["response_format"] = response_format
    request = urllib.request.Request(
        f"{DEEPSEEK_URL}/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        },
    )
    with urllib.request.urlopen(request, timeout=CHAT_TIMEOUT) as response:
        return json.load(response)["choices"][0]["message"]

# Everyone in the directory with their jobs, for the chat system prompt.
# Loaded at startup, or on the first chat if the database was down then.
# IDs are left out so the model has none to repeat.
chat_profiles: str | None = None

def load_chat_profiles() -> str:
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            """
            SELECT p.id, p.name, p.role, p.services, p.location, p.bio,
                   e.title, e.start_date, e.end_date,
                   o.name AS organization, o.org_type
            FROM people p
            LEFT JOIN employment e ON e.person_id = p.id
            LEFT JOIN organizations o ON o.id = e.organization_id
            ORDER BY p.id, e.end_date IS NOT NULL, e.start_date DESC
            """
        ).fetchall()
    people = {}
    for row in rows:
        person = people.setdefault(row["id"], {**row, "jobs": []})
        if row["title"] is not None:
            # "current"/"former" spelled out: models read "since 2022" as ended.
            when = (
                f"former, {row['start_date'].year}-{row['end_date'].year}"
                if row["end_date"]
                else f"current, since {row['start_date'].year}"
            )
            person["jobs"].append(
                f"{row['title']} at {row['organization']} ({row['org_type']}) ({when})"
            )
    lines = []
    for p in people.values():
        line = f"- {p['name']}, {p['role']}"
        if p["location"]:
            line += f", {p['location']}"
        line += "."
        if p["services"]:
            line += f" Services: {', '.join(p['services'])}."
        if p["bio"]:
            line += f" {p['bio']}"
        if p["jobs"]:
            line += f" Jobs: {'; '.join(p['jobs'])}."
        lines.append(line)
    return "\n".join(lines)

def get_chat_profiles() -> str:
    global chat_profiles
    if chat_profiles is None:
        chat_profiles = load_chat_profiles()
    return chat_profiles

def normalize_quote(text: str) -> str:
    text = text.translate({0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"'})
    return re.sub(r"\s+", " ", text).strip(" .,;:!?\"'*_").lower()

def quote_found(quote: str, sources: list[str]) -> bool:
    # A quote shortened with "..." must match piece by piece.
    pieces = [normalize_quote(p) for p in re.split(r"\.\.\.|…", quote)]
    return all(any(piece in source for source in sources) for piece in pieces if piece)

def is_quote(text: str) -> bool:
    return len(text.split()) >= QUOTE_MIN_WORDS

# Replaces quotes that aren't in sources (passages and interview titles from
# this turn's search_documents calls). Returns the answer and removed quotes.
def remove_unverified_quotes(answer: str, sources: list[str]) -> tuple[str, list[str]]:
    sources = [normalize_quote(s) for s in sources]
    removed = []

    def check_block(match: re.Match) -> str:
        line = match[2].strip()
        # Leave attribution lines ("— Ravi Menon") and lines with inline
        # quotes, which the inline check handles.
        if not is_quote(line) or line[0] in "—–-" or INLINE_QUOTE.search(line):
            return match[0]
        if quote_found(line, sources):
            return match[0]
        removed.append(line)
        return match[1] + UNVERIFIED_QUOTE

    def check_inline(match: re.Match) -> str:
        quote = match[1] or match[2]
        if not is_quote(quote) or quote_found(quote, sources):
            return match[0]
        removed.append(quote)
        return UNVERIFIED_QUOTE

    answer = BLOCK_QUOTE.sub(check_block, answer)
    answer = INLINE_QUOTE.sub(check_inline, answer)
    return answer, removed

def remove_ids(answer: str) -> str:
    return ID_MENTION.sub("", ID_IN_PARENS.sub("", answer))

# Sentences about how the answer was found ("That search wasn't useful"),
# which the prompt forbids but the model still writes, mostly after the
# automatic follow-up search. Narrow on purpose: "search and rescue" and a
# surveyor's "a search of the hull" are real topics.
META_SENTENCE = re.compile(
    r"\b(?:the|that|this|these|those|my|another|automatic|earlier|previous|initial"
    r"|transcript|interview|document|course|people)\s+search(?:es)?\b(?!\s+and\s+rescue)"
    r"|\bsearch(?:es)?\s+(?:results?|returned|found|turned\s+up|came\s+back|did\s+not"
    r"|didn't|wasn't|was\s+not|shows?|showed)\b"
    r"|\bI\s+(?:searched|looked\s+up|queried|ran\s+a\s+search|called|checked\s+the\s+(?:database|tools?|records))\b"
    r"|\b(?:tool|function)\s+(?:calls?|results?)\b|\bsearch\s+tool\b"
    r"|\b(?:in|from|according\s+to)\s+the\s+database\b"
    r"|\bthe\s+profiles?\s+(?:provided|below|above|given|I\s+have|you\s+(?:gave|shared))\b"
    r"|\b(?:from|in|according\s+to|based\s+on)\s+the\s+profiles?\b"
    r"|\bI\s+only\s+have\s+(?:the|these)\b"
    r"|\bI\s+can\s+(?:answer|tell\s+you|help\s+with)\s+(?:that|this)\s+from\b"
    r"|\b(?:search_(?:people|courses|documents)|get_person|get_course_batches)\b",
    re.I,
)
# "According to the database, the fee is ..." keeps the fact after the comma.
META_LEAD_IN = re.compile(
    r"^(?:according\s+to|based\s+on|from|per)\s+(?:the|my)\s+(?:database|records"
    r"|profiles?|directory|search(?:es)?(?:\s+results?)?|tool\s+results?|transcript\s+search)"
    r"\b[^,]*,\s*",
    re.I,
)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=\S)")
LIST_PREFIX = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)")
NO_ANSWER = "I don't have that information."

# A sentence without its talk of searching: unchanged if there is none, the
# fact after a lead-in, ", but" or ":", or "" when nothing is left.
def meta_free_part(sentence: str) -> str:
    if not META_SENTENCE.search(sentence) and not META_LEAD_IN.match(sentence):
        return sentence
    rest = META_LEAD_IN.sub("", sentence, count=1)
    if META_SENTENCE.search(rest):
        for joint in (", but ", ": "):
            _, found, after = rest.partition(joint)
            if found and not META_SENTENCE.search(after):
                rest = after
                break
        else:
            rest = ""
    return rest[:1].upper() + rest[1:]

# Drops those sentences. Table rows and block quotes (checked transcript
# text) are left alone. Returns the answer and the removed sentences.
def remove_meta_sentences(answer: str) -> tuple[str, list[str]]:
    removed = []
    lines = []
    for line in answer.split("\n"):
        stripped = line.strip()
        if not stripped or stripped[0] in "|>":
            lines.append(line)
            continue
        prefix_match = LIST_PREFIX.match(line)
        prefix = prefix_match[1] if prefix_match else ""
        kept = []
        changed = False
        for sentence in SENTENCE_END.split(line[len(prefix):]):
            rest = meta_free_part(sentence)
            if rest != sentence:
                removed.append(sentence)
                changed = True
            if rest:
                kept.append(rest)
        if not changed:
            lines.append(line)
        elif kept:
            lines.append(prefix + " ".join(kept))
    # Collapse blank runs left where paragraphs were removed.
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return (cleaned or NO_ANSWER), removed

# Only user and assistant turns, so a client can't inject system or tool messages.
class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str

class ChatRequest(BaseModel):
    question: str = Field(min_length=1)
    history: list[ChatTurn] = []

@app.post("/chat")
def chat(request: ChatRequest):
    if not DEEPSEEK_API_KEY:
        raise HTTPException(status_code=503, detail="Language model not configured")
    try:
        profiles = get_chat_profiles()
    except DB_ERRORS:
        raise HTTPException(status_code=503, detail="Database unavailable")
    system_prompt = CHAT_SYSTEM_PROMPT.format(today=date.today(), profiles=profiles)
    messages = [
        {"role": "system", "content": system_prompt},
        *(
            {"role": turn.role, "content": turn.content[:CHAT_HISTORY_CHARS]}
            for turn in request.history[-CHAT_HISTORY_MESSAGES:]
        ),
        {"role": "user", "content": request.question},
    ]
    tool_calls = []
    quote_sources = []

    def call_tool(call_id: str, name: str, arguments: dict | None, automatic: bool = False):
        result = run_chat_tool(name, arguments)
        tool_calls.append({"name": name, "arguments": arguments, "automatic": automatic})
        if name == "search_documents":
            for match in result.get("matches", []):
                quote_sources.extend([match["text"], match["interview_title"]])
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(jsonable_encoder(result)),
            }
        )

    try:
        if request.history:
            # A follow-up often asks about something an earlier answer
            # described, which the model would retell from memory. Searching
            # the transcripts for the last exchange plus the new question gives
            # it the real passages, as if it had called the tool itself.
            context = " ".join(
                turn.content[:CHAT_FOLLOW_UP_CONTEXT_CHARS] for turn in request.history[-2:]
            )
            arguments = {"query": f"{context} {request.question}"}
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "auto-search",
                            "type": "function",
                            "function": {
                                "name": "search_documents",
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                }
            )
            call_tool("auto-search", "search_documents", arguments, automatic=True)
        for _ in range(CHAT_MAX_TOOL_ROUNDS + 1):
            reply = deepseek_chat(messages)
            # content is null when the model calls tools.
            content = reply.get("content") or ""
            if not reply.get("tool_calls"):
                answer, removed = remove_unverified_quotes(content, quote_sources)
                answer, removed_meta = remove_meta_sentences(remove_ids(answer))
                return {
                    "answer": answer,
                    "tool_calls": tool_calls,
                    "removed_quotes": removed,
                    "removed_meta": removed_meta,
                }
            messages.append(
                {"role": "assistant", "content": content, "tool_calls": reply["tool_calls"]}
            )
            # Arguments arrive as a JSON string; bad JSON goes back as an error.
            for call in reply["tool_calls"]:
                try:
                    arguments = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = None
                call_tool(call["id"], call["function"]["name"], arguments)
    except DB_ERRORS:
        raise HTTPException(status_code=503, detail="Database unavailable")
    except urllib.error.HTTPError as e:
        # Bad key, rate limit or a DeepSeek outage.
        raise HTTPException(status_code=502, detail=f"Language model error (HTTP {e.code})")
    except CHAT_MODEL_ERRORS:
        raise HTTPException(status_code=503, detail="Language model unavailable")
    raise HTTPException(
        status_code=502,
        detail=f"Model was still calling tools after {CHAT_MAX_TOOL_ROUNDS} rounds",
    )

# Course updates from PDFs: the model extracts course fields into a draft,
# an admin compares it with the current course and approves, and only then
# is anything saved.
PDF_MAX_BYTES = 5 * 1024 * 1024
# Characters of PDF text sent to the model; course brochures are short.
PDF_MAX_CHARS = 20000
# Drafts live in memory until approved, so a restart discards them.
DRAFT_TTL = timedelta(hours=1)
# Course columns a draft can change. The institute is matched, never changed.
DRAFT_FIELDS = ("name", "code", "duration_days", "fee_inr")

EXTRACT_PROMPT = """Extract training course details from the document text the user \
sends. Reply with only a JSON object with these keys:
- "name": course name
- "code": course code (e.g. "BST")
- "institute": name of the institute offering the course
- "duration_days": length of the course in days, as an integer
- "fee_inr": course fee in Indian rupees, as an integer without symbols or commas
- "batches": list of {"start_date", "end_date", "capacity", "seats_available", \
"location"}, dates as YYYY-MM-DD
  - "capacity": total seats in the batch, only when labelled as capacity, intake, \
total seats or batch size
  - "seats_available": seats still open, when labelled as seats available, seats \
left, vacancies or similar
  A number under "Seats available" is seats_available, never capacity.
Use null for anything the document does not state and [] when it lists no batches. \
Never guess a value: a fee in another currency is null, a batch without both a start \
and an end date is left out, and capacity and seats_available are null unless that \
exact count is written."""

def empty_to_none(value):
    if isinstance(value, str):
        value = value.strip()
    return value if value != "" else None

class ExtractedBatch(BaseModel):
    start_date: date
    end_date: date
    capacity: int | None = Field(default=None, gt=0)
    seats_available: int | None = Field(default=None, ge=0)
    location: str | None = None

    @field_validator("capacity", "seats_available", "location", mode="before")
    @classmethod
    def blank_is_none(cls, value):
        return empty_to_none(value)

    @model_validator(mode="after")
    def check_dates(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date is before start_date")
        return self

class ExtractedCourse(BaseModel):
    name: str | None = None
    code: str | None = None
    institute: str | None = None
    duration_days: int | None = Field(default=None, gt=0)
    fee_inr: int | None = Field(default=None, ge=0)
    batches: list[ExtractedBatch] = []

    @field_validator("name", "code", "institute", "duration_days", "fee_inr", mode="before")
    @classmethod
    def blank_is_none(cls, value):
        return empty_to_none(value)

    @field_validator("batches", mode="before")
    @classmethod
    def null_batches(cls, value):
        return [] if value is None else value

def pdf_text(data: bytes) -> str:
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    except pypdf.errors.PyPdfError:
        raise HTTPException(status_code=422, detail="Could not read this PDF")

def extract_course(text: str) -> ExtractedCourse:
    reply = deepseek_chat(
        [
            {"role": "system", "content": EXTRACT_PROMPT},
            {"role": "user", "content": text[:PDF_MAX_CHARS]},
        ],
        tools=None,
        response_format={"type": "json_object"},
        temperature=0,  # same PDF, same draft
    )
    try:
        return ExtractedCourse.model_validate(json.loads(reply.get("content") or ""))
    except (json.JSONDecodeError, ValidationError) as e:
        raise HTTPException(status_code=502, detail=f"Model returned invalid course data: {e}")

# The model is told not to invent values; like quotes in chat, that is
# checked here. Text values must appear in the PDF, and numbers must appear
# as a whole number (with "19,500" read as 19500).
def ground_course(course: ExtractedCourse, text: str) -> tuple[ExtractedCourse, list[str]]:
    plain = re.sub(r"\s+", " ", text).lower()
    # Digit-group commas, including Indian grouping ("1,19,500").
    numbers = re.sub(r"(?<=\d),(?=\d{2,3}\b)", "", plain)

    def has_text(value: str) -> bool:
        return re.sub(r"\s+", " ", value).lower() in plain

    def has_number(value: int) -> bool:
        return re.search(rf"(?<!\d){value}(?!\d)", numbers) is not None

    warnings = []
    for field in ("name", "code", "institute"):
        value = getattr(course, field)
        if value is not None and not has_text(value):
            warnings.append(f"Ignored {field} {value!r}: not found in the PDF")
            setattr(course, field, None)
    for field in ("duration_days", "fee_inr"):
        value = getattr(course, field)
        if value is not None and not has_number(value):
            warnings.append(f"Ignored {field} {value}: not found in the PDF")
            setattr(course, field, None)
    batches = []
    for b in course.batches:
        days = [b.start_date.day, b.start_date.year, b.end_date.day, b.end_date.year]
        if not all(has_number(n) for n in days):
            warnings.append(f"Ignored batch {b.start_date} to {b.end_date}: dates not found in the PDF")
            continue
        for field in ("capacity", "seats_available"):
            value = getattr(b, field)
            if value is not None and not has_number(value):
                warnings.append(
                    f"Ignored {field} {value} for batch {b.start_date}: not found in the PDF"
                )
                setattr(b, field, None)
        if b.capacity is not None and b.seats_available is not None \
                and b.seats_available > b.capacity:
            warnings.append(
                f"Ignored seats_available {b.seats_available} for batch {b.start_date}: "
                f"more than its capacity {b.capacity}"
            )
            b.seats_available = None
        if b.location is not None and not has_text(b.location):
            b.location = None
        batches.append(b)
    course.batches = batches
    return course, warnings

COURSE_SNAPSHOT = """
    SELECT c.id, c.organization_id, c.name, c.code, c.duration_days, c.fee_inr,
           o.name AS institute
    FROM courses c
    JOIN organizations o ON o.id = c.organization_id
"""

# Names at least this similar (difflib ratio, 0-1) count as a likely match.
LIKELY_NAME_RATIO = 0.75
# Shortest code that counts in a prefix match ("AFF" for "AFF-01").
LIKELY_CODE_MIN = 3

def normalize_name(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", name.lower()).split())

def compact_code(code: str) -> str:
    return re.sub(r"[^a-z0-9]", "", code.lower())

# Why a course might be the one a PDF describes without matching exactly,
# or None. "AFF-01" is close to "AFF"; "Advanced Fire Fighting (STCW)"
# contains "Advanced Fire Fighting".
def likely_match_reason(course: ExtractedCourse, row: dict) -> str | None:
    if course.code and row["code"]:
        a, b = compact_code(course.code), compact_code(row["code"])
        shorter = min(a, b, key=len)
        if len(shorter) >= LIKELY_CODE_MIN and (a.startswith(b) or b.startswith(a)):
            return f"Code {row['code']} is similar to {course.code}"
    if course.name:
        a, b = normalize_name(course.name), normalize_name(row["name"])
        if a in b or b in a or SequenceMatcher(None, a, b).ratio() >= LIKELY_NAME_RATIO:
            return f"Name is similar to {course.name}"
    return None

# Finds the course a PDF describes: by code at the named institute, then by
# code alone if only one course has it, then by name the same way. Without
# an exact match, returns likely matches at the same institute instead, as
# (course, reason) pairs.
def match_course(cur, course: ExtractedCourse) -> tuple[dict | None, list[tuple], dict | None]:
    institute = None
    if course.institute:
        institute = cur.execute(
            "SELECT id, name FROM organizations WHERE lower(name) = lower(%s)",
            (course.institute,),
        ).fetchone()
    for column, value in (("code", course.code), ("name", course.name)):
        if value is None:
            continue
        rows = cur.execute(
            f"{COURSE_SNAPSHOT} WHERE lower(c.{column}) = lower(%s) ORDER BY c.id", (value,)
        ).fetchall()
        if institute is not None:
            rows = [r for r in rows if r["organization_id"] == institute["id"]]
        if len(rows) == 1:
            return rows[0], [], institute
    likely = []
    if institute is not None:
        rows = cur.execute(
            f"{COURSE_SNAPSHOT} WHERE c.organization_id = %s ORDER BY c.id", (institute["id"],)
        ).fetchall()
        for row in rows:
            reason = likely_match_reason(course, row)
            if reason:
                likely.append((row, reason))
    return None, likely, institute

# A course's batches by (start_date, end_date).
def existing_batches(cur, course_id: int) -> dict[tuple[date, date], dict]:
    rows = cur.execute(
        "SELECT id, start_date, end_date, capacity FROM course_batches WHERE course_id = %s",
        (course_id,),
    ).fetchall()
    return {(r["start_date"], r["end_date"]): r for r in rows}

drafts: dict[str, dict] = {}

# What approving a draft would do to one course (or to a new one when
# current is None). keep_identity leaves the name and code alone, for a
# likely match the PDF may name differently.
def build_option(course: ExtractedCourse, current: dict | None, keep_identity: bool,
                 institute: dict | None, batches_by_dates: dict) -> dict:
    fields = []
    for field in DRAFT_FIELDS:
        old = current[field] if current else None
        new = getattr(course, field)
        # Empty values from the PDF leave the course as it is.
        differs = new is not None and new != old
        kept = differs and keep_identity and field in ("name", "code")
        fields.append({"field": field, "current": old, "new": new,
                       "changed": differs and not kept, "kept": kept})
    batches = []
    for index, b in enumerate(course.batches):
        existing = batches_by_dates.get((b.start_date, b.end_date))
        capacity = existing["capacity"] if existing else b.capacity
        if existing:
            status = "exists"
        elif b.capacity is None:
            status = "needs_capacity"
        else:
            status = "new"
        # A new batch's capacity may still be entered, so it is checked on apply.
        seat_report = b.seats_available is not None and (
            capacity is None or b.seats_available <= capacity
        )
        batches.append({**b.model_dump(), "index": index, "status": status,
                        "existing_capacity": existing["capacity"] if existing else None,
                        "seat_report": seat_report})
    blocked = None
    if current is None and (course.name is None or institute is None):
        blocked = "A new course needs a course name and an institute already in the directory"
    elif not any(f["changed"] for f in fields) and not any(
        b["status"] != "exists" or b["seat_report"] for b in batches
    ):
        blocked = "Nothing to change: the course already matches this PDF"
    return {
        "target": current["id"] if current else "new",
        "course": {"name": current["name"], "code": current["code"],
                   "institute": current["institute"]} if current else None,
        "fields": fields,
        "batches": batches,
        "can_apply": blocked is None,
        "blocked_reason": blocked,
    }

@app.post("/courses/upload-pdf")
def upload_course_pdf(file: UploadFile = File(...), admin: dict = Depends(require_admin)):
    if not DEEPSEEK_API_KEY:
        raise HTTPException(status_code=503, detail="Language model not configured")
    data = file.file.read(PDF_MAX_BYTES + 1)
    if len(data) > PDF_MAX_BYTES:
        raise HTTPException(status_code=413, detail="PDF is larger than 5 MB")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(status_code=422, detail="File is not a PDF")
    text = pdf_text(data)
    if not text:
        raise HTTPException(
            status_code=422, detail="PDF has no text to read (it may be a scanned image)"
        )
    try:
        course, warnings = ground_course(extract_course(text), text)
        with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            current, likely, institute = match_course(cur, course)
            # (course, keep_identity) for each choice; an exact match is the
            # only one, likely matches come with "create as new".
            if current:
                choices = [(current, False)]
            else:
                choices = [(row, True) for row, _ in likely] + [(None, False)]
            options = [
                (c, keep, build_option(
                    course, c, keep, institute,
                    existing_batches(cur, c["id"]) if c else {},
                ))
                for c, keep in choices
            ]
    except DB_ERRORS:
        raise HTTPException(status_code=503, detail="Database unavailable")
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Language model error (HTTP {e.code})")
    except CHAT_MODEL_ERRORS:
        raise HTTPException(status_code=503, detail="Language model unavailable")

    if course.institute and institute is None:
        warnings.append(f"Institute {course.institute!r} is not in the directory")
    elif current and institute and institute["id"] != current["organization_id"]:
        warnings.append("The PDF names a different institute; the course's institute is not changed")
    if any(b.seats_available is not None for b in course.batches):
        warnings.append(
            "Seat counts from the PDF are recorded as of approval, so the chatbot shows them "
            "as fresh; approve only if the PDF is current"
        )

    now = datetime.now(timezone.utc)
    for draft_id in [k for k, d in drafts.items() if now - d["extracted_at"] > DRAFT_TTL]:
        drafts.pop(draft_id, None)
    draft_id = uuid.uuid4().hex
    drafts[draft_id] = {
        "course": course,
        "institute": institute,
        "options": {
            option["target"]: {"current": c, "keep_identity": keep, "batches": option["batches"]}
            for c, keep, option in options
        },
        "requires_choice": bool(likely),
        "source_file": Path(file.filename or "upload.pdf").name,
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "extracted_at": now,
    }
    return {
        "draft_id": draft_id,
        "source_file": drafts[draft_id]["source_file"],
        "extracted_at": now,
        "match": "exact" if current else "likely" if likely else "none",
        "requires_choice": bool(likely),
        "pdf": {f: getattr(course, f) for f in ("name", "code", "institute")},
        "institute": institute["name"] if institute else None,
        "likely_matches": [
            {"target": row["id"], "name": row["name"], "code": row["code"], "reason": reason}
            for row, reason in likely
        ],
        "options": [option for _, _, option in options],
        "warnings": warnings,
    }

class ApplyDraft(BaseModel):
    draft_id: str
    # A course ID or "new"; required when the draft has likely matches.
    target: int | Literal["new"] | None = None
    # Capacities the admin entered, by batch index, for batches the PDF
    # gives no capacity for.
    capacities: dict[int, int] = {}

@app.post("/courses/apply-draft")
def apply_course_draft(request: ApplyDraft, admin: dict = Depends(require_admin)):
    draft = drafts.get(request.draft_id)
    if draft is None or datetime.now(timezone.utc) - draft["extracted_at"] > DRAFT_TTL:
        raise HTTPException(status_code=404, detail="Draft not found or expired; upload the PDF again")
    target = request.target
    if target is None:
        if draft["requires_choice"]:
            raise HTTPException(
                status_code=422,
                detail="This PDF may describe an existing course: choose to update it or "
                "create a new course",
            )
        (target,) = draft["options"]
    if target not in draft["options"]:
        raise HTTPException(status_code=422, detail="That course is not one of this draft's choices")
    option = draft["options"][target]
    course, institute = draft["course"], draft["institute"]
    current, keep_identity = option["current"], option["keep_identity"]
    for b in option["batches"]:
        if b["status"] != "needs_capacity":
            continue
        capacity = request.capacities.get(b["index"])
        when = f"the batch starting {b['start_date']}"
        if capacity is None:
            raise HTTPException(status_code=422, detail=f"Enter the capacity for {when}")
        if capacity <= 0:
            raise HTTPException(status_code=422, detail=f"Capacity for {when} must be above 0")
        if b["seats_available"] is not None and capacity < b["seats_available"]:
            raise HTTPException(
                status_code=422,
                detail=f"Capacity for {when} can't be less than its {b['seats_available']} "
                "seats available",
            )

    # Taken out now so the same draft can't be applied twice at once; put
    # back if the database is down so the admin can retry.
    if drafts.pop(request.draft_id, None) is None:
        raise HTTPException(status_code=404, detail="Draft not found or expired; upload the PDF again")
    try:
        with pool.connection() as conn, conn.transaction(), \
                conn.cursor(row_factory=dict_row) as cur:
            if current is not None:
                row = cur.execute(
                    f"{COURSE_SNAPSHOT} WHERE c.id = %s FOR UPDATE OF c", (current["id"],)
                ).fetchone()
                if row != current:
                    raise HTTPException(
                        status_code=409,
                        detail="The course changed after this PDF was read; upload it again",
                    )
                course_id = current["id"]
                fields = [f for f in DRAFT_FIELDS
                          if not (keep_identity and f in ("name", "code"))]
                changes = {
                    f: {"old": current[f], "new": getattr(course, f)}
                    for f in fields
                    if getattr(course, f) is not None and getattr(course, f) != current[f]
                }
                if changes:
                    assignments = ", ".join(f"{f} = %({f})s" for f in changes)
                    cur.execute(
                        f"UPDATE courses SET {assignments} WHERE id = %(id)s",
                        {**{f: c["new"] for f, c in changes.items()}, "id": course_id},
                    )
            else:
                if course.name is None or institute is None:
                    raise HTTPException(
                        status_code=422,
                        detail="A new course needs a name and an institute in the directory",
                    )
                course_id = cur.execute(
                    """
                    INSERT INTO courses (organization_id, code, name, duration_days, fee_inr)
                    VALUES (%s, %s, %s, %s, %s) RETURNING id
                    """,
                    (institute["id"], course.code, course.name,
                     course.duration_days, course.fee_inr),
                ).fetchone()["id"]
                changes = {
                    f: {"old": None, "new": getattr(course, f)}
                    for f in DRAFT_FIELDS if getattr(course, f) is not None
                }
            # Checked again inside the transaction in case a batch was added since.
            batches_by_dates = existing_batches(cur, course_id)
            added, seat_reports = [], []
            for index, b in enumerate(course.batches):
                existing = batches_by_dates.get((b.start_date, b.end_date))
                if existing:
                    batch_id, capacity = existing["id"], existing["capacity"]
                else:
                    capacity = b.capacity or request.capacities.get(index)
                    if capacity is None:
                        continue  # existed when the draft was made, deleted since
                    batch_id = cur.execute(
                        """
                        INSERT INTO course_batches
                            (course_id, start_date, end_date, capacity, location)
                        VALUES (%s, %s, %s, %s, %s) RETURNING id
                        """,
                        (course_id, b.start_date, b.end_date, capacity, b.location),
                    ).fetchone()["id"]
                    added.append({**b.model_dump(), "capacity": capacity,
                                  "capacity_entered": b.capacity is None})
                if b.seats_available is not None and b.seats_available <= capacity:
                    cur.execute(
                        """
                        INSERT INTO seat_availability
                            (batch_id, seats_available, source, recorded_by_admin_id)
                        VALUES (%s, %s, 'pdf', %s)
                        """,
                        (batch_id, b.seats_available, admin["id"]),
                    )
                    seat_reports.append({"start_date": b.start_date, "end_date": b.end_date,
                                         "seats_available": b.seats_available})
            if not changes and not added and not seat_reports:
                raise HTTPException(status_code=422, detail="Nothing to change")
            cur.execute(
                """
                INSERT INTO course_updates
                    (course_id, source_file, source_sha256, extracted_at, changes, admin_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (course_id, draft["source_file"], draft["source_sha256"], draft["extracted_at"],
                 Jsonb(jsonable_encoder({
                     "target": "new" if current is None else "existing",
                     "fields": changes, "batches_added": added, "seat_reports": seat_reports,
                 })), admin["id"]),
            )
    except psycopg.errors.UniqueViolation:
        raise HTTPException(
            status_code=409, detail="A course with this code already exists at the institute"
        )
    except DB_ERRORS:
        drafts[request.draft_id] = draft
        raise HTTPException(status_code=503, detail="Database unavailable")
    # Saved by now; a failed read-back must not restore the draft.
    try:
        saved = fetch_course(course_id)
    except DB_ERRORS:
        saved = None
    return {"course": saved, "changes": changes, "batches_added": added,
            "seat_reports": seat_reports}

# Recent changes for the admin changes page, newest first: PDF course
# updates, and seat reports not from a PDF (those are part of their update).
CHANGES_LIMIT = 100

@admin_router.get("/api/changes")
def list_changes():
    try:
        with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                """
                SELECT 'course_update' AS kind, u.applied_at AS at, a.name AS admin,
                       c.name AS course, c.code, u.source_file, u.changes,
                       NULL::date AS start_date, NULL::date AS end_date,
                       NULL::integer AS seats_available, NULL::text AS source
                FROM course_updates u
                JOIN courses c ON c.id = u.course_id
                LEFT JOIN admins a ON a.id = u.admin_id
                UNION ALL
                SELECT 'seat_report', s.reported_at, a.name, c.name, c.code, NULL, NULL,
                       b.start_date, b.end_date, s.seats_available, s.source
                FROM seat_availability s
                JOIN course_batches b ON b.id = s.batch_id
                JOIN courses c ON c.id = b.course_id
                LEFT JOIN admins a ON a.id = s.recorded_by_admin_id
                WHERE s.source <> 'pdf'
                ORDER BY at DESC
                LIMIT %s
                """,
                (CHANGES_LIMIT,),
            ).fetchall()
    except DB_ERRORS:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {"changes": rows}

app.include_router(admin_router)
