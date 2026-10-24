from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, HTTPException
from psycopg.rows import dict_row
from psycopg_pool import PoolTimeout

from app.db import pool

DB_ERRORS = (psycopg.Error, PoolTimeout)

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