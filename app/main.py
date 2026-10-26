import json
import os
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Literal

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from psycopg.rows import dict_row
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, Field

from app.db import pool
from app.embeddings import EMBED_MODEL, OLLAMA_URL, embed_query, to_pgvector

DB_ERRORS = (psycopg.Error, PoolTimeout)

# Seat counts older than these are flagged so clients can warn users.
SEATS_FRESH_FOR = timedelta(days=1)
SEATS_STALE_AFTER = timedelta(days=7)

@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open(wait=False)
    yield
    pool.close()

app = FastAPI(lifespan=lifespan)

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

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
OLLAMA_TIMEOUT = 120  # seconds per model call; local models can be slow
OLLAMA_ERRORS = (urllib.error.URLError, TimeoutError)
# Upper bound on model -> tool -> model round trips for one question.
CHAT_MAX_TOOL_ROUNDS = 5

# Most matches a search tool returns.
CHAT_SEARCH_LIMIT = 5
# Candidates each of vector and full-text search feed into rank fusion, and
# the standard Reciprocal Rank Fusion constant (dampens the gap between ranks).
DOCUMENT_CANDIDATES = 20
RRF_K = 60

CHAT_SYSTEM_PROMPT = """You are the assistant for ANchor, a directory of maritime \
professionals and training courses. Answer using the tools; never invent people, \
courses, dates or seat numbers. When the user gives no ID, search first with a few \
short keywords (a name, role, service, course name or code), then look up details \
by the ID you found. The directory records which institute offers each course, \
not who teaches it. Seat counts include how fresh they are: say so when one is \
"aging" or "stale", and say seats are unknown when there is no report. \
For what people have said about their work, experience or advice, use \
search_documents and attribute each point to the person who said it. \
Today is {today}."""

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

def run_chat_tool(name: str, arguments: dict) -> dict:
    # Bad calls go back to the model as errors so it can retry or explain.
    if name not in CHAT_TOOL_QUERIES:
        return {"error": f"Unknown tool: {name}"}
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

def ollama_chat(messages: list[dict]) -> dict:
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(
            {"model": OLLAMA_MODEL, "messages": messages, "tools": CHAT_TOOLS, "stream": False}
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT) as response:
        return json.load(response)["message"]

class ChatRequest(BaseModel):
    question: str = Field(min_length=1)

@app.post("/chat")
def chat(request: ChatRequest):
    messages = [
        {"role": "system", "content": CHAT_SYSTEM_PROMPT.format(today=date.today())},
        {"role": "user", "content": request.question},
    ]
    tool_calls = []
    try:
        for _ in range(CHAT_MAX_TOOL_ROUNDS + 1):
            reply = ollama_chat(messages)
            messages.append(reply)
            if not reply.get("tool_calls"):
                return {"answer": reply.get("content", ""), "tool_calls": tool_calls}
            for call in reply["tool_calls"]:
                name = call["function"]["name"]
                arguments = call["function"].get("arguments") or {}
                result = run_chat_tool(name, arguments)
                tool_calls.append({"name": name, "arguments": arguments})
                messages.append(
                    {
                        "role": "tool",
                        "tool_name": name,
                        "content": json.dumps(jsonable_encoder(result)),
                    }
                )
    except DB_ERRORS:
        raise HTTPException(status_code=503, detail="Database unavailable")
    except OLLAMA_ERRORS:
        raise HTTPException(status_code=503, detail="Language model unavailable")
    raise HTTPException(
        status_code=502,
        detail=f"Model was still calling tools after {CHAT_MAX_TOOL_ROUNDS} rounds",
    )
