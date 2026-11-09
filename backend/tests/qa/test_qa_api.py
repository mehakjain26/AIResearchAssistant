"""HTTP-level tests of every API route: happy paths, edge cases and error handling."""
import logging

import httpx
import openai
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as SASession, sessionmaker

from fastapi.testclient import TestClient

from app import db as db_module
from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.services import cache, llm, pdf, storage
from qa_support import ATTENTION_ENTRY, atom_feed, make_pdf

SEARCH_RESULT = {
    "title": "Attention Is All You Need",
    "authors": ["Ashish Vaswani", "Noam Shazeer"],
    "year": 2017,
    "abstract": "The dominant sequence transduction models.",
    "url": "https://arxiv.org/abs/1706.03762",
    "pdf_url": "https://arxiv.org/pdf/1706.03762",
    "external_id": "1706.03762",
}


def stored_pdf(s3, paper_id: int) -> dict:
    return s3.get_object(Bucket=settings.s3_bucket, Key=f"papers/{paper_id}.pdf")


# --------------------------------------------------------------------------- search

class TestSearch:
    def test_returns_parsed_arxiv_results(self, client, fake_arxiv):
        fake_arxiv.queue(200, atom_feed(ATTENTION_ENTRY))
        r = client.get("/api/search", params={"q": "attention is all you need"})
        assert r.status_code == 200
        assert r.json() == [SEARCH_RESULT]
        req = fake_arxiv.requests[0]
        assert req["url"] == "https://export.arxiv.org/api/query"
        assert req["params"]["search_query"] == 'all:"attention is all you need"'
        assert req["params"]["max_results"] == 10
        assert req["headers"]["User-Agent"].startswith("AIResearchAssistant/")

    def test_request_has_a_timeout_and_follows_redirects(self, client, fake_arxiv):
        fake_arxiv.queue(200, atom_feed(ATTENTION_ENTRY))
        client.get("/api/search", params={"q": "attention"})
        options = fake_arxiv.requests[0]["options"]
        assert 0 < options["timeout"] <= 60  # a stuck arXiv must not hang the request forever
        assert options["follow_redirects"] is True

    def test_falls_back_to_any_order_word_search_when_phrase_has_no_hits(self, client, fake_arxiv):
        fake_arxiv.queue(200, atom_feed()).queue(200, atom_feed(ATTENTION_ENTRY))
        r = client.get("/api/search", params={"q": "need attention"})
        assert [p["external_id"] for p in r.json()] == ["1706.03762"]
        assert fake_arxiv.queries == ['all:"need attention"', "all:need AND all:attention"]
        assert [req["params"]["max_results"] for req in fake_arxiv.requests] == [10, 10]

    def test_no_results_from_either_query_is_an_empty_list(self, client, fake_arxiv):
        fake_arxiv.queue(200, atom_feed()).queue(200, atom_feed())
        r = client.get("/api/search", params={"q": "zzqqxx"})
        assert r.status_code == 200 and r.json() == []
        assert len(fake_arxiv.requests) == 2

    def test_punctuation_only_query_returns_nothing_without_calling_arxiv(self, client, fake_arxiv):
        r = client.get("/api/search", params={"q": "?!  ..."})
        assert r.status_code == 200 and r.json() == []
        assert fake_arxiv.requests == []

    @pytest.mark.parametrize("params", [{}, {"q": ""}])
    def test_missing_or_empty_query_is_rejected(self, client, fake_arxiv, params):
        r = client.get("/api/search", params=params)
        assert r.status_code == 422
        assert fake_arxiv.requests == []

    def test_very_long_query_is_sent_as_one_phrase(self, client, fake_arxiv):
        q = " ".join(["word"] * 500)
        fake_arxiv.queue(200, atom_feed(ATTENTION_ENTRY))
        assert client.get("/api/search", params={"q": q}).status_code == 200
        assert fake_arxiv.queries[0] == f'all:"{q}"'

    def test_entries_without_an_id_are_skipped(self, client, fake_arxiv):
        fake_arxiv.queue(200, atom_feed({"id": "", "title": "Broken"}, ATTENTION_ENTRY))
        assert [p["title"] for p in client.get("/api/search", params={"q": "x"}).json()] == [
            "Attention Is All You Need"]

    def test_rate_limit_is_reported_as_429(self, client, fake_arxiv):
        fake_arxiv.queue(429)
        r = client.get("/api/search", params={"q": "graphs"})
        assert r.status_code == 429
        assert r.json()["detail"] == "arXiv rate limit reached. Wait a few seconds and try again."

    def test_arxiv_server_error_is_a_502(self, client, fake_arxiv):
        fake_arxiv.queue(503)
        r = client.get("/api/search", params={"q": "graphs"})
        assert r.status_code == 502 and r.json()["detail"] == "arXiv returned HTTP 503"

    def test_network_failure_is_a_502(self, client, fake_arxiv):
        fake_arxiv.queue_error(httpx.ConnectError("connection refused"))
        r = client.get("/api/search", params={"q": "graphs"})
        assert r.status_code == 502 and r.json()["detail"].startswith("Could not reach arXiv")

    def test_malformed_xml_is_a_502(self, client, fake_arxiv):
        fake_arxiv.queue(200, "<feed><entry>")
        r = client.get("/api/search", params={"q": "graphs"})
        assert r.status_code == 502 and r.json()["detail"] == "arXiv returned an unreadable response."

    @pytest.mark.xfail(strict=True, reason="Bug B1: non-ASCII letters split words ('naïve' -> 'na ve').")
    def test_non_ascii_words_are_kept_whole(self, client, fake_arxiv):
        fake_arxiv.queue(200, atom_feed(ATTENTION_ENTRY))
        client.get("/api/search", params={"q": "naïve bayes"})
        assert fake_arxiv.queries[0] == 'all:"naïve bayes"'

    @pytest.mark.xfail(strict=True, reason="Bug B1: a query with no Latin letters becomes empty, so arXiv is never asked.")
    def test_non_latin_query_is_sent_to_arxiv(self, client, fake_arxiv):
        fake_arxiv.queue(200, atom_feed(ATTENTION_ENTRY))
        r = client.get("/api/search", params={"q": "机器学习"})
        assert len(fake_arxiv.requests) == 1 and r.json() != []


