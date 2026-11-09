"""Backend tests. The save/download tests need an S3-compatible store at S3_ENDPOINT
and internet access to arxiv.org. LLM-dependent calls are not exercised here.

Covers:
- arXiv Atom XML parsing, query construction, stop-word fallback, rate-limit handling, and live search
- PDF download rules (valid PDFs, non-PDF rejection, SSRF/internal IP blocking, size caps, scanned PDF rejection)
- S3 object storage (`storage.py`) put/get/delete round-trips and bucket isolation
- In-memory LRU PDF cache (`cache.py`) byte-limit eviction
- FastAPI endpoints (`/api/search`, `/api/papers`, `/api/papers/upload`, `/api/summarize`, `/api/ask`)
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas import SearchResult
from helpers import make_pdf

from app.errors import AppError
from app.services import arxiv, cache, pdf, storage

# URL of "Attention Is All You Need" on arXiv used for live download/extraction tests
ARXIV_PDF = "https://arxiv.org/pdf/1706.03762"


@pytest.fixture(scope="module")
def client():
    """Creates a FastAPI `TestClient` once for this test module.

    Entering the `with TestClient(app)` context triggers FastAPI's startup lifespan hook,
    which creates the SQLite tables and ensures the test S3 bucket exists.
    """
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def attention_pdf():
    """Downloads the real 'Attention Is All You Need' PDF once per module so multiple
    tests can reuse its bytes without re-downloading from arXiv.
    """
    return pdf.download_pdf(ARXIV_PDF)


# Canned arXiv Atom XML response containing two entries:
# 1. A complete modern entry with multiline title/abstract, multiple authors, and version suffix (`v7`)
# 2. A minimal legacy entry (`cs/0112017v1`) missing published date, authors, and summary text
SAMPLE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <published>2017-06-12T17:57:34Z</published>
    <title>Attention Is All
      You Need</title>
    <summary>  The dominant sequence
      transduction models.  </summary>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/cs/0112017v1</id>
    <title>Old style id</title>
    <summary></summary>
  </entry>
</feed>"""


def test_arxiv_parsing():
    """Verifies that `arxiv.parse` normalizes Atom XML into `SearchResult` objects:
    collapsing internal whitespace, extracting author lists and publication year,
    stripping version suffixes (`v7` -> `1706.03762`), and constructing `url` and `pdf_url`.
    """
    r = arxiv.parse(SAMPLE_ATOM)
    assert r[0].title == "Attention Is All You Need"  # whitespace normalized
    assert r[0].authors == ["Ashish Vaswani", "Noam Shazeer"]
    assert r[0].year == 2017
    assert r[0].abstract == "The dominant sequence transduction models."
    assert r[0].external_id == "1706.03762"  # version stripped
    assert r[0].url == "https://arxiv.org/abs/1706.03762"
    assert r[0].pdf_url == "https://arxiv.org/pdf/1706.03762"
    assert r[1].external_id == "cs/0112017" and r[1].year is None and r[1].abstract is None


def test_arxiv_query_building():
    """Verifies how user search strings are converted into arXiv API query syntax:
    - `_phrase_query` strips punctuation and wraps the words in an exact `all:"..."` phrase.
    - `_terms_query` removes common stop words ("is", "all", "you") and joins remaining words with `AND`.
    - If a query consists solely of stop words ("the of"), it keeps them rather than sending an empty query.
    """
    assert arxiv._phrase_query("graph attention, networks!") == 'all:"graph attention networks"'
    assert arxiv._terms_query("attention is all you need") == "all:attention AND all:need"
    assert arxiv._terms_query("the of") == "all:the AND all:of"  # all stop words: keep them rather than send nothing
    assert arxiv._phrase_query("  ?! ") == ""


def test_arxiv_falls_back_to_word_search(monkeypatch):
    """Verifies the two-step search strategy in `arxiv.search`:
    1. First tries an exact phrase query (`all:"networks for graph"`).
    2. When that returns no matches (`[]`), automatically retries with an `AND` word query
       excluding stop words (`all:networks AND all:graph`).
    """
    sent = []

    def fetch(query, limit):
        sent.append(query)
        return [] if query.startswith('all:"') else ["hit"]

    monkeypatch.setattr(arxiv, "_fetch", fetch)
    assert arxiv.search("networks for graph") == ["hit"]
    assert sent == ['all:"networks for graph"', "all:networks AND all:graph"]


