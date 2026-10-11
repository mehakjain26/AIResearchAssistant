"""Backend tests. The save/download tests need an S3-compatible store at S3_ENDPOINT
and internet access to arxiv.org. LLM-dependent calls are not exercised here."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas import SearchResult
from helpers import make_pdf

from app.errors import AppError
from app.services import arxiv, cache, pdf, storage

ARXIV_PDF = "https://arxiv.org/pdf/1706.03762"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def attention_pdf():
    return pdf.download_pdf(ARXIV_PDF)


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
    assert arxiv._phrase_query("graph attention, networks!") == 'all:"graph attention networks"'
    assert arxiv._terms_query("attention is all you need") == "all:attention AND all:need"
    assert arxiv._terms_query("the of") == "all:the AND all:of"  # all stop words: keep them rather than send nothing
    assert arxiv._phrase_query("  ?! ") == ""


def test_arxiv_falls_back_to_word_search(monkeypatch):
    sent = []

    def fetch(query, limit):
        sent.append(query)
        return [] if query.startswith('all:"') else ["hit"]

    monkeypatch.setattr(arxiv, "_fetch", fetch)
    assert arxiv.search("networks for graph") == ["hit"]
    assert sent == ['all:"networks for graph"', "all:networks AND all:graph"]


def test_arxiv_rate_limit_is_readable(monkeypatch):
    monkeypatch.setattr(arxiv, "MIN_INTERVAL", 0)
    monkeypatch.setattr(arxiv.httpx, "get", lambda *a, **k: type("R", (), {"status_code": 429})())
    with pytest.raises(AppError) as e:
        arxiv.search("x")
    assert e.value.status == 429 and "rate limit" in e.value.message


def test_live_arxiv_search_returns_downloadable_papers(client):
    r = client.get("/api/search", params={"q": "attention is all you need"})
    assert r.status_code == 200, r.text
    results = r.json()
    assert results and all(p["pdf_url"] and p["title"] for p in results)
    assert any("Attention Is All You Need" in p["title"] for p in results)


def test_download_and_extract(attention_pdf):
    assert attention_pdf[:5] == b"%PDF-"
    assert "Transformer" in pdf.extract_text(attention_pdf)
    text, meta = pdf.read_upload(attention_pdf)
    assert "Attention" in text


def test_download_rejects_non_pdf():
    with pytest.raises(pdf.PdfError):
        pdf.download_pdf("https://arxiv.org/abs/1706.03762")


def test_scanned_pdf_has_no_text():
    with pytest.raises(pdf.PdfError):
        pdf.extract_text(make_pdf())
    with pytest.raises(pdf.PdfError):
        pdf.check_has_text(make_pdf())
    with pytest.raises(pdf.PdfError):
        pdf.read_upload(make_pdf())


def test_storage_round_trip():
    storage.put_pdf(999, b"%PDF-1.4 hello")
    assert storage.get_pdf(999) == b"%PDF-1.4 hello"
    storage.delete_pdf(999)


def test_save_uses_cache_and_persists(client, attention_pdf):
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
    before = len(client.get("/api/papers").json())
    r = client.post("/api/papers", json={"title": "No links"})
    assert r.status_code == 422 and "no link" in r.json()["detail"]
    r = client.post("/api/papers", json={"title": "Needs LLM", "pdf_url": "https://example.com/x.pdf"})
    assert r.status_code == 503 and "OPENAI_API_KEY" in r.json()["detail"]
    assert len(client.get("/api/papers").json()) == before  # nothing saved


def test_summarize_and_ask_validation(client):
    assert client.post("/api/summarize", json={}).status_code == 422
    assert client.post("/api/summarize", json={"paper_id": 1, "paper": {"title": "x"}}).status_code == 422
    assert client.post("/api/summarize", json={"paper_id": 424242}).status_code == 404
    pid = client.get("/api/papers").json()[0]["id"]
    r = client.post("/api/ask", json={"paper_id": pid, "question": "What datasets?"})
    assert r.status_code == 503 and "OPENAI_API_KEY" in r.json()["detail"]


def test_upload_rejects_non_pdf(client):
    r = client.post("/api/papers/upload", files={"file": ("a.txt", b"hello", "text/plain")})
    assert r.status_code == 422


def test_tests_use_their_own_bucket():
    from app.config import settings
    assert settings.s3_bucket.startswith("test-")


@pytest.mark.parametrize("url", [
    "http://localhost:8333/", "http://127.0.0.1/x.pdf", "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/a.pdf", "http://[::1]/a.pdf", "file:///etc/passwd", "ftp://example.com/a.pdf",
])
def test_download_refuses_internal_and_non_http_links(url):
    with pytest.raises(pdf.PdfError):
        pdf.download_pdf(url)


def test_download_size_cap(monkeypatch):
    monkeypatch.setattr(pdf, "MAX_PDF_BYTES", 1000)
    with pytest.raises(pdf.PdfError, match="larger"):
        pdf.download_pdf(ARXIV_PDF)


def test_upload_size_cap(client, monkeypatch):
    monkeypatch.setattr(pdf, "MAX_PDF_BYTES", 100)
    r = client.post("/api/papers/upload", files={"file": ("a.pdf", b"%PDF-" + b"x" * 500, "application/pdf")})
    assert r.status_code == 413


def test_missing_stored_pdf_is_404_not_outage(client):
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
    text, meta = pdf.read_upload(make_pdf("Title of the paper"))
    assert "Title of the paper" in text and isinstance(meta, dict)
    pdf.check_has_text(make_pdf("x"))


def test_empty_question_is_rejected_by_the_schema(client):
    r = client.post("/api/ask", json={"paper_id": 1, "question": "   "})
    assert r.status_code == 422


def test_over_length_paper_is_rejected_not_truncated(monkeypatch):
    from app.config import settings
    from app.services import llm
    monkeypatch.setattr(settings, "max_input_tokens", 5)
    with pytest.raises(AppError) as e:
        llm.summarize("one two three four five six seven eight nine ten")
    assert e.value.status == 422 and "too long" in e.value.message


def test_cache_is_bounded_by_bytes(monkeypatch):
    monkeypatch.setattr(cache, "MAX_BYTES", 100)
    cache.put("size-a", b"a" * 60)
    cache.put("size-b", b"b" * 60)
    assert cache.get("size-a") is None and cache.get("size-b") is not None


def test_stored_pdf_is_served_inline(client):
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