# --------------------------------------------------------------------------- library: list / get

class TestListAndGet:
    def test_empty_library(self, client):
        r = client.get("/api/papers")
        assert r.status_code == 200 and r.json() == []

    def test_lists_newest_first_with_all_fields(self, client, seed):
        first = seed("First", authors=["A"], year=2020, abstract="a", url="https://x/1")
        second = seed("Second")
        body = client.get("/api/papers").json()
        assert [p["id"] for p in body] == [second, first]
        assert body[1] == {"id": first, "title": "First", "authors": ["A"], "year": 2020,
                           "abstract": "a", "url": "https://x/1"}
        assert body[0] == {"id": second, "title": "Second", "authors": [], "year": None,
                           "abstract": None, "url": None}

    def test_get_one(self, client, seed):
        pid = seed("Graph Attention Networks", authors=["Petar Veličković"], year=2018)
        r = client.get(f"/api/papers/{pid}")
        assert r.status_code == 200
        assert r.json()["title"] == "Graph Attention Networks"
        assert r.json()["authors"] == ["Petar Veličković"]  # non-ASCII round-trips

    @pytest.mark.parametrize("pid", [0, -1, 999999])
    def test_unknown_id_is_404(self, client, pid):
        r = client.get(f"/api/papers/{pid}")
        assert r.status_code == 404 and r.json() == {"detail": "Paper not found."}

    def test_non_integer_id_is_422(self, client):
        assert client.get("/api/papers/abc").status_code == 422


# --------------------------------------------------------------------------- library: save a search result