def test_arxiv_rate_limit_is_readable(monkeypatch):
    """Verifies that if arXiv responds with HTTP 429 (Too Many Requests), `arxiv.search`
    raises an `AppError` with status 429 and a user-friendly rate-limit message.
    """
    monkeypatch.setattr(arxiv, "MIN_INTERVAL", 0)
    monkeypatch.setattr(arxiv.httpx, "get", lambda *a, **k: type("R", (), {"status_code": 429})())
    with pytest.raises(AppError) as e:
        arxiv.search("x")
    assert e.value.status == 429 and "rate limit" in e.value.message


def test_live_arxiv_search_returns_downloadable_papers(client):
    """Live integration smoke test for `GET /api/search`: queries the real arXiv API
    for 'attention is all you need' and verifies that results include titles and PDF URLs.
    Skips gracefully if arXiv is currently rate-limiting the test runner.
    """
    r = client.get("/api/search", params={"q": "attention is all you need"})
    if r.status_code == 429:
        pytest.skip("arXiv rate limit reached; not a bug in the app")
    assert r.status_code == 200, r.text
    results = r.json()
    assert results and all(p["pdf_url"] and p["title"] for p in results)
    assert any("Attention Is All You Need" in p["title"] for p in results)


def test_download_and_extract(attention_pdf):
    """Verifies that `attention_pdf` starts with the `%PDF-` magic header and that both
    `pdf.extract_text` (full paper text) and `pdf.read_upload` (first pages + metadata)
    extract expected text from the PDF.
    """
    assert attention_pdf[:5] == b"%PDF-"
    assert "Transformer" in pdf.extract_text(attention_pdf)
    text, meta = pdf.read_upload(attention_pdf)
    assert "Attention" in text


def test_download_rejects_non_pdf():
    """Verifies that `pdf.download_pdf` rejects a URL that returns HTML instead of a PDF
    (such as an arXiv `/abs/` abstract landing page).
    """
    with pytest.raises(pdf.PdfError):
        pdf.download_pdf("https://arxiv.org/abs/1706.03762")


def test_scanned_pdf_has_no_text():
    """Verifies that a blank/scanned PDF with no extractable text (`make_pdf()`) is rejected
    by `extract_text`, `check_has_text`, and `read_upload` with `PdfError`.
    """
    with pytest.raises(pdf.PdfError):
        pdf.extract_text(make_pdf())
    with pytest.raises(pdf.PdfError):
        pdf.check_has_text(make_pdf())
    with pytest.raises(pdf.PdfError):
        pdf.read_upload(make_pdf())


def test_storage_round_trip():
    """Verifies that `storage.put_pdf`, `storage.get_pdf`, and `storage.delete_pdf` can
    upload, retrieve, and remove raw PDF bytes in SeaweedFS (S3).
    """
    storage.put_pdf(999, b"%PDF-1.4 hello")
    assert storage.get_pdf(999) == b"%PDF-1.4 hello"
    storage.delete_pdf(999)


def test_save_uses_cache_and_persists(client, attention_pdf):
    """Verifies `POST /api/papers` when the paper's PDF is already in the in-memory cache:
    1. Reuses the cached PDF without calling OpenAI.
    2. Inserts the paper metadata into SQLite and stores the PDF in S3 under `papers/{id}.pdf`.
    3. Appears in `GET /api/papers/{id}` and `GET /api/papers`.
    4. Allows saving duplicate entries, ordered newest first (`id` descending).
    """
    paper = SearchResult(title="Attention Is All You Need", authors=["A. Vaswani"], year=2017,
                         abstract="abs", url="https://s2/a", pdf_url=ARXIV_PDF, external_id="cache-test")
    cache.put(cache.cache_key(paper), attention_pdf)
    r = client.post("/api/papers", json=paper.model_dump())
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert client.get(f"/api/papers/{pid}").json()["title"] == paper.title
    assert pid in [p["id"] for p in client.get("/api/papers").json()]
    assert storage.get_pdf(pid) == attention_pdf
    # a second save creates a second entry (duplicates allowed), newest first
    pid2 = client.post("/api/papers", json=paper.model_dump()).json()["id"]
    assert client.get("/api/papers").json()[0]["id"] == pid2


