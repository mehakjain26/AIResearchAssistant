"""Unit tests for the service modules: arXiv parsing, PDF download/reading, cache, storage and the LLM layer."""
import json
import re
import socket
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import httpx
import pytest

from app.config import settings
from app.errors import AppError
from app.schemas import SearchResult
from app.services import arxiv, cache, llm, pdf, storage
import openai
from botocore.exceptions import ClientError

from qa_support import InvalidOpenAIRequest, WordEncoding, atom_feed, check_request, chunked, make_pdf, mock_http


class FakeDoc:
    """Stands in for a pymupdf document whose pages return the given text."""

    def __init__(self, texts):
        self.pages = [SimpleNamespace(get_text=lambda t=t: t) for t in texts]
        self.metadata = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self.pages)

    def __getitem__(self, i):
        return self.pages[i]

    def __len__(self):
        return len(self.pages)


class RaisingS3:
    """An S3 client whose every call raises `error`."""

    def __init__(self, error):
        self.error = error

    def __getattr__(self, name):
        def call(**kwargs):
            raise self.error
        return call


def exactly(message: str) -> str:
    """A pytest.raises(match=...) pattern for this exact message."""
    return f"^{re.escape(message)}$"


# --------------------------------------------------------------------------- arxiv

class TestArxivParse:
    def test_normalizes_whitespace_and_strips_version(self):
        [r] = arxiv.parse(atom_feed({"id": "http://arxiv.org/abs/2101.00001v12", "published": "2021-01-01T00:00:00Z",
                                     "title": "  A\n   Title  ", "summary": "\n Some\n\n abstract ",
                                     "authors": [" Jane  Doe "]}))
        assert (r.title, r.abstract, r.authors, r.year) == ("A Title", "Some abstract", ["Jane Doe"], 2021)
        assert (r.external_id, r.url, r.pdf_url) == (
            "2101.00001", "https://arxiv.org/abs/2101.00001", "https://arxiv.org/pdf/2101.00001")

    def test_old_style_ids_keep_their_archive_prefix(self):
        [r] = arxiv.parse(atom_feed({"id": "http://arxiv.org/abs/hep-th/9901001v3", "title": "Old"}))
        assert r.external_id == "hep-th/9901001" and r.pdf_url == "https://arxiv.org/pdf/hep-th/9901001"

    def test_missing_fields_get_defaults(self):
        [r] = arxiv.parse(atom_feed({"id": "http://arxiv.org/abs/1", "title": "   ", "summary": "",
                                     "published": "unknown", "authors": ["  "]}))
        assert (r.title, r.abstract, r.year, r.authors) == ("Untitled", None, None, [])

    def test_empty_feed(self):
        assert arxiv.parse(atom_feed()) == []

    def test_invalid_xml_raises(self):
        with pytest.raises(ET.ParseError):
            arxiv.parse("not xml")