class TestSave:
    def test_save_fetches_pdf_stores_it_and_persists_row(self, client, fake_openai, downloads, s3,
                                                         paper_rows, text_pdf):
        downloads.files[SEARCH_RESULT["pdf_url"]] = text_pdf
        r = client.post("/api/papers", json=SEARCH_RESULT)
        assert r.status_code == 201
        body = r.json()
        assert body == {"id": body["id"], **{k: SEARCH_RESULT[k] for k in ("title", "authors", "year", "abstract", "url")}}
        assert "pdf_url" not in body and "external_id" not in body

        [row] = paper_rows()
        assert (row.id, row.title, row.authors) == (body["id"], SEARCH_RESULT["title"], SEARCH_RESULT["authors"])
        obj = stored_pdf(s3, row.id)
        assert obj["Body"].read() == text_pdf and obj["ContentType"] == "application/pdf"
        assert downloads.calls == [SEARCH_RESULT["pdf_url"]]
        assert cache.get("1706.03762") == text_pdf  # later summaries of the unsaved result reuse it

    def test_save_uses_cached_pdf_without_calling_the_model(self, client, fake_openai, downloads, s3, text_pdf):
        cache.put("1706.03762", text_pdf)
        r = client.post("/api/papers", json=SEARCH_RESULT)
        assert r.status_code == 201
        assert fake_openai.calls == [] and downloads.calls == []
        assert stored_pdf(s3, r.json()["id"])["Body"].read() == text_pdf

    def test_saving_twice_creates_two_entries(self, client, fake_openai, downloads, text_pdf, paper_rows):
        downloads.files[SEARCH_RESULT["pdf_url"]] = text_pdf
        a = client.post("/api/papers", json=SEARCH_RESULT).json()["id"]
        b = client.post("/api/papers", json=SEARCH_RESULT).json()["id"]
        assert a != b and [p.id for p in paper_rows()] == [a, b]

    def test_minimal_result_with_only_title_and_link(self, client, fake_openai, downloads, text_pdf):
        downloads.files["https://host/p.pdf"] = text_pdf
        r = client.post("/api/papers", json={"title": "Only a title", "pdf_url": "https://host/p.pdf"})
        assert r.status_code == 201
        assert r.json()["authors"] == [] and r.json()["year"] is None and r.json()["abstract"] is None

    def test_numeric_string_year_is_coerced(self, client, fake_openai, downloads, text_pdf):
        downloads.files[SEARCH_RESULT["pdf_url"]] = text_pdf
        r = client.post("/api/papers", json={**SEARCH_RESULT, "year": "2017"})
        assert r.status_code == 201 and r.json()["year"] == 2017

    @pytest.mark.parametrize("body", [{}, {"authors": ["x"]}, {"title": "t", "year": "not a year"},
                                      {"title": "t", "authors": "not a list"}])
    def test_invalid_body_is_422_and_nothing_saved(self, client, paper_rows, body):
        assert client.post("/api/papers", json=body).status_code == 422
        assert paper_rows() == []

    def test_result_without_any_link_is_422(self, client, paper_rows, stored_keys):
        r = client.post("/api/papers", json={"title": "No links"})
        assert r.status_code == 422 and r.json()["detail"] == "This paper has no link to download a PDF from."
        assert paper_rows() == [] and stored_keys() == []

    def test_missing_api_key_is_503(self, client, paper_rows):
        r = client.post("/api/papers", json=SEARCH_RESULT)
        assert r.status_code == 503 and "OPENAI_API_KEY" in r.json()["detail"]
        assert paper_rows() == []

    def test_unfetchable_pdf_is_422_and_nothing_saved(self, client, fake_openai, downloads, paper_rows, stored_keys):
        r = client.post("/api/papers", json=SEARCH_RESULT)  # every download 404s
        assert r.status_code == 422
        assert r.json()["detail"] == "Could not fetch the paper's PDF: server returned HTTP 404."
        assert paper_rows() == [] and stored_keys() == []
        assert cache.get("1706.03762") is None  # failures are not cached

    @pytest.mark.xfail(strict=True, reason="Known defect: an invalid port raises an uncaught ValueError (500).")
    def test_link_with_invalid_port_is_422_not_a_crash(self, lenient_client, fake_openai, paper_rows):
        r = lenient_client.post("/api/papers", json={"title": "t", "pdf_url": "http://example.com:99999/a.pdf"})
        assert paper_rows() == []
        assert r.status_code == 422

    def test_openai_outage_is_502(self, client, fake_openai, paper_rows):
        fake_openai.error = openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com"))
        r = client.post("/api/papers", json=SEARCH_RESULT)
        assert r.status_code == 502 and r.json()["detail"].startswith("OpenAI request failed")
        assert paper_rows() == []

    def test_storage_outage_rolls_back_the_row(self, client, fake_openai, downloads, s3, text_pdf, paper_rows):
        downloads.files[SEARCH_RESULT["pdf_url"]] = text_pdf
        storage.ensure_bucket()
        s3.delete_bucket(Bucket=settings.s3_bucket)  # the store "goes away" after startup
        r = client.post("/api/papers", json=SEARCH_RESULT)
        assert r.status_code == 503 and r.json()["detail"].startswith("Object storage is unavailable")
        assert paper_rows() == []  # no row pointing at a PDF that does not exist

    def test_database_commit_failure_removes_the_uploaded_pdf(self, lenient_client, fake_openai, downloads,
                                                              text_pdf, paper_rows, stored_keys, monkeypatch):
        downloads.files[SEARCH_RESULT["pdf_url"]] = text_pdf

        def broken_commit(self):
            raise OperationalError("COMMIT", {}, Exception("database is locked"))

        with monkeypatch.context() as m:
            m.setattr(SASession, "commit", broken_commit)
            r = lenient_client.post("/api/papers", json=SEARCH_RESULT)
        assert r.status_code == 500
        assert paper_rows() == [] and stored_keys() == []  # the PDF put before the commit was cleaned up

    def test_unreachable_storage_is_503_and_nothing_saved(self, client, fake_openai, downloads, text_pdf,
                                                          paper_rows, storage_down):
        downloads.files[SEARCH_RESULT["pdf_url"]] = text_pdf
        r = client.post("/api/papers", json=SEARCH_RESULT)
        assert r.status_code == 503
        assert r.json()["detail"] == ('Object storage is unavailable: Could not connect to the endpoint URL: '
                                      '"http://storage:8333"')
        assert paper_rows() == []
        assert storage_down.calls[-1] == "delete_object"  # cleanup was attempted for the flushed id

    def test_database_failure_before_an_id_exists_touches_no_storage(self, lenient_client, fake_openai, downloads,
                                                                     text_pdf, paper_rows, monkeypatch):
        downloads.files[SEARCH_RESULT["pdf_url"]] = text_pdf
        storage_calls = []
        monkeypatch.setattr(storage, "put_pdf", lambda *a: storage_calls.append("put"))
        monkeypatch.setattr(storage, "delete_pdf", lambda *a: storage_calls.append("delete"))

        def broken_flush(self, *a, **k):
            raise OperationalError("INSERT", {}, Exception("disk I/O error"))

        with monkeypatch.context() as m:
            m.setattr(SASession, "flush", broken_flush)
            r = lenient_client.post("/api/papers", json=SEARCH_RESULT)
        assert r.status_code == 500
        assert storage_calls == []  # no id was assigned, so there is nothing to store or clean up
        assert paper_rows() == []


