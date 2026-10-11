"""The save/fetch tool loop, driven by a fake OpenAI client (no API key needed)."""
import json
from types import SimpleNamespace

import pytest

from helpers import make_pdf

from app.errors import AppError
from app.schemas import SearchResult
from app.services import cache, llm, pdf


class FakeMessage:
    def __init__(self, urls):
        self.tool_calls = [
            SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name="download_paper_pdf", arguments=json.dumps({"url": u})))
            for i, u in enumerate(urls)
        ] or None

    def model_dump(self, **_):
        return {"role": "assistant", "content": None, "tool_calls": []}


class FakeClient:
    """Each create() call returns the next scripted list of URLs the 'model' asks to download."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls += 1
        urls = self.script.pop(0) if self.script else []
        return SimpleNamespace(choices=[SimpleNamespace(message=FakeMessage(urls))])


def use_fake(monkeypatch, script) -> FakeClient:
    client = FakeClient(script)
    monkeypatch.setattr(llm, "_client", lambda: client)
    return client


PAPER = SearchResult(title="T", pdf_url="https://host/a.pdf", url="https://host/landing")


def test_success_on_first_call(monkeypatch):
    client = use_fake(monkeypatch, [[PAPER.pdf_url]])
    monkeypatch.setattr(pdf, "download_pdf", lambda url: make_pdf("hello world"))
    assert llm.fetch_pdf_with_tool(PAPER)[:5] == b"%PDF-"
    assert client.calls == 1


def test_falls_back_to_landing_url_after_failure(monkeypatch):
    client = use_fake(monkeypatch, [[PAPER.pdf_url], [PAPER.url]])

    def download(url):
        if url == PAPER.pdf_url:
            raise pdf.PdfError("server returned HTTP 403")
        return make_pdf("hello world")

    monkeypatch.setattr(pdf, "download_pdf", download)
    assert llm.fetch_pdf_with_tool(PAPER)[:5] == b"%PDF-"
    assert client.calls == 2


def test_rejects_urls_the_model_invents(monkeypatch):
    client = use_fake(monkeypatch, [["https://evil.example/x.pdf"]])
    called = []
    monkeypatch.setattr(pdf, "download_pdf", lambda url: called.append(url) or make_pdf("hello world"))
    with pytest.raises(AppError) as e:
        llm.fetch_pdf_with_tool(PAPER)
    assert e.value.status == 422 and called == []


def test_gives_up_after_three_failed_calls(monkeypatch):
    client = use_fake(monkeypatch, [[PAPER.pdf_url]] * 5)

    def fail(url):
        raise pdf.PdfError("server returned HTTP 403")

    monkeypatch.setattr(pdf, "download_pdf", fail)
    with pytest.raises(AppError) as e:
        llm.fetch_pdf_with_tool(PAPER)
    assert e.value.status == 422 and "403" in e.value.message
    assert client.calls == 3


def test_success_is_decided_by_the_backend_not_the_model(monkeypatch):
    client = use_fake(monkeypatch, [[]])  # model answers in prose without calling the tool
    with pytest.raises(AppError) as e:
        llm.fetch_pdf_with_tool(PAPER)
    assert e.value.status == 422


def test_pdf_without_text_counts_as_failure(monkeypatch):
    client = use_fake(monkeypatch, [[PAPER.pdf_url]])
    monkeypatch.setattr(pdf, "download_pdf", lambda url: make_pdf())
    with pytest.raises(AppError):
        llm.fetch_pdf_with_tool(PAPER)


def test_ensure_pdf_caches(monkeypatch):
    paper = SearchResult(title="C", pdf_url="https://host/c.pdf", external_id="cache-loop")
    cache.put(cache.cache_key(paper), b"cached")
    monkeypatch.setattr(llm, "_client", lambda: pytest.fail("LLM must not be called on a cache hit"))
    assert llm.ensure_pdf(paper) == b"cached"


def test_non_object_tool_arguments_are_handled(monkeypatch):
    client = use_fake(monkeypatch, [["x"]])
    original = FakeMessage.__init__

    def init(self, urls):
        original(self, urls)
        for c in self.tool_calls or []:
            c.function.arguments = json.dumps(["not", "an", "object"])

    monkeypatch.setattr(FakeMessage, "__init__", init)
    with pytest.raises(AppError) as e:
        llm.fetch_pdf_with_tool(PAPER)
    assert e.value.status == 422
