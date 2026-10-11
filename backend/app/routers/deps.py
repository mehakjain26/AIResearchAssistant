from sqlalchemy.orm import Session

from ..errors import AppError
from ..models import Paper


def get_paper_or_404(db: Session, paper_id: int) -> Paper:
    paper = db.get(Paper, paper_id)
    if paper is None:
        raise AppError(404, "Paper not found.")
    return paper
