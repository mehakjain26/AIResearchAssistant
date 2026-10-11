from fastapi import APIRouter, Query

from ..schemas import SearchResult
from ..services import arxiv

router = APIRouter(prefix="/api")


@router.get("/search", response_model=list[SearchResult])
def search(q: str = Query(min_length=1)):
    return arxiv.search(q)
