import os

from dotenv import load_dotenv
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

load_dotenv()

conninfo = make_conninfo(
    host=os.getenv("POSTGRES_HOST", "localhost"),
    port=os.getenv("POSTGRES_PORT", "5432"),
    user=os.environ["POSTGRES_USER"],
    password=os.environ["POSTGRES_PASSWORD"],
    dbname=os.environ["POSTGRES_DB"],
    connect_timeout=3,
)

# Opened in the app lifespan so startup doesn't fail if the DB is down.
pool = ConnectionPool(conninfo, min_size=1, max_size=5, open=False, timeout=3)
