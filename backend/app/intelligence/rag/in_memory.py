"""In-process knowledge source with lexical scoring.

Deliberately lexical rather than embedding-based. Embeddings would mean a model
download at startup, a network dependency for a feature that must work offline,
and — on the small local models this platform targets — worse retrieval than
good keyword matching over a corpus this size. The corpus is ~10 documents; an
inverted index over content words scores it correctly.

`InMemoryKnowledgeSource` also accepts an injected source of extra documents, so
per-machine or per-workspace notes can be layered on without changing the
planner.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

from app.intelligence.rag.base import KnowledgeDocument, RetrievedDocument
from app.intelligence.rag.knowledge import KNOWLEDGE_DOCUMENTS

_TOKEN = re.compile(r"[a-z0-9]+")

#: Words too common to carry signal in a corpus this small.
_STOPWORDS = frozenset(
    """
    a an and are as at be but by can do does for from had has have how i if in into is it
    its me my of on or that the their then there these they this to want was what when
    which will with would you your please make print
    """.split()
)

#: Query words mapped to the corpus vocabulary they mean. Needed because a user
#: says "strong", "fast", "cheap" and "light" while the documents are written in
#: "strength", "speed", "cost" and "material". Without this, "I want this really
#: strong" retrieved the clarification document instead of the strength document.
#:
#: Hand-written rather than embedded: it is short, auditable, and correct for
#: this corpus. Extending it is a deliberate edit.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "strong": ("strength", "strong"),
    "sturdy": ("strength", "strong"),
    "tough": ("strength", "strong"),
    "durable": ("strength",),
    "rigid": ("strength",),
    "fast": ("speed", "fast"),
    "quick": ("speed", "fast"),
    "rapid": ("speed", "fast"),
    "quality": ("surface_quality", "quality"),
    "smooth": ("surface_quality", "quality"),
    "finish": ("surface_quality", "quality"),
    "pretty": ("surface_quality", "quality"),
    "detail": ("surface_quality", "quality"),
    "light": ("material_efficiency", "lightweight"),
    "lightweight": ("material_efficiency", "lightweight"),
    "cheap": ("material_efficiency", "cost"),
    "cost": ("material_efficiency", "cost"),
    "save": ("material_efficiency",),
    "waste": ("material_efficiency",),
    "fill": ("infill",),
    "perimeter": ("walls", "perimeter"),
    "perimeters": ("walls", "perimeter"),
    "wall": ("walls",),
    "walls": ("walls",),
    "layer": ("layer_height", "layer"),
    "layers": ("layer_height", "layer"),
    "support": ("supports", "support"),
    "supports": ("supports", "support"),
    "bracket": ("supports", "bracket"),
    "hotend": ("temperature",),
    "temp": ("temperature",),
    "temperature": ("temperature",),
    "budget": ("constraints", "budget"),
    "hour": ("time",),
    "hours": ("time",),
    "fastest": ("speed", "time"),
    "orientation": ("orientation",),
    "orient": ("orientation",),
    "rotate": ("orientation",),
}


def _tokenize(text: str) -> list[str]:
    """Tokens with plurals normalised and domain synonyms expanded."""
    tokens: list[str] = []
    for raw in _TOKEN.findall(text.lower()):
        if raw in _STOPWORDS:
            continue
        token = raw
        # Cheap plural/verb normalisation: "walls" -> "wall", "printing" -> "print".
        if len(token) > 4 and token.endswith("ies"):
            token = token[:-3] + "y"
        elif len(token) > 4 and token.endswith(("ses", "xes", "zes", "ches", "shes")):
            token = token[:-2]
        elif len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        tokens.append(token)
        for synonym in _SYNONYMS.get(raw, ()) + _SYNONYMS.get(token, ()):
            if synonym not in _STOPWORDS:
                tokens.append(synonym)
    return tokens


class InMemoryKnowledgeSource:
    """A read-only, lexical knowledge source."""

    name = "in_memory"

    def __init__(self, extra_documents: Iterable[KnowledgeDocument] = ()) -> None:
        self._documents: tuple[KnowledgeDocument, ...] = tuple(extra_documents) + KNOWLEDGE_DOCUMENTS
        # title + tags are indexed with a higher weight than body prose, so a
        # query naming a topic ("supports", "infill") ranks the relevant document
        # above one that merely mentions the word in passing.
        self._index: list[tuple[KnowledgeDocument, Counter[str], set[str]]] = [
            (document, Counter(_tokenize(f"{document.title} {' '.join(document.tags)}")), set(_tokenize(document.content)))
            for document in self._documents
        ]

    @property
    def documents(self) -> tuple[KnowledgeDocument, ...]:
        return self._documents

    def search(
        self, query: str, limit: int, tags: tuple[str, ...] = ()
    ) -> list[RetrievedDocument]:
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        wanted_tags = {tag.lower() for tag in tags}
        results: list[RetrievedDocument] = []

        for document, metadata_tokens, content_tokens in self._index:
            if wanted_tags and not wanted_tags.intersection(
                {tag.lower() for tag in document.tags}
            ):
                continue

            score = 0.0
            for token in query_tokens:
                # Title and tag hits count triple: they are curated signal.
                if token in metadata_tokens:
                    score += 3.0 * metadata_tokens[token]
                elif token in content_tokens:
                    score += 1.0

            if score <= 0:
                continue
            # Squash so one very long document cannot dominate purely by length.
            results.append(
                RetrievedDocument(document=document, score=score / (score + 4.0))
            )

        results.sort(key=lambda item: (-item.score, item.document.id))
        return results[:limit]