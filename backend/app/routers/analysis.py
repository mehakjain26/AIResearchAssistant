from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas import AnswerOut, AskRequest, SummarizeRequest, SummaryOut
from ..services import llm, pdf, storage
from .deps import get_paper_or_404

router = APIRouter(prefix="/api")


def _full_text(body: SummarizeRequest | AskRequest, db: Session) -> str:
    """Full text of the target paper: from the object store if saved, else fetched via the tool call."""
    if body.paper_id is not None:
        get_paper_or_404(db, body.paper_id)
        data = storage.get_pdf(body.paper_id)
    else:
        data = llm.ensure_pdf(body.paper)
    return pdf.extract_text(data)


@router.post("/summarize", response_model=SummaryOut)
def summarize(body: SummarizeRequest, db: Session = Depends(get_db)):
    return SummaryOut(summary=llm.summarize(_full_text(body, db)))


@router.post("/ask", response_model=AnswerOut)
def ask(body: AskRequest, db: Session = Depends(get_db)):
    return AnswerOut(answer=llm.answer(_full_text(body, db), body.question))