class TestArxivQueries:
    @pytest.mark.parametrize("text,expected", [
        ("graph attention, networks!", 'all:"graph attention networks"'),
        ("  BERT  ", 'all:"BERT"'),
        ("GPT-4 vs. GPT-3.5", 'all:"GPT 4 vs GPT 3 5"'),
        ("?!", ""),
        ("", ""),
    ])
    def test_phrase_query(self, text, expected):
        assert arxiv._phrase_query(text) == expected

    @pytest.mark.parametrize("text,expected", [
        ("The Attention of Transformers", "all:Attention AND all:Transformers"),  # stop words are case-insensitive
        ("the of and", "all:the AND all:of AND all:and"),  # only stop words: keep them rather than send nothing
        ("x", "all:x"),
    ])
    def test_terms_query(self, text, expected):
        assert arxiv._terms_query(text) == expected

    @pytest.mark.parametrize("since_last,expected_sleep", [(2.5, [0.5]), (0.0, [3.0]), (3.0, []), (10.0, [])])
    def test_waits_out_the_rest_of_the_interval(self, monkeypatch, fake_arxiv, since_last, expected_sleep):
        slept = []
        monkeypatch.setattr(arxiv, "MIN_INTERVAL", 3.0)
        monkeypatch.setattr(arxiv, "time", SimpleNamespace(monotonic=lambda: 100.0, sleep=slept.append))
        monkeypatch.setattr(arxiv, "_last_request", 100.0 - since_last)
        fake_arxiv.queue(200, atom_feed())
        arxiv._fetch("all:a", 1)
        assert slept == pytest.approx(expected_sleep)
        assert arxiv._last_request == 100.0

    def test_last_request_time_is_updated_even_when_the_request_fails(self, fake_arxiv):
        fake_arxiv.queue_error(httpx.ReadTimeout("slow"))
        before = arxiv._last_request
        with pytest.raises(AppError):
            arxiv._fetch("all:a", 1)
        assert arxiv._last_request > before

    def test_custom_limit_is_forwarded(self, fake_arxiv):
        fake_arxiv.queue(200, atom_feed())
        arxiv._fetch("all:a", 3)
        assert fake_arxiv.requests[0]["params"] == {"search_query": "all:a", "start": 0, "max_results": 3,
                                                    "sortBy": "relevance"}


# --------------------------------------------------------------------------- pdf

PUBLIC_IP = "93.184.216.34"


@pytest.fixture
def resolve(monkeypatch):
    """Controls DNS for pdf._check_public_url: host -> IP (default: a public address)."""
    table = {}

    def getaddrinfo(host, port, **kw):
        ip = table.get(host, host if host.replace(".", "").isdigit() or ":" in host else PUBLIC_IP)
        if ip is None:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(pdf.socket, "getaddrinfo", getaddrinfo)
    return table


def serve(monkeypatch, handler):
    monkeypatch.setattr(pdf.httpx, "Client", mock_http(handler))