# --------------------------------------------------------------------------- library: upload a PDF

class TestUpload:
    def upload(self, client, data: bytes, name="paper.pdf", ctype="application/pdf"):
        return client.post("/api/papers/upload", files={"file": (name, data, ctype)})

    def test_upload_extracts_metadata_and_stores_pdf(self, client, fake_openai, s3, paper_rows):
        data = make_pdf("Deep Residual Learning", "Second page text", metadata={"title": "ResNet (pdf meta)"})
        fake_openai.metadata = fake_openai.metadata.model_copy(update={"title": "Deep Residual Learning"})
        r = self.upload(client, data)
        assert r.status_code == 201
        assert r.json() == {"id": r.json()["id"], "title": "Deep Residual Learning", "authors": ["Ada Lovelace"],
                            "year": 2024, "abstract": "Extracted abstract.", "url": None}
        [row] = paper_rows()
        assert row.title == "Deep Residual Learning"
        assert stored_pdf(s3, row.id)["Body"].read() == data

        prompt = fake_openai.calls_of("parse")[0]["messages"][0]["content"]
        assert "Deep Residual Learning" in prompt and "Second page text" in prompt
        assert "ResNet (pdf meta)" in prompt  # the PDF's own metadata is passed as a hint

    def test_only_the_first_two_pages_go_to_the_model(self, client, fake_openai):
        data = make_pdf("page one", "page two", "PAGE-THREE-MARKER")
        assert self.upload(client, data).status_code == 201
        prompt = fake_openai.calls_of("parse")[0]["messages"][0]["content"]
        assert "page two" in prompt and "PAGE-THREE-MARKER" not in prompt

    def test_pdf_with_leading_whitespace_is_accepted(self, client, fake_openai):
        assert self.upload(client, b"\n  " + make_pdf("hello")).status_code == 201

    def test_content_type_and_filename_are_not_trusted(self, client, fake_openai):
        assert self.upload(client, make_pdf("hello"), name="notes.txt", ctype="text/plain").status_code == 201

    @pytest.mark.parametrize("data", [b"", b"hello world", b"<html>%PDF-</html>"])
    def test_non_pdf_is_422_and_nothing_stored(self, client, fake_openai, paper_rows, stored_keys, data):
        r = self.upload(client, data)
        assert r.status_code == 422 and r.json()["detail"] == "The uploaded file is not a PDF."
        assert paper_rows() == [] and stored_keys() == [] and fake_openai.calls == []

    def test_corrupt_pdf_is_422(self, client, fake_openai, paper_rows):
        r = self.upload(client, b"%PDF-1.7\n garbage garbage")
        assert r.status_code == 422 and r.json()["detail"].startswith("could not read the PDF")
        assert paper_rows() == []

    def test_scanned_pdf_without_text_is_422(self, client, fake_openai, paper_rows):
        r = self.upload(client, make_pdf(None, None))
        assert r.status_code == 422 and "no extractable text" in r.json()["detail"]
        assert paper_rows() == [] and fake_openai.calls == []

    def test_too_large_message_states_the_limit_in_mb(self, client, fake_openai, monkeypatch, paper_rows):
        monkeypatch.setattr(pdf, "MAX_PDF_BYTES", 3 * 1024 * 1024)
        r = self.upload(client, b"%PDF-" + b"x" * (3 * 1024 * 1024))
        assert r.status_code == 413 and r.json()["detail"] == "The file is larger than 3 MB."
        assert paper_rows() == [] and fake_openai.calls == []

    def test_size_limit_boundary(self, client, fake_openai, monkeypatch):
        data = make_pdf("boundary")
        monkeypatch.setattr(pdf, "MAX_PDF_BYTES", len(data))
        assert self.upload(client, data).status_code == 201  # exactly at the limit is allowed
        monkeypatch.setattr(pdf, "MAX_PDF_BYTES", len(data) - 1)
        r = self.upload(client, data)
        assert r.status_code == 413 and "larger than" in r.json()["detail"]

    def test_missing_file_field_is_422(self, client, paper_rows):
        assert client.post("/api/papers/upload").status_code == 422
        assert paper_rows() == []

    def test_missing_api_key_is_503(self, client, paper_rows, stored_keys):
        r = self.upload(client, make_pdf("hello"))
        assert r.status_code == 503 and paper_rows() == [] and stored_keys() == []

    def test_model_returning_no_metadata_is_502(self, client, fake_openai, paper_rows):
        fake_openai.metadata = None
        r = self.upload(client, make_pdf("hello"))
        assert r.status_code == 502 and r.json()["detail"] == "The model did not return paper metadata."
        assert paper_rows() == []

    def test_first_pages_are_cut_at_exactly_20000_characters(self, client, fake_openai, monkeypatch):
        monkeypatch.setattr(pdf, "read_upload", lambda data: ("a" * 19_999 + "B" + "C" + "x" * 5_000, {}))
        assert self.upload(client, make_pdf("hello")).status_code == 201
        prompt = fake_openai.calls_of("parse")[0]["messages"][0]["content"]
        assert prompt.endswith("\n\n" + "a" * 19_999 + "B")  # character 20,000 is the last one sent


