"""Text embeddings for search, from the provider EMBEDDINGS_PROVIDER names:

- ollama (default): nomic-embed-text on a local Ollama.
- nomic: the same model via Nomic's hosted API (NOMIC_API_KEY); its vectors
  match Ollama's.
- gemini: Google's gemini-embedding-001 at 768 dimensions (GEMINI_API_KEY).
  Free-tier requests may be used by Google to improve its products.

Every chunk records EMBED_MODEL, and search only compares vectors from the
current model; scripts/reembed.py converts stored chunks after a switch.
"""

import json
import math
import os
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv()

PROVIDER = os.getenv("EMBEDDINGS_PROVIDER", "ollama").strip().lower()
EMBED_DIMENSIONS = 768  # chunks.embedding is vector(768)
EMBED_TIMEOUT = 120  # seconds; Ollama's first call also loads the model
# Texts per request; hosted APIs cap batch sizes.
EMBED_BATCH = 64

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
NOMIC_URL = "https://api-atlas.nomic.ai/v1/embedding/text"
NOMIC_MODEL = "nomic-embed-text-v1.5"
GEMINI_MODEL = "gemini-embedding-001"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:batchEmbedContents"

if PROVIDER == "ollama":
    EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
elif PROVIDER == "nomic":
    EMBED_MODEL = NOMIC_MODEL
elif PROVIDER == "gemini":
    EMBED_MODEL = f"{GEMINI_MODEL}-{EMBED_DIMENSIONS}"
else:
    raise RuntimeError(f"EMBEDDINGS_PROVIDER must be ollama, nomic or gemini, not {PROVIDER!r}")

class EmbeddingError(Exception):
    """The embedding provider is unreachable, misconfigured or refused the request."""

def post_json(url: str, body: dict, headers: dict | None = None) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=EMBED_TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        # Never echo the request: it carries the API key.
        raise EmbeddingError(f"{PROVIDER} embeddings failed (HTTP {e.code})") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise EmbeddingError(f"{PROVIDER} embeddings unreachable") from e

def api_key(name: str) -> str:
    key = os.getenv(name)
    if not key:
        raise EmbeddingError(f"{name} is not set (EMBEDDINGS_PROVIDER={PROVIDER})")
    return key

def normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]

# task is "document" (stored passages) or "query" (a search).
def embed_batch(texts: list[str], task: str) -> list[list[float]]:
    if PROVIDER == "ollama":
        # nomic-embed-text is trained with these task prefixes; retrieval is
        # noticeably worse without them. Nomic's API adds them itself.
        prefixed = [f"search_{task}: {t}" for t in texts]
        return post_json(f"{OLLAMA_URL}/api/embed",
                         {"model": EMBED_MODEL, "input": prefixed})["embeddings"]
    if PROVIDER == "nomic":
        return post_json(
            NOMIC_URL,
            {"model": NOMIC_MODEL, "texts": texts, "task_type": f"search_{task}"},
            {"Authorization": f"Bearer {api_key('NOMIC_API_KEY')}"},
        )["embeddings"]
    # Gemini vectors cut below 3072 dimensions must be normalized.
    task_type = "RETRIEVAL_DOCUMENT" if task == "document" else "RETRIEVAL_QUERY"
    response = post_json(
        GEMINI_URL,
        {"requests": [{"model": f"models/{GEMINI_MODEL}", "content": {"parts": [{"text": t}]},
                       "taskType": task_type, "outputDimensionality": EMBED_DIMENSIONS}
                      for t in texts]},
        {"x-goog-api-key": api_key("GEMINI_API_KEY")},
    )
    return [normalize(e["values"]) for e in response["embeddings"]]

def embed(texts: list[str], task: str) -> list[list[float]]:
    vectors = []
    for start in range(0, len(texts), EMBED_BATCH):
        vectors += embed_batch(texts[start:start + EMBED_BATCH], task)
    if len(vectors) != len(texts) or any(len(v) != EMBED_DIMENSIONS for v in vectors):
        raise EmbeddingError(f"{PROVIDER} returned unexpected embeddings")
    return vectors

def embed_documents(texts: list[str]) -> list[list[float]]:
    return embed(texts, "document")

def embed_query(text: str) -> list[float]:
    return embed([text], "query")[0]

# pgvector accepts '[x,y,...]' text cast with ::vector, so no extra driver
# package is needed.
def to_pgvector(vector: list[float]) -> str:
    return "[" + ",".join(map(str, vector)) + "]"
