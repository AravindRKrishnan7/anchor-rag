import json
import os
import urllib.request

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
EMBED_TIMEOUT = 120  # seconds; the first call also loads the model

def embed(texts: list[str]) -> list[list[float]]:
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed",
        data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=EMBED_TIMEOUT) as response:
        return json.load(response)["embeddings"]

# nomic-embed-text is trained with these task prefixes; retrieval is
# noticeably worse without them.
def embed_documents(texts: list[str]) -> list[list[float]]:
    return embed([f"search_document: {t}" for t in texts])

def embed_query(text: str) -> list[float]:
    return embed([f"search_query: {text}"])[0]

# pgvector accepts '[x,y,...]' text cast with ::vector, so no extra driver
# package is needed.
def to_pgvector(vector: list[float]) -> str:
    return "[" + ",".join(map(str, vector)) + "]"
