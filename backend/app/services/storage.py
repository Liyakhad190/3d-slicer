"""Content-addressed blob storage.

Storage keys are derived from the SHA-256 of the content and nothing else.
No caller-supplied string ever reaches a filesystem path — that is the
structural defence against path traversal, rather than a sanitiser we might
later get wrong.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import NotFoundError, PayloadTooLargeError
from app.core.logging import get_logger
from app.db.session import session_scope
from app.models.assets import Blob

log = get_logger(__name__)

CHUNK_SIZE = 1024 * 1024


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _path_for(sha: str) -> Path:
    settings = get_settings()
    # sha is 64 hex chars produced by hashlib, so this cannot escape the root.
    return settings.blob_dir / sha[:2] / sha


def put_blob(
    data: bytes,
    *,
    media_type: str = "application/octet-stream",
    detected_format: str = "unknown",
    session: Session | None = None,
) -> tuple[Blob, bool]:
    """Store `data`, deduplicating by content hash.

    Returns `(blob, created)`. An identical upload returns the existing row
    with `created=False` and does not touch the filesystem.
    """
    sha = sha256_hex(data)

    if session is not None:
        existing = session.execute(select(Blob).where(Blob.sha256 == sha)).scalar_one_or_none()
        if existing is not None:
            return existing, False
    else:
        with session_scope() as scope:
            existing = scope.execute(
                select(Blob).where(Blob.sha256 == sha)
            ).scalar_one_or_none()
            if existing is not None:
                return existing, False

    target = _path_for(sha)
    target.parent.mkdir(parents=True, exist_ok=True)

    if not target.exists():
        # Atomic: write to a sibling temp file, fsync, then rename. A crash can
        # leave a stray temp file but never a truncated blob at the real key.
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, target)
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise

    relative = target.relative_to(get_settings().data_dir).as_posix()

    if session is not None:
        blob = Blob(
            sha256=sha,
            size_bytes=len(data),
            media_type=media_type,
            detected_format=detected_format,
            storage_path=relative,
        )
        session.add(blob)
        session.flush()
        return blob, True

    with session_scope() as scope:
        blob = scope.execute(select(Blob).where(Blob.sha256 == sha)).scalar_one_or_none()
        if blob is not None:
            return blob, False
        blob = Blob(
            sha256=sha,
            size_bytes=len(data),
            media_type=media_type,
            detected_format=detected_format,
            storage_path=relative,
        )
        scope.add(blob)
        scope.flush()
        return blob, True


def get_blob_path(blob: Blob) -> Path:
    """Resolve a blob to its path, refusing anything outside the blob root."""
    settings = get_settings()
    candidate = (settings.data_dir / blob.storage_path).resolve()
    root = settings.blob_dir.resolve()
    if not candidate.is_relative_to(root):
        # A row pointing outside the blob root means either corruption or a
        # tampered database. Refuse rather than read an arbitrary file.
        log.error("blob.path_escape", storage_path=blob.storage_path, sha256=blob.sha256)
        raise NotFoundError("Stored file could not be located.")
    if not candidate.is_file():
        raise NotFoundError("Stored file is missing from the object store.")
    return candidate


def read_blob(blob: Blob) -> bytes:
    path = get_blob_path(blob)
    settings = get_settings()
    size = path.stat().st_size
    if size > settings.max_upload_bytes:
        raise PayloadTooLargeError("Stored file exceeds the configured size limit.")
    return path.read_bytes()


def iter_blob_chunks(blob: Blob, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
    with get_blob_path(blob).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            yield chunk


def get_or_create_blob(
    sha: str, *, media_type: str, detected_format: str, size_bytes: int, session: Session
) -> Blob:
    blob = session.execute(select(Blob).where(Blob.sha256 == sha)).scalar_one_or_none()
    if blob is not None:
        return blob
    target = _path_for(sha)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        # Row exists (or was created by a concurrent worker) but bytes are gone:
        # the content is unrecoverable, so fail loudly rather than record a
        # phantom blob.
        raise NotFoundError(f"Blob {sha[:12]} is referenced by the database but absent on disk.")
    blob = Blob(
        sha256=sha,
        size_bytes=size_bytes,
        media_type=media_type,
        detected_format=detected_format,
        storage_path=target.relative_to(get_settings().data_dir).as_posix(),
    )
    session.add(blob)
    session.flush()
    return blob


def verify_blob_integrity(blob: Blob) -> bool:
    """Re-hash the stored bytes and compare. Used by tests and integrity jobs."""
    path = get_blob_path(blob)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest() == blob.sha256


def new_job_workspace(job_id: uuid.UUID) -> Path:
    """A scratch directory for a slicing subprocess.

    Lives under the data dir (never a temp dir outside it) so the whole
    data directory can be mounted read-write for the worker and read-only for
    the model's input file.
    """
    root = get_settings().data_dir / "workspaces"
    path = root / str(job_id)
    path.mkdir(parents=True, exist_ok=True)
    return path