# --------------------------------------------------------------------------- library: view and delete

class TestViewAndDelete:
    @pytest.mark.xfail(strict=True, reason="Bug B3: SQLite reuses the id of a deleted newest paper, "
                                           "so an old /papers/<id> link opens a different paper.")
    def test_deleted_paper_id_is_not_reused(self, client, seed):
        seed("first")
        deleted = seed("second")
        client.delete(f"/api/papers/{deleted}")
        new = seed("third")
        assert new != deleted
        assert client.get(f"/api/papers/{deleted}").status_code == 404

    def test_stored_pdf_is_served_inline(self, client, seed, text_pdf):
        pid = seed(pdf_bytes=text_pdf)
        r = client.get(f"/api/papers/{pid}/pdf")
        assert r.status_code == 200 and r.content == text_pdf
        assert r.headers["content-type"] == "application/pdf"
        assert r.headers["content-disposition"] == f'inline; filename="paper-{pid}.pdf"'

    def test_pdf_of_unknown_paper_is_404(self, client):
        r = client.get("/api/papers/4242/pdf")
        assert r.status_code == 404 and r.json()["detail"] == "Paper not found."

    def test_row_without_stored_pdf_is_404_not_503(self, client, seed):
        pid = seed()
        storage.ensure_bucket()
        r = client.get(f"/api/papers/{pid}/pdf")
        assert r.status_code == 404 and r.json()["detail"] == "The stored PDF for this paper is missing."

    def test_storage_outage_when_viewing_is_503(self, client, seed, s3, text_pdf):
        pid = seed(pdf_bytes=text_pdf)
        s3.delete_object(Bucket=settings.s3_bucket, Key=f"papers/{pid}.pdf")
        s3.delete_bucket(Bucket=settings.s3_bucket)
        r = client.get(f"/api/papers/{pid}/pdf")
        assert r.status_code == 503 and "Object storage is unavailable" in r.json()["detail"]

    def test_unreachable_storage_when_viewing_is_503(self, client, seed, text_pdf, storage_down):
        pid = seed()
        r = client.get(f"/api/papers/{pid}/pdf")
        assert r.status_code == 503
        assert r.json()["detail"] == ('Object storage is unavailable: Could not connect to the endpoint URL: '
                                      '"http://storage:8333"')

    def test_delete_succeeds_when_storage_is_unreachable(self, client, seed, paper_rows, storage_down):
        pid = seed()
        assert client.delete(f"/api/papers/{pid}").status_code == 204
        assert paper_rows() == [] and storage_down.calls == ["delete_object"]  # attempted, failure ignored

    def test_delete_removes_row_and_pdf_only_for_that_paper(self, client, seed, stored_keys, text_pdf):
        keep = seed("keep", pdf_bytes=text_pdf)
        gone = seed("gone", pdf_bytes=text_pdf)
        r = client.delete(f"/api/papers/{gone}")
        assert r.status_code == 204 and r.content == b""
        assert [p["id"] for p in client.get("/api/papers").json()] == [keep]
        assert stored_keys() == [f"papers/{keep}.pdf"]
        assert client.delete(f"/api/papers/{gone}").status_code == 404  # second delete

    def test_delete_succeeds_even_if_storage_is_down(self, client, seed, s3, text_pdf, paper_rows):
        pid = seed(pdf_bytes=text_pdf)
        s3.delete_object(Bucket=settings.s3_bucket, Key=f"papers/{pid}.pdf")
        s3.delete_bucket(Bucket=settings.s3_bucket)
        assert client.delete(f"/api/papers/{pid}").status_code == 204
        assert paper_rows() == []