class TestDownloadPdf:
    def test_downloads_pdf_bytes(self, monkeypatch, resolve, text_pdf):
        seen = []
        serve(monkeypatch, lambda req: seen.append(req) or httpx.Response(200, content=text_pdf))
        assert pdf.download_pdf("https://papers.example/a.pdf") == text_pdf
        assert seen[0].headers["accept"].startswith("application/pdf")

    def test_follows_redirect_to_public_host(self, monkeypatch, resolve, text_pdf):
        def handler(req):
            if req.url.path == "/start":
                return httpx.Response(302, headers={"location": "https://cdn.example/final.pdf"})
            return httpx.Response(200, content=text_pdf)
        serve(monkeypatch, handler)
        assert pdf.download_pdf("https://papers.example/start") == text_pdf

    @pytest.mark.parametrize("target", ["http://127.0.0.1/secret", "http://169.254.169.254/latest/meta-data/",
                                        "http://192.168.1.10/x.pdf"])
    def test_redirect_to_internal_address_is_blocked(self, monkeypatch, resolve, text_pdf, target):
        hits = []

        def handler(req):
            hits.append(str(req.url))
            return httpx.Response(302, headers={"location": target})
        serve(monkeypatch, handler)
        with pytest.raises(pdf.PdfError, match=exactly("links to private or local addresses are not allowed")):
            pdf.download_pdf("https://papers.example/start")
        assert hits == ["https://papers.example/start"]  # the internal URL was never requested

    def test_hostname_resolving_to_private_ip_is_blocked(self, monkeypatch, resolve):
        resolve["intranet.example"] = "10.1.2.3"
        serve(monkeypatch, lambda req: pytest.fail("must not send the request"))
        with pytest.raises(pdf.PdfError, match=exactly("links to private or local addresses are not allowed")):
            pdf.download_pdf("https://intranet.example/a.pdf")

    def test_unresolvable_host(self, monkeypatch, resolve):
        resolve["nope.invalid"] = None
        with pytest.raises(pdf.PdfError, match=exactly("could not resolve the link's host")):
            pdf.download_pdf("https://nope.invalid/a.pdf")

    @pytest.mark.parametrize("url", ["ftp://example.com/a.pdf", "file:///etc/passwd", "javascript:alert(1)", "https://"])
    def test_non_http_links_are_refused(self, url):
        with pytest.raises(pdf.PdfError, match=r"^(download failed: )?only http\(s\) links can be downloaded$"):
            pdf.download_pdf(url)

    @pytest.mark.parametrize("status", [403, 404, 500])
    def test_non_200_status(self, monkeypatch, resolve, status):
        serve(monkeypatch, lambda req: httpx.Response(status))
        with pytest.raises(pdf.PdfError, match=exactly(f"server returned HTTP {status}")):
            pdf.download_pdf("https://papers.example/a.pdf")

    def test_html_instead_of_pdf(self, monkeypatch, resolve):
        serve(monkeypatch, lambda req: httpx.Response(200, content=b"<html>login</html>"))
        with pytest.raises(pdf.PdfError, match=exactly("the link did not return a PDF")):
            pdf.download_pdf("https://papers.example/a.pdf")

    def test_size_cap_boundary(self, monkeypatch, resolve):
        body = b"%PDF-" + b"x" * 95
        serve(monkeypatch, lambda req: httpx.Response(200, content=body))
        monkeypatch.setattr(pdf, "MAX_PDF_BYTES", 100)
        assert pdf.download_pdf("https://papers.example/a.pdf") == body  # exactly 100 bytes
        monkeypatch.setattr(pdf, "MAX_PDF_BYTES", 99)
        with pytest.raises(pdf.PdfError, match="larger than"):
            pdf.download_pdf("https://papers.example/a.pdf")

    def test_size_cap_counts_every_chunk(self, monkeypatch, resolve):
        """A large file sent in small chunks must still hit the limit: the total is what counts."""
        serve(monkeypatch, lambda req: httpx.Response(200, content=chunked(b"%PDF-" + b"x" * 55, b"y" * 60)))
        monkeypatch.setattr(pdf, "MAX_PDF_BYTES", 100)
        with pytest.raises(pdf.PdfError):
            pdf.download_pdf("https://papers.example/a.pdf")

    def test_chunks_are_joined_exactly(self, monkeypatch, resolve):
        parts = (b"%PDF-1.7\n", b"part two ", b"part three")
        serve(monkeypatch, lambda req: httpx.Response(200, content=chunked(*parts)))
        assert pdf.download_pdf("https://papers.example/a.pdf") == b"".join(parts)

    def test_default_limit_is_50_mb_and_stops_reading_early(self, monkeypatch, resolve):
        mb = b"x" * (1024 * 1024)
        sent = []

        def body():
            yield b"%PDF-"
            for _ in range(200):  # a 200 MB response
                sent.append(1)
                yield mb

        serve(monkeypatch, lambda req: httpx.Response(200, content=body()))
        with pytest.raises(pdf.PdfError, match=exactly("the file is larger than 50 MB")):
            pdf.download_pdf("https://papers.example/a.pdf")
        assert len(sent) <= 51  # gave up as soon as the limit was passed

    def test_uses_get_with_a_timeout(self, monkeypatch, resolve, text_pdf):
        seen = []
        factory = mock_http(lambda req: seen.append(req.method) or httpx.Response(200, content=text_pdf))
        monkeypatch.setattr(pdf.httpx, "Client", factory)
        pdf.download_pdf("https://papers.example/a.pdf")
        assert seen == ["GET"]
        assert factory.options[0]["timeout"] and factory.options[0]["follow_redirects"] is True

    def test_transport_error(self, monkeypatch, resolve):
        def handler(req):
            raise httpx.ConnectTimeout("timed out")
        serve(monkeypatch, handler)
        with pytest.raises(pdf.PdfError, match=r"^download failed: timed out$"):
            pdf.download_pdf("https://papers.example/a.pdf")


