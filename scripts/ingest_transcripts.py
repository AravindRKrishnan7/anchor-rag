"""Load interview transcripts into the interviews and chunks tables.

Run from the repo root: python -m scripts.ingest_transcripts

Each file in transcripts/ starts with "key: value" header lines (person_id,
title, date), a blank line, then "Speaker: text" turns. Every answer (any
speaker but the interviewer) becomes one chunk. Files are keyed by name, so
re-running replaces a file's chunks instead of duplicating them, and files
whose text and embedding model are unchanged are skipped.
"""

import hashlib
import re
import sys
from pathlib import Path

from app.db import pool
from app.embeddings import EMBED_MODEL, embed_documents, to_pgvector

TRANSCRIPTS_DIR = Path(__file__).resolve().parent.parent / "transcripts"
INTERVIEWER = "Interviewer"
TURN = re.compile(r"^([A-Z][\w .'-]*?):\s*(.*)$")

def parse_transcript(text: str) -> tuple[dict, list[tuple[str, str]]]:
    header_text, _, body = text.partition("\n\n")
    header = {}
    for line in header_text.splitlines():
        key, _, value = line.partition(":")
        header[key.strip()] = value.strip()
    turns = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        match = TURN.match(line)
        if match:
            turns.append((match[1], match[2]))
        elif turns:
            # Continuation line: join it to the previous turn.
            speaker, said = turns[-1]
            turns[-1] = (speaker, f"{said} {line}")
        else:
            raise ValueError(f"Text before the first speaker: {line!r}")
    return header, turns

def ingest_file(path: Path) -> str:
    text = path.read_text()
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    header, turns = parse_transcript(text)
    answers = [said for speaker, said in turns if speaker != INTERVIEWER]
    speakers = {speaker for speaker, _ in turns if speaker != INTERVIEWER}
    if not answers:
        raise ValueError(f"{path.name}: no answers found")
    person_id = int(header["person_id"])

    with pool.connection() as conn:
        person = conn.execute(
            "SELECT name FROM people WHERE id = %s", (person_id,)
        ).fetchone()
        if person is None:
            raise ValueError(f"{path.name}: person {person_id} not in database")
        # Catches a header pointing at the wrong person.
        if any(speaker not in person[0] for speaker in speakers):
            raise ValueError(
                f"{path.name}: speakers {sorted(speakers)} don't match person "
                f"{person_id} ({person[0]})"
            )

        existing = conn.execute(
            """
            SELECT i.content_hash,
                   bool_and(c.embedding_model = %s) AS same_model
            FROM interviews i
            LEFT JOIN chunks c ON c.interview_id = i.id
            WHERE i.source_file = %s
            GROUP BY i.id
            """,
            (EMBED_MODEL, path.name),
        ).fetchone()
        if existing and existing[0] == content_hash and existing[1]:
            return "unchanged"

        # Embed before writing so an Ollama failure leaves the old rows intact.
        embeddings = embed_documents(answers)
        interview_id = conn.execute(
            """
            INSERT INTO interviews (person_id, title, interviewed_on, source_file, content_hash)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (source_file) DO UPDATE SET
                person_id = EXCLUDED.person_id,
                title = EXCLUDED.title,
                interviewed_on = EXCLUDED.interviewed_on,
                content_hash = EXCLUDED.content_hash
            RETURNING id
            """,
            (person_id, header["title"], header.get("date"), path.name, content_hash),
        ).fetchone()[0]
        conn.execute("DELETE FROM chunks WHERE interview_id = %s", (interview_id,))
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO chunks
                    (interview_id, person_id, chunk_index, content, embedding, embedding_model)
                VALUES (%s, %s, %s, %s, %s::vector, %s)
                """,
                [
                    (interview_id, person_id, i, answer, to_pgvector(vector), EMBED_MODEL)
                    for i, (answer, vector) in enumerate(zip(answers, embeddings))
                ],
            )
    return f"inserted {len(answers)} chunks"

def main() -> int:
    paths = sorted(TRANSCRIPTS_DIR.glob("*.md"))
    if not paths:
        print(f"No transcripts in {TRANSCRIPTS_DIR}", file=sys.stderr)
        return 1
    pool.open()
    try:
        for path in paths:
            print(f"{path.name}: {ingest_file(path)}")
    finally:
        pool.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
