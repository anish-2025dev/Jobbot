"""Onboarding endpoints: resume import into a draft vault, setup checklist."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app import db
from app.auth import principal
from app.llm.client import get_llm
from app.onboarding import checklist, import_resume

router = APIRouter(dependencies=[Depends(principal)])
MAX_UPLOAD = 5 * 1024 * 1024


@router.post("/users/{user_id}/onboarding/import")
async def onboarding_import(user_id: UUID, file: UploadFile | None = File(default=None),
                            text: str | None = Form(default=None)) -> dict:
    """Upload a resume PDF/DOCX/TXT (or paste text). Returns a draft vault to
    review; nothing is saved until you PUT it to /users/{id}/vault."""
    if file is None and not text:
        raise HTTPException(422, "send a file or text")
    if file is not None:
        data = await file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "file too large (5 MB max)")
        name = (file.filename or "").lower()
        if name.endswith(".pdf") or data[:4] == b"%PDF":
            from app.resume.ats import pdf_text
            text = pdf_text(data)
        elif name.endswith(".docx"):
            import io

            from docx import Document
            text = "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
        else:
            text = data.decode("utf-8", "replace")
    if not text or len(text.strip()) < 100:
        raise HTTPException(422, "could not read enough text from that resume (scanned PDFs need OCR first)")
    return import_resume(text, get_llm())


@router.get("/users/{user_id}/onboarding")
def onboarding_status(user_id: UUID) -> list[dict]:
    with db.user_session(user_id) as s:
        return checklist(s, user_id)
