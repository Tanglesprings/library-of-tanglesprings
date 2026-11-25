"""Browser queue actions and the outbound ww-node worker protocol."""

import base64
import json
import logging
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.auth import require_role
from app.database import get_db, get_setting
from app.services import section_label_pdf, section_printing

router = APIRouter()
logger = logging.getLogger(__name__)


class Claim(BaseModel):
    type: Literal["claim"]
    queue: Literal["DYMO_LabelWriter_450_Turbo"]
    widthMm: float = Field(ge=25, le=160)
    heightMm: float = Field(ge=25, le=160)


class Submitted(BaseModel):
    type: Literal["submitted"]
    id: UUID
    spoolId: str = Field(min_length=1, max_length=140)


class Failed(BaseModel):
    type: Literal["failed"]
    id: UUID
    error: str = Field(max_length=160)


@router.post("/api/label-printer")
def printer_worker(
    request: Request, message: Annotated[Claim | Submitted | Failed, Field(discriminator="type")],
):
    with get_db() as db:
        if not section_printing.worker_authorized(db, request.headers.get("Authorization", "")):
            return Response(status_code=401)
        if not isinstance(message, Claim):
            spool_id = message.spoolId if isinstance(message, Submitted) else None
            done = section_printing.finish(db, str(message.id), message.type, spool_id)
            return Response(status_code=204 if done else 409)
        job = section_printing.claim(db)

    if job is None:
        return Response(status_code=204)
    try:
        output = section_label_pdf.render(json.loads(job["labels"]), message.widthMm, message.heightMm)
    except Exception:
        logger.exception("Could not render section print job %s", job["id"])
        with get_db() as db:
            section_printing.finish(db, job["id"], "failed")
        return Response(status_code=500)
    return {"id": job["id"], "sku": "Library Sections", "pdf": base64.b64encode(output).decode("ascii")}


@router.post("/labels/sections/jobs")
def queue_sections(
    request: Request, job_id: UUID = Form(...), labels: str = Form(..., max_length=170_000),
    _=Depends(require_role("editor")),
):
    try:
        names = json.loads(labels)
    except ValueError:
        names = None
    if not section_printing.valid_labels(names):
        raise HTTPException(400, "Choose between 1 and 300 section labels, up to 100 characters each.")
    with get_db() as db:
        if not get_setting(db, section_printing.WORKER_DIGEST):
            raise HTTPException(503, "The ww-node DYMO is awaiting pairing.")
        if not section_printing.enqueue(db, str(job_id), request.state.user["id"], names):
            raise HTTPException(409, "Print job ID already exists.")
    return RedirectResponse(f"/labels/sections/jobs/{job_id}", status_code=303)


@router.get("/labels/sections/jobs/{job_id}")
def section_job(request: Request, job_id: UUID, _=Depends(require_role("editor"))):
    with get_db() as db:
        job = db.execute(
            "SELECT *, CASE WHEN status = 'sending' AND claimed_at < datetime('now', '-10 minutes') "
            "THEN 'unconfirmed' ELSE status END AS display_status "
            "FROM section_label_jobs WHERE id = ? AND requested_by = ?",
            (str(job_id), request.state.user["id"]),
        ).fetchone()
    if job is None:
        raise HTTPException(404, "Section print job not found.")
    template = "fragments/section_print_status.html" if request.headers.get("HX-Request") else "section_print_job.html"
    return request.app.state.templates.TemplateResponse(
        request, template, {"job": job, "count": len(json.loads(job["labels"]))},
    )
