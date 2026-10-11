from fastapi import APIRouter, Depends, File, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import AppError
from ..models import Paper
from ..schemas import PaperOut, SearchResult
from ..services import llm, pdf, storage
from .deps import get_paper_or_404

router = APIRouter(prefix="/api/papers")


def _insert_with_pdf(db: Session, paper: Paper, data: bytes) -> Paper:
    """flush (to get the id) -> put the PDF in object storage -> commit. Undo everything on failure."""
    db.add(paper)
    try:
        db.flush()
        storage.put_pdf(paper.id, data)
        db.commit()
    except BaseException:
        paper_id = paper.id
        db.rollback()
        if paper_id is not None:
            storage.delete_pdf(paper_id)
        raise
    db.refresh(paper)
    return paper


@router.get("", response_model=list[PaperOut])
def list_papers(db: Session = Depends(get_db)):
    return db.scalars(select(Paper).order_by(Paper.id.desc())).all()


@router.get("/{paper_id}", response_model=PaperOut)
def get_paper(paper_id: int, db: Session = Depends(get_db)):
    return get_paper_or_404(db, paper_id)


@router.delete("/{paper_id}", status_code=204)
def delete_paper(paper_id: int, db: Session = Depends(get_db)):
    """Removes the paper from the library and deletes its stored PDF."""
    db.delete(get_paper_or_404(db, paper_id))
    db.commit()  # row first: a leftover PDF is harmless, a row without its PDF would be broken
    storage.delete_pdf(paper_id)


@router.get("/{paper_id}/pdf")
def get_paper_pdf(paper_id: int, db: Session = Depends(get_db)):
    """The stored PDF, shown inline in the browser (the 'View paper' link for uploaded papers)."""
    get_paper_or_404(db, paper_id)
    return Response(
        storage.get_pdf(paper_id),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="paper-{paper_id}.pdf"'},
    )


@router.post("", response_model=PaperOut, status_code=201)
def save_paper(body: SearchResult, db: Session = Depends(get_db)):
    """Save fails (422) unless the paper's PDF can be fetched."""
    data = llm.ensure_pdf(body)  # raises if the PDF cannot be fetched
    paper = Paper(title=body.title, authors=body.authors, year=body.year, abstract=body.abstract, url=body.url)
    return _insert_with_pdf(db, paper, data)


@router.post("/upload", response_model=PaperOut, status_code=201)
def upload_paper(file: UploadFile = File(...), db: Session = Depends(get_db)):
    data = file.file.read(pdf.MAX_PDF_BYTES + 1)
    if len(data) > pdf.MAX_PDF_BYTES:
        raise AppError(413, f"The file is larger than {pdf.MAX_PDF_BYTES // (1024 * 1024)} MB.")
    if not pdf.is_pdf(data):
        raise pdf.PdfError("The uploaded file is not a PDF.")
    first_text, pdf_meta = pdf.read_upload(data)
    meta = llm.extract_metadata(first_text, pdf_meta)
    paper = Paper(title=meta.title, authors=meta.authors, year=meta.year, abstract=meta.abstract)
    return _insert_with_pdf(db, paper, data)
