import json
import os
import re
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from psycopg.rows import dict_row
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, Field

from app.db import pool
from app.embeddings import EMBED_MODEL, embed_query, to_pgvector

DB_ERRORS = (psycopg.Error, PoolTimeout)

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

INDEX_HTML = Path(__file__).parent / "static" / "index.html"

# Demo chat page; read per request so edits show up on refresh.
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(INDEX_HTML)

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
            person["employment"] = cur.execute(
                """
                SELECT e.title, e.start_date, e.end_date,
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
                       s.seats_available, s.source, s.reported_at,
                       now() - s.reported_at AS age,
                       p.id AS reported_by_id, p.name AS reported_by_name
                FROM course_batches b
                LEFT JOIN LATERAL (
                    SELECT seats_available, source, reported_at, reported_by_person_id
                    FROM seat_availability
                    WHERE batch_id = b.id
                    ORDER BY reported_at DESC
                    LIMIT 1
                ) s ON true
                LEFT JOIN people p ON p.id = s.reported_by_person_id
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
                "source": b["source"],
                "reported_by": (
                    {"id": b["reported_by_id"], "name": b["reported_by_name"]}
                    if b["reported_by_id"] is not None
                    else None
                ),
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
def create_seat_report(batch_id: int, report: SeatReport):
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
                    (batch_id, seats_available, source, reported_by_person_id)
                VALUES (%s, %s, %s, %s)
                RETURNING id, batch_id, seats_available, source, reported_at,
                          reported_by_person_id
                """,
                (batch_id, report.seats_available, report.source,
                 report.reported_by_person_id),
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

The profiles list everyone in the directory with their jobs. For courses, seats \
and what people said in interviews, use the tools. When the user gives no ID, \
search first with a few short keywords (a name, role, service, course name or \
code), then look up details by the ID you found. The directory records which institute offers each course, \
not who teaches it. Seat counts include how fresh they are: say so when one is \
"aging" or "stale", and say seats are unknown when there is no report. \
For what people have said about their work, experience or advice, use \
search_documents and attribute each point to the person who said it. \
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
            "and its batches with dates, capacity, latest seat availability and how "
            "fresh that seat count is.",
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

def deepseek_chat(messages: list[dict]) -> dict:
    request = urllib.request.Request(
        f"{DEEPSEEK_URL}/chat/completions",
        data=json.dumps(
            {"model": DEEPSEEK_MODEL, "messages": messages, "tools": CHAT_TOOLS}
        ).encode(),
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
            years = (
                f"{row['start_date'].year}-{row['end_date'].year}"
                if row["end_date"]
                else f"since {row['start_date'].year}"
            )
            person["jobs"].append(
                f"{row['title']} at {row['organization']} ({row['org_type']}), {years}"
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
                return {
                    "answer": remove_ids(answer),
                    "tool_calls": tool_calls,
                    "removed_quotes": removed,
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
