"""Retrieval entry point: query -> a bounded, formatted context block."""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.intelligence.rag.base import KnowledgeSource, RetrievedDocument
from app.intelligence.rag.in_memory import InMemoryKnowledgeSource

log = get_logger(__name__)

_source: KnowledgeSource | None = None


def get_knowledge_source() -> KnowledgeSource:
    global _source
    if _source is None:
        _source = InMemoryKnowledgeSource()
    return _source


def reset_knowledge_source() -> None:
    """Test hook."""
    global _source
    _source = None


def retrieve(
    query: str,
    *,
    settings: Settings | None = None,
    limit: int | None = None,
    tags: tuple[str, ...] = (),
) -> list[RetrievedDocument]:
    """Retrieve knowledge for a user query, bounded and RAG-gated."""
    settings = settings or get_settings()
    if not settings.rag_enabled:
        return []
    effective_limit = limit if limit is not None else settings.rag_max_documents
    if effective_limit <= 0:
        return []
    documents = get_knowledge_source().search(query, limit=effective_limit, tags=tags)
    log.info(
        "llm.retrieval",
        query_chars=len(query),
        retrieved=len(documents),
        ids=[item.document.id for item in documents],
    )
    return documents


def format_context(documents: list[RetrievedDocument]) -> str:
    """Render retrieved documents for the prompt.

    Bounded on purpose: every character here is prefill that the local model has
    to read before it can answer, and the corpus is already small.
    """
    if not documents:
        return ""
    blocks = []
    for item in documents:
        blocks.append(f"[{item.document.title}]\n{item.document.content}")
    return "\n\n".join(blocks)