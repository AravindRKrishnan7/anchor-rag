"""Turning interviews and documents into searchable chunks.

Used by scripts/ingest_transcripts.py (transcript files) and the admin
add-content page (pasted transcripts and uploaded PDFs). A transcript is
"Speaker: text" turns; each of the guest's answers becomes one chunk. A
document's text is split into chunks of about CHUNK_CHARS characters.
"""

import hashlib
import io
import re

import pypdf
from fastapi import HTTPException

from app.embeddings import EMBED_MODEL, to_pgvector

INTERVIEWER = "Interviewer"
TURN = re.compile(r"^([A-Z][\w .'-]*?):\s*(.*)$")
# Header keys a transcript file may start with (see the script's docstring).
HEADER_LINE = re.compile(r"^(person_id|title|date):\s*(.*)$")
# Target document chunk size: long enough to keep a point together, short
# enough that one chunk is about one topic.
CHUNK_CHARS = 1000

def split_header(text: str) -> tuple[dict, str]:
    """A transcript file's "key: value" header (if any) and the rest."""
    first, sep, rest = text.strip().partition("\n\n")
    lines = first.splitlines()
    if lines and all(HEADER_LINE.match(line.strip()) for line in lines):
        return {HEADER_LINE.match(l.strip())[1]: HEADER_LINE.match(l.strip())[2].strip()
                for l in lines}, rest
    return {}, text

def parse_turns(body: str) -> list[tuple[str, str]]:
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
    return turns

def parse_transcript(text: str) -> tuple[dict, list[tuple[str, str]]]:
    header, body = split_header(text)
    return header, parse_turns(body)

def is_guest(speaker: str, guest_name: str) -> bool:
    """A speaker label is the guest when it is part of their name ("Ravi" or
    "Ravi Menon" for Ravi Menon)."""
    return speaker != INTERVIEWER and speaker.lower() in guest_name.lower()

def answer_chunks(turns: list[tuple[str, str]], guest_name: str) -> list[str]:
    return [said for speaker, said in turns if is_guest(speaker, guest_name) and said]

def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s]

def text_chunks(text: str) -> list[str]:
    """Paragraphs and sentences packed into chunks of about CHUNK_CHARS."""
    chunks, current = [], ""
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = re.sub(r"\s+", " ", paragraph).strip()
        if not paragraph:
            continue
        # Prefer starting a new chunk at a paragraph once this one is half full.
        if current and len(current) >= CHUNK_CHARS // 2:
            chunks.append(current)
            current = ""
        for sentence in sentences(paragraph):
            # A sentence too long for one chunk is cut at word boundaries.
            while len(sentence) > CHUNK_CHARS:
                cut = sentence.rfind(" ", 0, CHUNK_CHARS)
                cut = cut if cut > 0 else CHUNK_CHARS
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(sentence[:cut].strip())
                sentence = sentence[cut:].strip()
            if current and len(current) + 1 + len(sentence) > CHUNK_CHARS:
                chunks.append(current)
                current = sentence
            else:
                current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks

def content_hash(chunks: list[str]) -> str:
    """Hash of what would be indexed, so the same content pasted or uploaded
    differently is still recognised."""
    return hashlib.sha256("\n\n".join(chunks).encode()).hexdigest()

def pdf_text(data: bytes) -> str:
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    except pypdf.errors.PyPdfError:
        raise HTTPException(status_code=422, detail="Could not read this PDF")

def store_chunks(cur, chunks: list[str], vectors: list[list[float]], *,
                 interview_id: int | None = None, person_id: int | None = None,
                 document_id: int | None = None, organization_id: int | None = None):
    cur.executemany(
        """
        INSERT INTO chunks
            (interview_id, person_id, document_id, organization_id, chunk_index,
             content, embedding, embedding_model)
        VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s)
        """,
        [
            (interview_id, person_id, document_id, organization_id, i, chunk,
             to_pgvector(vector), EMBED_MODEL)
            for i, (chunk, vector) in enumerate(zip(chunks, vectors))
        ],
    )
