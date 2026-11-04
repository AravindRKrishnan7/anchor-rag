import os

import psycopg
from dotenv import load_dotenv
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import ConnectionPool, PoolTimeout

load_dotenv()

# DATABASE_URL (e.g. Supabase's session pooler URL) wins; otherwise the
# POSTGRES_* settings for the local Docker database. A hosted database gets
# sslmode=require unless the URL sets its own sslmode.
if os.getenv("DATABASE_URL"):
    url = os.environ["DATABASE_URL"]
    ssl = {} if "sslmode" in conninfo_to_dict(url) else {"sslmode": "require"}
    conninfo = make_conninfo(url, connect_timeout=10, **ssl)
else:
    conninfo = make_conninfo(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"],
        connect_timeout=3,
    )

# Opened in the app lifespan so startup doesn't fail if the DB is down.
# prepare_threshold=None: poolers in transaction mode (Supabase port 6543)
# can't keep prepared statements between transactions.
pool = ConnectionPool(
    conninfo, min_size=1, max_size=5, open=False, timeout=10,
    kwargs={"prepare_threshold": None},
)

# What a failed query raises: a database error, or no free connection in time.
DB_ERRORS = (psycopg.Error, PoolTimeout)
