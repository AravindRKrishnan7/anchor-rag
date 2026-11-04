"""Re-embed stored chunks with the current embeddings model.

Run from the repo root after changing EMBEDDINGS_PROVIDER:
    python -m scripts.reembed

Search only compares vectors from the current model, so chunks embedded by
another one are invisible until re-embedded. This covers every chunk:
transcripts loaded by scripts/ingest_transcripts.py and interviews and
documents added in the browser. Each batch is committed, so it can be
stopped and run again; finished chunks are skipped.
"""

import sys

from app.db import pool
from app.embeddings import EMBED_BATCH, EMBED_MODEL, PROVIDER, embed_documents, to_pgvector

def main() -> int:
    pool.open()
    try:
        with pool.connection() as conn:
            # Without autocommit the first SELECT would open one transaction
            # for the whole run and each batch would only be a savepoint.
            conn.autocommit = True
            total = conn.execute(
                "SELECT count(*) FROM chunks WHERE embedding_model <> %s", (EMBED_MODEL,)
            ).fetchone()[0]
            print(f"{total} chunks to re-embed with {EMBED_MODEL} ({PROVIDER})")
            done = 0
            while True:
                rows = conn.execute(
                    "SELECT id, content FROM chunks WHERE embedding_model <> %s "
                    "ORDER BY id LIMIT %s",
                    (EMBED_MODEL, EMBED_BATCH),
                ).fetchall()
                if not rows:
                    break
                vectors = embed_documents([content for _, content in rows])
                with conn.transaction(), conn.cursor() as cur:
                    cur.executemany(
                        "UPDATE chunks SET embedding = %s::vector, embedding_model = %s "
                        "WHERE id = %s",
                        [(to_pgvector(v), EMBED_MODEL, id_) for (id_, _), v in zip(rows, vectors)],
                    )
                done += len(rows)
                print(f"  {done}/{total}")
    finally:
        pool.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