# --------------------------------------------------------------------------- summarize / ask

class TestAnalysis:
    def test_summary_uses_the_whole_of_a_long_paper(self, client, fake_openai, seed):
        """FR-4.3: the full text, not the first pages or the abstract."""
        line = "Body text describing the method, the experiments and the results in detail."  # 77 characters
        pages = ["\n".join([f"Page {i}."] + [line] * 20) for i in range(1, 40)] + ["CONCLUSION-ON-LAST-PAGE"]
        pid = seed(pdf_bytes=make_pdf(*pages), abstract="Only the abstract.")
        assert client.post("/api/summarize", json={"paper_id": pid}).status_code == 200
        prompt = fake_openai.last_user_prompt
        assert len(prompt) > 50_000  # a realistic length: about 60,000 characters of paper text
        assert "Page 1." in prompt and "Page 39." in prompt and "CONCLUSION-ON-LAST-PAGE" in prompt
        assert "Only the abstract." not in prompt  # built from the PDF, not from metadata

    def test_question_uses_the_whole_of_a_long_paper(self, client, fake_openai, seed):
        """FR-5.2."""
        filler = "\n".join(["Experiments on several benchmarks with careful ablations and analysis."] * 20)
        pid = seed(pdf_bytes=make_pdf(*[f"Section {i}.\n{filler}" for i in range(30)], "Limitations: small datasets."))
        client.post("/api/ask", json={"paper_id": pid, "question": "What are the major limitations?"})
        assert "Limitations: small datasets." in fake_openai.last_user_prompt

    def test_summarize_saved_paper_sends_full_text(self, client, fake_openai, seed, text_pdf):
        pid = seed(pdf_bytes=make_pdf("Intro text", "Results: 28.4 BLEU"))
        fake_openai.reply = "## Problem\n- Slow RNNs"
        r = client.post("/api/summarize", json={"paper_id": pid})
        assert r.status_code == 200 and r.json() == {"summary": "## Problem\n- Slow RNNs"}
        [call] = fake_openai.calls
        assert call["model"] == settings.openai_model
        assert call["messages"][0] == {"role": "system", "content": llm.QA_SYSTEM}
        user = call["messages"][1]["content"]
        assert user.startswith("<paper>\n") and "Intro text" in user and "28.4 BLEU" in user
        assert user.endswith(llm.SUMMARY_PROMPT)

    def test_ask_saved_paper_strips_question(self, client, fake_openai, seed, text_pdf):
        pid = seed(pdf_bytes=text_pdf)
        fake_openai.reply = "It uses attention."
        r = client.post("/api/ask", json={"paper_id": pid, "question": "   What replaces recurrence?  "})
        assert r.status_code == 200 and r.json() == {"answer": "It uses attention."}
        assert fake_openai.last_user_prompt.endswith("\n\nQuestion: What replaces recurrence?")
        assert "Transformers replace recurrence" in fake_openai.last_user_prompt

    def test_unsaved_result_fetches_pdf_once_then_uses_cache(self, client, fake_openai, downloads, text_pdf):
        downloads.files[SEARCH_RESULT["pdf_url"]] = text_pdf
        body = {"paper": SEARCH_RESULT}
        assert client.post("/api/summarize", json=body).status_code == 200
        assert client.post("/api/ask", json={**body, "question": "Why?"}).status_code == 200
        assert downloads.calls == [SEARCH_RESULT["pdf_url"]]
        assert len(fake_openai.calls_of("create", with_tools=True)) == 1  # tool loop ran once

    def test_empty_model_reply_is_an_empty_string(self, client, fake_openai, seed, text_pdf):
        fake_openai.reply = None
        r = client.post("/api/summarize", json={"paper_id": seed(pdf_bytes=text_pdf)})
        assert r.status_code == 200 and r.json() == {"summary": ""}

    @pytest.mark.parametrize("body", [{}, {"paper_id": 1, "paper": {"title": "x"}}, {"paper_id": None, "paper": None}])
    def test_must_target_exactly_one_paper(self, client, fake_openai, body):
        r = client.post("/api/summarize", json=body)
        assert r.status_code == 422 and "exactly one of paper_id or paper" in r.text
        assert fake_openai.calls == []

    @pytest.mark.parametrize("question", [None, "", "   \n\t"])
    def test_blank_or_missing_question_is_422(self, client, fake_openai, seed, question):
        body = {"paper_id": seed()} if question is None else {"paper_id": seed(), "question": question}
        assert client.post("/api/ask", json=body).status_code == 422
        assert fake_openai.calls == []

    def test_unknown_paper_is_404(self, client, fake_openai):
        r = client.post("/api/ask", json={"paper_id": 9999, "question": "Why?"})
        assert r.status_code == 404 and r.json()["detail"] == "Paper not found."

    def test_missing_stored_pdf_is_404(self, client, fake_openai, seed):
        pid = seed()
        storage.ensure_bucket()
        r = client.post("/api/summarize", json={"paper_id": pid})
        assert r.status_code == 404 and r.json()["detail"] == "The stored PDF for this paper is missing."

    def test_stored_pdf_without_text_is_422(self, client, fake_openai, seed):
        r = client.post("/api/summarize", json={"paper_id": seed(pdf_bytes=make_pdf(None))})
        assert r.status_code == 422 and "no extractable text" in r.json()["detail"]
        assert fake_openai.calls == []

    def test_paper_over_token_limit_is_rejected_not_truncated(self, client, fake_openai, seed, monkeypatch,
                                                              word_tokens):
        monkeypatch.setattr(settings, "max_input_tokens", 10)
        pid = seed(pdf_bytes=make_pdf("one two three four five six seven eight nine ten eleven twelve"))
        r = client.post("/api/summarize", json={"paper_id": pid})
        assert r.status_code == 422
        assert r.json()["detail"] == "This paper is too long for the model (12 tokens; limit 10)."
        assert fake_openai.calls == []

    def test_unreachable_storage_is_503(self, client, fake_openai, seed, storage_down):
        r = client.post("/api/summarize", json={"paper_id": seed()})
        assert r.status_code == 503 and r.json()["detail"].startswith("Object storage is unavailable: ")
        assert fake_openai.calls == []

    def test_missing_api_key_is_503(self, client, seed, text_pdf):
        r = client.post("/api/summarize", json={"paper_id": seed(pdf_bytes=text_pdf)})
        assert r.status_code == 503 and r.json()["detail"] == "OPENAI_API_KEY is not configured on the server."

    @pytest.mark.xfail(strict=True, reason="Bug B2: the PDF cache is keyed on the client-supplied external_id, "
                                           "so a different paper with the same id is summarized from the wrong text.")
    def test_unsaved_papers_sharing_an_external_id_get_their_own_text(self, client, fake_openai, downloads):
        downloads.files["https://a.example/real.pdf"] = make_pdf("TEXT OF PAPER A")
        downloads.files["https://b.example/other.pdf"] = make_pdf("TEXT OF PAPER B")
        client.post("/api/summarize", json={"paper": {"title": "A", "external_id": "same", "pdf_url": "https://a.example/real.pdf"}})
        client.post("/api/summarize", json={"paper": {"title": "B", "external_id": "same", "pdf_url": "https://b.example/other.pdf"}})
        assert "TEXT OF PAPER B" in fake_openai.last_user_prompt

    @pytest.mark.xfail(strict=True, reason="Bug B4: only the paper text is counted toward the token limit, "
                                           "so an over-long question is sent to the model anyway.")
    def test_question_counts_toward_the_token_limit(self, client, fake_openai, seed, monkeypatch, word_tokens):
        monkeypatch.setattr(settings, "max_input_tokens", 20)
        pid = seed(pdf_bytes=make_pdf("a short paper"))
        r = client.post("/api/ask", json={"paper_id": pid, "question": "why " * 50})
        assert r.status_code == 422 and fake_openai.calls == []

    def test_openai_error_is_502(self, client, fake_openai, seed, text_pdf):
        fake_openai.error = openai.APIStatusError("boom", response=httpx.Response(500, request=httpx.Request("POST", "https://api.openai.com")), body=None)
        r = client.post("/api/ask", json={"paper_id": seed(pdf_bytes=text_pdf), "question": "Why?"})
        assert r.status_code == 502 and r.json()["detail"].startswith("OpenAI request failed")


