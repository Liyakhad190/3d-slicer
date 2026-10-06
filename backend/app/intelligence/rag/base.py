"""RAG port.

Retrieval is deliberately decoupled from both the provider and any particular
store. `KnowledgeSource` is the only thing the retriever knows about, so
replacing the in-process corpus with Postgres + pgvector later is a new
implementation of one interface, not a rewrite of the planner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class KnowledgeDocument:
    """One retrievable piece of platform knowledge."""

    id: str
    title: str
    content: str
    #: Free-form labels used to filter retrieval, e.g. ("material", "petg").
    tags: tuple[str, ...] = ()
    source: str = "platform"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "content": self.content,
            "tags": list(self.tags),
            "source": self.source,
        }


@dataclass(frozen=True)
class RetrievedDocument:
    document: KnowledgeDocument
    #: 0..1 lexical relevance. Deliberately lexical: see `in_memory.py` for why.
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {**self.document.to_dict(), "score": round(self.score, 4)}


@runtime_checkable
class KnowledgeSource(Protocol):
    """Where knowledge comes from. Implementations must be read-only."""

    name: str

    def search(self, query: str, limit: int, tags: tuple[str, ...] = ()) -> list[RetrievedDocument]:
        ...