class TestReadPdf:
    @pytest.mark.parametrize("data,expected", [
        (b"%PDF-1.4", True), (b"  \n%PDF-1.7", True), (b"%PDF", False), (b"", False), (b"PK\x03\x04", False),
    ])
    def test_is_pdf(self, data, expected):
        assert pdf.is_pdf(data) is expected

    def test_extract_text_joins_pages_in_order(self):
        text = pdf.extract_text(make_pdf("alpha page", "beta page", "gamma page"))
        assert text.index("alpha") < text.index("beta") < text.index("gamma")

    def test_extract_text_ignores_blank_pages(self):
        assert pdf.extract_text(make_pdf(None, "only text")) == "only text"

    def test_read_upload_returns_first_pages_and_non_empty_metadata(self):
        text, meta = pdf.read_upload(make_pdf("one", "two", "three", metadata={"title": "Meta Title", "author": ""}))
        assert "one" in text and "two" in text and "three" not in text
        assert meta.get("title") == "Meta Title" and "author" not in meta  # empty values are dropped

    def test_read_upload_with_fewer_pages_than_requested(self):
        text, _ = pdf.read_upload(make_pdf("single"), pages=5)
        assert text.strip() == "single"

    def test_read_upload_finds_text_after_blank_first_page(self):
        text, _ = pdf.read_upload(make_pdf(None, "late text"))
        assert "late text" in text

    @pytest.mark.parametrize("fn", [pdf.extract_text, pdf.check_has_text, pdf.read_upload])
    def test_unreadable_bytes(self, fn):
        with pytest.raises(pdf.PdfError, match="could not read"):
            fn(b"%PDF-broken")

    @pytest.mark.parametrize("fn", [pdf.extract_text, pdf.check_has_text, pdf.read_upload])
    def test_scanned_pdf(self, fn):
        with pytest.raises(pdf.PdfError, match="no extractable text"):
            fn(make_pdf(None, None))

    def test_nul_bytes_are_removed_from_extracted_text(self, monkeypatch):
        """Some PDFs yield NUL characters; they must not reach the prompt (JSON and databases choke on them)."""
        monkeypatch.setattr(pdf, "_open", lambda data: FakeDoc(["Intro\x00 text", "Res\x00ults"]))
        assert pdf.extract_text(b"%PDF-") == "Intro text\nResults"
        text, _ = pdf.read_upload(b"%PDF-")
        assert text == "Intro text\nResults"

    def test_pdf_error_is_a_422(self):
        assert pdf.PdfError("x").status == 422


# --------------------------------------------------------------------------- cache

