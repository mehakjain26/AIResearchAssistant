from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator


class PaperOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    authors: list[str]
    year: int | None = None
    abstract: str | None = None
    url: str | None = None


class SearchResult(BaseModel):
    """A paper as returned by search. pdf_url and external_id are not stored."""

    title: str
    authors: list[str] = []
    year: int | None = None
    abstract: str | None = None
    url: str | None = None
    pdf_url: str | None = None
    external_id: str | None = None


class PaperMetadata(BaseModel):
    """Structured output requested from the LLM for uploaded PDFs."""

    title: str
    authors: list[str]
    year: int | None
    abstract: str | None


class _PaperTarget(BaseModel):
    """Identifies a paper: a saved one by id, or an unsaved search result."""

    paper_id: int | None = None
    paper: SearchResult | None = None

    @model_validator(mode="after")
    def exactly_one(self):
        if (self.paper_id is None) == (self.paper is None):
            raise ValueError("Provide exactly one of paper_id or paper")
        return self


class SummarizeRequest(_PaperTarget):
    pass


class AskRequest(_PaperTarget):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SummaryOut(BaseModel):
    summary: str


class AnswerOut(BaseModel):
    answer: str