# --------------------------------------------------------------------------- database outage

class TestDatabaseUnavailable:
    @pytest.fixture
    def broken_db(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path}/missing-dir/library.db")
        factory = sessionmaker(bind=engine)

        def override():
            db = factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override
        yield
        engine.dispose()

    @pytest.mark.parametrize("method,path,kwargs", [
        ("get", "/api/papers", {}),
        ("get", "/api/papers/1", {}),
        ("delete", "/api/papers/1", {}),
        ("post", "/api/summarize", {"json": {"paper_id": 1}}),
    ])
    def test_does_not_leak_internals(self, lenient_client, broken_db, method, path, kwargs):
        r = getattr(lenient_client, method)(path, **kwargs)
        assert r.status_code >= 500
        assert "sqlite" not in r.text.lower() and "library.db" not in r.text  # no SQL or file paths

    @pytest.mark.xfail(strict=True, reason="Bug B5: a database outage is a bare 500, so the UI can only show "
                                           "'Request failed (HTTP 500)'. Storage outages return a 503 JSON detail.")
    def test_outage_is_reported_like_other_outages(self, lenient_client, broken_db):
        r = lenient_client.get("/api/papers")
        assert r.status_code == 503 and r.json()["detail"]

    def test_search_still_works_without_the_database(self, lenient_client, broken_db, fake_arxiv):
        fake_arxiv.queue(200, atom_feed(ATTENTION_ENTRY))
        r = lenient_client.get("/api/search", params={"q": "attention"})
        assert r.status_code == 200 and r.json()[0]["title"] == "Attention Is All You Need"