class TestCache:
    def test_key_prefers_external_id_then_pdf_url_then_url(self):
        assert cache.cache_key(SearchResult(title="t", external_id="e", pdf_url="p", url="u")) == "e"
        assert cache.cache_key(SearchResult(title="t", pdf_url="p", url="u")) == "p"
        assert cache.cache_key(SearchResult(title="t", url="u")) == "u"
        assert cache.cache_key(SearchResult(title="t")) is None

    def test_none_key_is_never_stored(self):
        cache.put(None, b"data")
        assert cache.get(None) is None and len(cache._pdfs) == 0

    def test_evicts_least_recently_used_by_count(self, monkeypatch):
        monkeypatch.setattr(cache, "MAX_ENTRIES", 2)
        cache.put("a", b"1")
        cache.put("b", b"2")
        assert cache.get("a") == b"1"  # touch a: b is now the oldest
        cache.put("c", b"3")
        assert list(cache._pdfs) == ["a", "c"]

    def test_evicts_by_total_bytes(self, monkeypatch):
        monkeypatch.setattr(cache, "MAX_BYTES", 100)
        cache.put("a", b"x" * 40)
        cache.put("b", b"x" * 40)
        cache.put("c", b"x" * 40)
        assert list(cache._pdfs) == ["b", "c"]

    def test_total_exactly_at_the_byte_limit_is_kept(self, monkeypatch):
        monkeypatch.setattr(cache, "MAX_BYTES", 100)
        cache.put("a", b"x" * 50)
        cache.put("b", b"x" * 50)
        assert list(cache._pdfs) == ["a", "b"]

    def test_only_the_newest_of_several_oversized_entries_is_kept(self, monkeypatch):
        monkeypatch.setattr(cache, "MAX_BYTES", 10)
        cache.put("big1", b"x" * 1000)
        cache.put("big2", b"y" * 1000)
        assert list(cache._pdfs) == ["big2"]

    def test_single_oversized_entry_is_kept(self, monkeypatch):
        monkeypatch.setattr(cache, "MAX_BYTES", 10)
        cache.put("big", b"x" * 1000)
        assert cache.get("big") == b"x" * 1000

    def test_overwriting_a_key_replaces_its_value(self):
        cache.put("k", b"old")
        cache.put("k", b"new")
        assert cache.get("k") == b"new" and len(cache._pdfs) == 1


# --------------------------------------------------------------------------- storage