def test_save_fails_cleanly_without_pdf(client):
    """Verifies that `POST /api/papers` does not insert a database row when the PDF cannot be obtained:
    - Returns 422 if the paper has neither `pdf_url` nor `url`.
    - Returns 503 if fetching requires the OpenAI tool loop but `OPENAI_API_KEY` is unconfigured.
    - Leaves the library count unchanged (`before == after`).
    """
    before = len(client.get("/api/papers").json())
    r = client.post("/api/papers", json={"title": "No links"})
    assert r.status_code == 422 and "no link" in r.json()["detail"]
    r = client.post("/api/papers", json={"title": "Needs LLM", "pdf_url": "https://example.com/x.pdf"})
    assert r.status_code == 503 and "OPENAI_API_KEY" in r.json()["detail"]
    assert len(client.get("/api/papers").json()) == before  # nothing saved


def test_summarize_and_ask_validation(client):
    """Verifies request validation and error codes on `POST /api/summarize` and `POST /api/ask`:
    - 422 when neither `paper_id` nor `paper` is provided.
    - 422 when both `paper_id` and `paper` are provided simultaneously.
    - 404 when `paper_id` does not exist in SQLite.
    - 503 when `OPENAI_API_KEY` is not set.
    """
    assert client.post("/api/summarize", json={}).status_code == 422
    assert client.post("/api/summarize", json={"paper_id": 1, "paper": {"title": "x"}}).status_code == 422
    assert client.post("/api/summarize", json={"paper_id": 424242}).status_code == 404
    pid = client.get("/api/papers").json()[0]["id"]
    r = client.post("/api/ask", json={"paper_id": pid, "question": "What datasets?"})
    assert r.status_code == 503 and "OPENAI_API_KEY" in r.json()["detail"]


def test_upload_rejects_non_pdf(client):
    """Verifies that `POST /api/papers/upload` rejects non-PDF uploads with HTTP 422."""
    r = client.post("/api/papers/upload", files={"file": ("a.txt", b"hello", "text/plain")})
    assert r.status_code == 422


def test_tests_use_their_own_bucket():
    """Safety check: verifies that `conftest.py` isolated S3 storage to a `test-*` bucket
    so running tests never overwrites or deletes real library PDFs.
    """
    from app.config import settings
    assert settings.s3_bucket.startswith("test-")


@pytest.mark.parametrize("url", [
    "http://localhost:8333/", "http://127.0.0.1/x.pdf", "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/a.pdf", "http://[::1]/a.pdf", "file:///etc/passwd", "ftp://example.com/a.pdf",
])
def test_download_refuses_internal_and_non_http_links(url):
    """SSRF security check: verifies that `pdf.download_pdf` blocks loopback addresses,
    private network IPs, cloud metadata endpoints (`169.254.169.254`), IPv6 localhost,
    and non-HTTP(S) schemes (`file://`, `ftp://`).
    """
    with pytest.raises(pdf.PdfError):
        pdf.download_pdf(url)


def test_download_size_cap(monkeypatch):
    """Verifies that `pdf.download_pdf` aborts and raises `PdfError` if a remote PDF stream
    exceeds `MAX_PDF_BYTES`.
    """
    monkeypatch.setattr(pdf, "MAX_PDF_BYTES", 1000)
    with pytest.raises(pdf.PdfError, match="larger"):
        pdf.download_pdf(ARXIV_PDF)


def test_upload_size_cap(client, monkeypatch):
    """Verifies that `POST /api/papers/upload` rejects uploaded files larger than `MAX_PDF_BYTES`
    with HTTP 413 (Payload Too Large).
    """
    monkeypatch.setattr(pdf, "MAX_PDF_BYTES", 100)
    r = client.post("/api/papers/upload", files={"file": ("a.pdf", b"%PDF-" + b"x" * 500, "application/pdf")})
    assert r.status_code == 413