# --------------------------------------------------------------------------- persistence and startup

class TestRestartAndStartup:
    def start_server_on(self, url: str):
        """What a process start does: open the database file and create tables, fresh storage client, empty cache."""
        engine = create_engine(url, connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, autoflush=False)

        def override():
            db = factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override
        storage._client, storage._bucket_ready = None, False
        cache._pdfs.clear()
        return engine

    def test_saved_and_uploaded_papers_survive_a_restart(self, tmp_path, fake_openai, downloads, text_pdf):
        """FR-2.2: the library and its PDFs are still there after the application restarts."""
        url = f"sqlite:///{tmp_path}/library.db"
        downloads.files[SEARCH_RESULT["pdf_url"]] = text_pdf
        uploaded_pdf = make_pdf("Uploaded body")

        engine = self.start_server_on(url)
        with TestClient(app) as c:
            saved = c.post("/api/papers", json=SEARCH_RESULT).json()
            uploaded = c.post("/api/papers/upload", files={"file": ("u.pdf", uploaded_pdf, "application/pdf")}).json()
        engine.dispose()

        engine = self.start_server_on(url)
        try:
            with TestClient(app) as c:
                assert c.get("/api/papers").json() == [uploaded, saved]
                assert c.get(f"/api/papers/{saved['id']}/pdf").content == text_pdf
                assert c.get(f"/api/papers/{uploaded['id']}/pdf").content == uploaded_pdf
                fake_openai.reply = "summary after restart"
                assert c.post("/api/summarize", json={"paper_id": saved["id"]}).json() == {"summary": "summary after restart"}
            assert downloads.calls == [SEARCH_RESULT["pdf_url"]]  # served from storage, not downloaded again
        finally:
            engine.dispose()

    def test_app_starts_when_storage_is_down(self, monkeypatch, caplog, fake_arxiv, storage_down):
        caplog.set_level(logging.WARNING, logger="uvicorn.error")
        fake_arxiv.queue(200, atom_feed(ATTENTION_ENTRY))
        with TestClient(app) as c:  # runs the startup hook
            assert c.get("/api/papers").json() == []
            assert c.get("/api/search", params={"q": "attention"}).status_code == 200
        assert storage_down.calls == ["head_bucket"]
        assert "Could not reach object storage at startup" in caplog.text

    def test_real_get_db_opens_a_session_on_the_configured_database_and_closes_it(self):
        gen = db_module.get_db()
        session = next(gen)
        assert isinstance(session, SASession)
        assert str(session.get_bind().url) == settings.database_url
        session.execute(text("SELECT 1"))
        assert session.in_transaction()
        gen.close()  # FastAPI does this when the request ends
        assert not session.in_transaction()  # closed: its transaction was released
