from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Literal

import psycopg
from fastapi import FastAPI, HTTPException
from psycopg.rows import dict_row
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, Field

from app.db import pool

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

@app.get("/people/{person_id}")
def get_person(person_id: int):
    try:
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

@app.get("/courses/{course_id}")
def get_course(course_id: int):
    try:
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
    except DB_ERRORS:
        raise HTTPException(status_code=503, detail="Database unavailable")
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found")
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