def test_missing_stored_pdf_is_404_not_outage(client):
    """Verifies that if a paper row exists in SQLite but its corresponding PDF object is missing
    from S3 storage, `POST /api/summarize` returns HTTP 404 (not 500 or 503).
    """
    from app.db import SessionLocal
    from app.models import Paper
    with SessionLocal() as db:
        row = Paper(title="orphan", authors=[])
        db.add(row)
        db.commit()
        pid = row.id
    r = client.post("/api/summarize", json={"paper_id": pid})
    assert r.status_code == 404 and "missing" in r.json()["detail"]


def test_read_upload_opens_once_and_returns_first_pages():
    """Verifies that `pdf.read_upload` extracts the text of the first pages along with the PDF's
    embedded metadata dict, and `pdf.check_has_text` succeeds when text is present.
    """
    text, meta = pdf.read_upload(make_pdf("Title of the paper"))
    assert "Title of the paper" in text and isinstance(meta, dict)
    pdf.check_has_text(make_pdf("x"))


def test_empty_question_is_rejected_by_the_schema(client):
    """Verifies that `POST /api/ask` rejects whitespace-only questions via Pydantic schema validation
    with HTTP 422 before touching the database or LLM.
    """
    r = client.post("/api/ask", json={"paper_id": 1, "question": "   "})
    assert r.status_code == 422


def test_over_length_paper_is_rejected_not_truncated(monkeypatch):
    """Verifies that `llm.summarize` counts tokens with `tiktoken` and raises HTTP 422 when the
    paper text exceeds `settings.max_input_tokens`, rather than silently truncating the paper.
    """
    from app.config import settings
    from app.services import llm
    monkeypatch.setattr(settings, "max_input_tokens", 5)
    with pytest.raises(AppError) as e:
        llm.summarize("one two three four five six seven eight nine ten")
    assert e.value.status == 422 and "too long" in e.value.message


def test_cache_is_bounded_by_bytes(monkeypatch):
    """Verifies that the in-memory LRU cache (`cache.py`) evicts the oldest entry (`size-a`)
    when inserting a new entry (`size-b`) would cause total cached bytes to exceed `MAX_BYTES`.
    """
    monkeypatch.setattr(cache, "MAX_BYTES", 100)
    cache.put("size-a", b"a" * 60)
    cache.put("size-b", b"b" * 60)
    assert cache.get("size-a") is None and cache.get("size-b") is not None


def test_stored_pdf_is_served_inline(client):
    """Verifies that `GET /api/papers/{id}/pdf` streams the stored PDF from S3 with
    `Content-Type: application/pdf` and `Content-Disposition: inline` so browsers open it
    directly, and returns 404 for unknown paper IDs.
    """
    from app.db import SessionLocal
    from app.models import Paper
    pdf_bytes = make_pdf("uploaded paper")
    with SessionLocal() as db:
        row = Paper(title="uploaded", authors=[])
        db.add(row)
        db.commit()
        pid = row.id
    storage.put_pdf(pid, pdf_bytes)
    r = client.get(f"/api/papers/{pid}/pdf")
    assert r.status_code == 200 and r.content == pdf_bytes
    assert r.headers["content-type"] == "application/pdf" and "inline" in r.headers["content-disposition"]
    assert client.get("/api/papers/424242/pdf").status_code == 404


def test_delete_removes_row_and_stored_pdf(client):
    """Verifies that `DELETE /api/papers/{id}`:
    1. Deletes the paper row from SQLite and returns HTTP 204.
    2. Deletes the corresponding `papers/{id}.pdf` object from S3 storage.
    3. Subsequent `GET` or `DELETE` requests for that ID return HTTP 404.
    """
    from app.db import SessionLocal
    from app.models import Paper
    with SessionLocal() as db:
        row = Paper(title="to delete", authors=[])
        db.add(row)
        db.commit()
        pid = row.id
    storage.put_pdf(pid, make_pdf("bye"))
    assert client.delete(f"/api/papers/{pid}").status_code == 204
    assert client.get(f"/api/papers/{pid}").status_code == 404
    assert pid not in [p["id"] for p in client.get("/api/papers").json()]
    with pytest.raises(AppError) as e:
        storage.get_pdf(pid)  # the stored PDF is gone too
    assert e.value.status == 404
    assert client.delete(f"/api/papers/{pid}").status_code == 404  # already gone
