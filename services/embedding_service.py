"""
Embedding service — generates 768-dim text embeddings via Gemini embedding-001.
Uses asymmetric task types for better search relevance:
  - RETRIEVAL_DOCUMENT for stored feature request text
  - RETRIEVAL_QUERY for transient search queries
"""
import os
from google import genai
from google.genai import types

_client = None


def _get_client():
    global _client
    if _client is None:
        api_key = os.getenv("api_key", "").strip()
        if not api_key:
            raise RuntimeError("Gemini API key not configured")
        _client = genai.Client(api_key=api_key, http_options={"timeout": 30})
    return _client


def generate_embedding(text, task_type="RETRIEVAL_DOCUMENT"):
    """Generate a 768-dim embedding for the given text.

    task_type: "RETRIEVAL_DOCUMENT" for stored content,
               "RETRIEVAL_QUERY" for search queries.
    """
    client = _get_client()
    result = client.models.embed_content(
        model="gemini-embedding-001",
        contents=text,
        config=types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=768,
        ),
    )
    return result.embeddings[0].values


def generate_embeddings_batch(texts, task_type="RETRIEVAL_DOCUMENT"):
    """Batch embed multiple texts. Returns list of 768-dim vectors."""
    client = _get_client()
    result = client.models.embed_content(
        model="gemini-embedding-001",
        contents=texts,
        config=types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=768,
        ),
    )
    return [e.values for e in result.embeddings]
