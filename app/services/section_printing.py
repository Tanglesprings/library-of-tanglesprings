"""Persistent section-label queue for the ww-node DYMO worker."""

import hashlib
import hmac
import json
import re

from app.database import get_setting

WORKER_DIGEST = "section_dymo_digest"
WORKER_SEEN = "section_dymo_seen"
WORKER_STOCK = "section_dymo_stock"
QUEUE = "DYMO_LabelWriter_450_Turbo"
DEFAULT_LABEL_SIZE = (89, 28)


def preview_dimensions(db) -> tuple[float, float]:
    """Show the worker's stock with its long edge horizontal in the browser."""
    try:
        width, height = map(float, get_setting(db, WORKER_STOCK).split("x"))
    except ValueError:
        return DEFAULT_LABEL_SIZE
    if not (25 <= width <= 160 and 25 <= height <= 160):
        return DEFAULT_LABEL_SIZE
    return max(width, height), min(width, height)


def valid_labels(labels):
    return (
        isinstance(labels, list) and 1 <= len(labels) <= 300
        and all(isinstance(name, str) and 1 <= len(name) <= 100 and name.strip() for name in labels)
    )


def worker_authorized(db, header: str) -> bool:
    match = re.fullmatch(r"Bearer ([A-Za-z0-9_-]{43})", header)
    return bool(match and hmac.compare_digest(
        get_setting(db, WORKER_DIGEST), hashlib.sha256(match[1].encode()).hexdigest(),
    ))


def enqueue(db, job_id: str, user_id: int, names: list[str]) -> bool:
    db.execute(
        "INSERT OR IGNORE INTO section_label_jobs (id, requested_by, labels) VALUES (?, ?, ?)",
        (job_id, user_id, json.dumps(names, ensure_ascii=False)),
    )
    return db.execute(
        "SELECT 1 FROM section_label_jobs WHERE id = ? AND requested_by = ?", (job_id, user_id),
    ).fetchone() is not None


def claim(db, width_mm: float, height_mm: float):
    # One UPDATE owns selection and claim so concurrent workers never share a job.
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value "
        "WHERE settings.value < datetime('now', '-20 seconds')", (WORKER_SEEN,),
    )
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value "
        "WHERE settings.value <> excluded.value", (WORKER_STOCK, f"{width_mm:g}x{height_mm:g}"),
    )
    return db.execute(
        "UPDATE section_label_jobs SET status = 'sending', claimed_at = datetime('now') "
        "WHERE id = (SELECT id FROM section_label_jobs WHERE status = 'queued' ORDER BY created_at, rowid LIMIT 1) "
        "RETURNING id, labels",
    ).fetchone()


def finish(db, job_id: str, status: str, spool_id: str | None = None) -> bool:
    # A host can retry an acknowledgement after dropping the HTTP response.
    return db.execute(
        "UPDATE section_label_jobs SET status = ?, spool_id = ? WHERE id = ? "
        "AND (status = 'sending' OR (status = ? AND spool_id IS ?))",
        (status, spool_id, job_id, status, spool_id),
    ).rowcount == 1
