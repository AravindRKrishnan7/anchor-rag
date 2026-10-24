from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, HTTPException
from psycopg_pool import PoolTimeout

from app.db import pool

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
    except (psycopg.Error, PoolTimeout):
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {"status": "ok", "postgres": postgres, "pgvector": row[0] if row else None}

PEOPLE = {
1: {"name": "Captain Ravi Menon", "role": "Chief Engineer", "services": ["ship maintenance"]},
2: {"name": "Anita Thomas", "role": "Port Consultant", "services": ["port operations"]},
}

@app.get("/people/{person_id}")
def get_person(person_id: int):
    person = PEOPLE.get(person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return person