class TestStorage:
    def test_round_trip_under_expected_key(self, s3):
        storage.put_pdf(7, b"%PDF-data")
        obj = s3.get_object(Bucket=settings.s3_bucket, Key="papers/7.pdf")
        assert obj["Body"].read() == b"%PDF-data" and obj["ContentType"] == "application/pdf"
        assert storage.get_pdf(7) == b"%PDF-data"

    def test_client_targets_the_configured_store_with_path_style_urls(self, monkeypatch):
        """SeaweedFS (and MinIO) need path-style addressing; a store that is down must fail fast."""
        monkeypatch.setattr(settings, "s3_endpoint", "http://storage:8333")
        client = storage._s3()
        assert client.meta.endpoint_url == "http://storage:8333"
        assert client.meta.region_name == "us-east-1"
        assert client.meta.config.s3 == {"addressing_style": "path"}
        assert client.meta.config.connect_timeout <= 10
        assert client.meta.config.retries["total_max_attempts"] <= 2  # at most one retry
        assert storage._s3() is client  # built once

    def test_ensure_bucket_checks_the_store_only_once(self, s3):
        client = storage._s3()
        heads = []
        real_head = client.head_bucket
        client.head_bucket = lambda **kw: heads.append(kw) or real_head(**kw)
        storage.ensure_bucket()
        storage.ensure_bucket()
        storage.put_pdf(1, b"x")
        assert heads == [{"Bucket": settings.s3_bucket}]

    @pytest.mark.parametrize("code", ["NoSuchKey", "404"])
    def test_not_found_codes_are_404(self, monkeypatch, code):
        monkeypatch.setattr(storage, "_client", RaisingS3(ClientError({"Error": {"Code": code}}, "GetObject")))
        with pytest.raises(AppError) as e:
            storage.get_pdf(1)
        assert (e.value.status, e.value.message) == (404, "The stored PDF for this paper is missing.")

    def test_other_client_errors_are_503_naming_the_cause(self, monkeypatch):
        error = ClientError({"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}, "GetObject")
        monkeypatch.setattr(storage, "_client", RaisingS3(error))
        with pytest.raises(AppError) as e:
            storage.get_pdf(1)
        assert (e.value.status, e.value.message) == (503, f"Object storage is unavailable: {error}")

    def test_client_error_without_a_code_is_503(self, monkeypatch):
        monkeypatch.setattr(storage, "_client", RaisingS3(ClientError({}, "GetObject")))
        with pytest.raises(AppError) as e:
            storage.get_pdf(1)
        assert e.value.status == 503

    @pytest.mark.parametrize("call", [lambda: storage.get_pdf(1), lambda: storage.put_pdf(1, b"x")])
    def test_unreachable_store_is_503_naming_the_cause(self, storage_down, call):
        with pytest.raises(AppError) as e:
            call()
        assert e.value.status == 503
        assert e.value.message == 'Object storage is unavailable: Could not connect to the endpoint URL: "http://storage:8333"'

    def test_ensure_bucket_creates_once(self, s3, monkeypatch):
        assert s3.list_buckets()["Buckets"] == []
        storage.ensure_bucket()
        storage.ensure_bucket()
        assert [b["Name"] for b in s3.list_buckets()["Buckets"]] == [settings.s3_bucket]

    def test_ensure_bucket_keeps_existing_bucket_contents(self, s3):
        s3.create_bucket(Bucket=settings.s3_bucket)
        s3.put_object(Bucket=settings.s3_bucket, Key="papers/1.pdf", Body=b"keep me")
        storage.ensure_bucket()
        assert storage.get_pdf(1) == b"keep me"

    def test_missing_object_is_404(self, s3):
        storage.ensure_bucket()
        with pytest.raises(AppError) as e:
            storage.get_pdf(12345)
        assert e.value.status == 404

    def test_missing_bucket_is_503(self):
        with pytest.raises(AppError) as e:
            storage.get_pdf(1)
        assert e.value.status == 503

    def test_delete_is_best_effort(self, s3):
        storage.delete_pdf(1)  # no bucket at all: must not raise
        storage.put_pdf(2, b"x")
        storage.delete_pdf(2)
        assert s3.list_objects_v2(Bucket=settings.s3_bucket).get("KeyCount") == 0


# --------------------------------------------------------------------------- llm

class TestLlm:
    def test_client_requires_api_key(self):
        with pytest.raises(AppError) as e:
            llm._client()
        assert e.value.status == 503

    def test_client_is_built_once_with_the_configured_key(self, monkeypatch):
        monkeypatch.setattr(settings, "openai_api_key", "sk-test-key")
        llm._openai.cache_clear()
        try:
            first, second = llm._client(), llm._client()
            assert isinstance(first, openai.OpenAI) and first.api_key == "sk-test-key"
            assert first is second  # one client, so HTTP connections are reused
        finally:
            llm._openai.cache_clear()

    def test_short_text_skips_tokenizing(self, monkeypatch):
        monkeypatch.setattr(settings, "max_input_tokens", 5)
        tokenized = []
        monkeypatch.setattr(llm, "_encoding", lambda m: tokenized.append(m))
        assert llm._check_length("abcde") is None  # 5 bytes can never be more than 5 tokens
        assert tokenized == []

    def test_token_limit_boundary(self, monkeypatch, word_tokens):
        text = "w " * 7  # 14 bytes, 7 tokens: past the byte shortcut, so it is really counted
        monkeypatch.setattr(settings, "max_input_tokens", 7)
        assert llm._check_length(text) is None  # exactly at the limit is allowed
        monkeypatch.setattr(settings, "max_input_tokens", 6)
        with pytest.raises(AppError) as e:
            llm._check_length(text)
        assert (e.value.status, e.value.message) == (422, "This paper is too long for the model (7 tokens; limit 6).")

    def test_token_count_is_formatted_with_thousands_separators(self, monkeypatch, word_tokens):
        monkeypatch.setattr(settings, "max_input_tokens", 1_000)
        with pytest.raises(AppError) as e:
            llm._check_length("w " * 1_234)
        assert e.value.message == "This paper is too long for the model (1,234 tokens; limit 1,000)."

    def test_real_tokenizer_counts_tokens_not_bytes(self, monkeypatch):
        """The one test that uses tiktoken itself; it needs the encoding file (downloaded once, then cached)."""
        llm._encoding.cache_clear()
        try:
            enc = llm._encoding(settings.openai_model)
        except Exception as e:  # offline with an empty cache
            pytest.skip(f"tiktoken encoding unavailable: {e}")
        finally:
            llm._encoding.cache_clear()
        text = "attention " * 50
        tokens = len(enc.encode(text))
        assert tokens < len(text.encode())  # many bytes per token
        monkeypatch.setattr(settings, "max_input_tokens", tokens)
        assert llm._check_length(text) is None

    def test_unknown_model_falls_back_to_default_encoding(self, monkeypatch):
        requested = []

        def get_encoding(name):
            requested.append(name)
            return WordEncoding()

        monkeypatch.setattr(llm.tiktoken, "get_encoding", get_encoding)
        llm._encoding.cache_clear()
        try:
            assert isinstance(llm._encoding("not-a-real-model"), WordEncoding)
            assert requested == ["o200k_base"]
        finally:
            llm._encoding.cache_clear()

    def test_answer_prompt_structure(self, fake_openai):
        fake_openai.reply = "42"
        assert llm.answer("PAPER TEXT", "What is it?") == "42"
        assert fake_openai.last_user_prompt == "<paper>\nPAPER TEXT\n</paper>\n\nQuestion: What is it?"

    def test_extract_metadata_passes_schema_and_hint(self, fake_openai):
        meta = llm.extract_metadata("first pages", {"title": "Hint"})
        call = fake_openai.calls_of("parse")[0]
        assert call["response_format"].__name__ == "PaperMetadata"
        assert json.dumps({"title": "Hint"}) in call["messages"][0]["content"]
        assert meta.title == "Extracted Title"


class TestToolLoop:
    PAPER = SearchResult(title="T", pdf_url="https://host/a.pdf", url="https://host/abs")

    def test_first_turn_forces_tool_use_then_auto(self, fake_openai, downloads, text_pdf):
        fake_openai.tool_script = [[self.PAPER.pdf_url], [self.PAPER.url]]
        downloads.files[self.PAPER.url] = text_pdf
        assert llm.fetch_pdf_with_tool(self.PAPER) == text_pdf
        assert [c["tool_choice"] for c in fake_openai.calls] == ["required", "auto"]
        assert downloads.calls == [self.PAPER.pdf_url, self.PAPER.url]

    def test_tool_error_is_reported_back_to_the_model(self, fake_openai, downloads, text_pdf):
        fake_openai.tool_script = [[self.PAPER.pdf_url], [self.PAPER.url]]
        downloads.files[self.PAPER.url] = text_pdf
        llm.fetch_pdf_with_tool(self.PAPER)
        tool_msgs = [m for m in fake_openai.calls[1]["messages"] if m.get("role") == "tool"]
        assert tool_msgs == [{"role": "tool", "tool_call_id": "call0", "content": "error: server returned HTTP 404"}]

    def test_parallel_calls_count_toward_the_limit(self, fake_openai, downloads):
        fake_openai.tool_script = [[self.PAPER.pdf_url] * 4]
        with pytest.raises(AppError) as e:
            llm.fetch_pdf_with_tool(self.PAPER)
        assert len(downloads.calls) == llm.MAX_TOOL_CALLS and len(fake_openai.calls) == 1
        assert e.value.status == 422

    def test_request_names_the_paper_and_its_links(self, fake_openai, downloads, text_pdf):
        downloads.files[self.PAPER.pdf_url] = text_pdf
        llm.fetch_pdf_with_tool(self.PAPER)
        call = fake_openai.calls[0]
        assert call["model"] == settings.openai_model and call["tools"] == [llm.DOWNLOAD_TOOL]
        assert call["messages"][0] == {"role": "system", "content": llm.FETCH_SYSTEM}
        sent = json.loads(call["messages"][1]["content"].removeprefix("Fetch the PDF for this paper: "))
        assert sent == {"title": "T", "pdf_url": "https://host/a.pdf", "url": "https://host/abs"}

    def test_invented_url_is_refused_and_the_model_is_told_why(self, fake_openai, downloads, text_pdf):
        fake_openai.tool_script = [["https://evil.example/x.pdf"], [self.PAPER.pdf_url]]
        downloads.files[self.PAPER.pdf_url] = text_pdf
        assert llm.fetch_pdf_with_tool(self.PAPER) == text_pdf
        tool_msgs = [m for m in fake_openai.calls[1]["messages"] if m["role"] == "tool"]
        assert [m["content"] for m in tool_msgs] == ["error: that URL is not one of the paper's links"]
        assert downloads.calls == [self.PAPER.pdf_url]  # the invented URL was never fetched

    def test_gives_up_after_three_turns_with_the_last_error(self, fake_openai, downloads):
        fake_openai.tool_script = [[self.PAPER.pdf_url], [self.PAPER.url], [self.PAPER.pdf_url], [self.PAPER.url]]
        downloads.files[self.PAPER.url] = pdf.PdfError("server returned HTTP 403")
        with pytest.raises(AppError) as e:
            llm.fetch_pdf_with_tool(self.PAPER)
        assert len(fake_openai.calls) == 3 and len(downloads.calls) == 3
        assert e.value.message == "Could not fetch the paper's PDF: server returned HTTP 404."

    def test_three_failed_calls_in_one_turn_end_the_loop(self, fake_openai, downloads):
        fake_openai.tool_script = [[self.PAPER.pdf_url] * 3, [self.PAPER.url]]
        with pytest.raises(AppError):
            llm.fetch_pdf_with_tool(self.PAPER)
        assert len(fake_openai.calls) == 1 and len(downloads.calls) == 3  # no fourth attempt, no second turn

    def test_model_that_never_calls_the_tool(self, fake_openai):
        fake_openai.tool_script = [[]]
        with pytest.raises(AppError) as e:
            llm.fetch_pdf_with_tool(self.PAPER)
        assert e.value.message == "Could not fetch the paper's PDF: the model did not request a download."

    def test_only_pdf_url_link_is_enough(self, fake_openai, downloads, text_pdf):
        paper = SearchResult(title="T", pdf_url="https://host/only.pdf")
        downloads.files[paper.pdf_url] = text_pdf
        assert llm.fetch_pdf_with_tool(paper) == text_pdf

    @pytest.mark.parametrize("raw_args", ["{not json", '["a list"]', '{"link": "https://host/a.pdf"}'])
    def test_malformed_arguments_are_a_failed_attempt(self, fake_openai, downloads, raw_args):
        fake_openai.tool_script = [raw_args]
        with pytest.raises(AppError) as e:
            llm.fetch_pdf_with_tool(self.PAPER)
        assert e.value.status == 422 and downloads.calls == []


class TestFakeOpenAIContract:
    """The fake must reject what the real API rejects, or tests would pass on malformed requests."""
    GOOD = {"model": "gpt-4.1", "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]}

    def test_valid_request_passes(self):
        check_request("create", self.GOOD)

    @pytest.mark.parametrize("bad", [
        {"model": None},
        {"messages": []},
        {"messages": [{"ROLE": "user", "content": "u"}]},
        {"messages": [{"role": "USER", "content": "u"}]},
        {"messages": [{"role": "system", "content": None}, {"role": "user", "content": "u"}]},
        {"messages": [{"role": "user", "CONTENT": "u"}]},
        {"tools": None, "tool_choice": "auto"},
        {"tools": [{"type": "function", "function": {"name": "f"}}], "tool_choice": "sometimes"},
    ])
    def test_malformed_request_is_rejected(self, bad):
        with pytest.raises(InvalidOpenAIRequest):
            check_request("create", {**self.GOOD, **bad})

    def test_parse_needs_a_response_format(self):
        with pytest.raises(InvalidOpenAIRequest):
            check_request("parse", self.GOOD)
