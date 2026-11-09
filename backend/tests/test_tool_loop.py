"""The save/fetch tool loop, driven by a fake OpenAI client (no API key needed).

This file tests `llm.fetch_pdf_with_tool` and `llm.ensure_pdf`, which implement the
OpenAI function-calling loop used when saving or analyzing an unsaved search result:
1. The backend sends the paper's `title`, `pdf_url`, and `url` to OpenAI along with the
   `download_paper_pdf(url)` tool definition.
2. The model responds with tool call(s) requesting to download a URL (up to 3 attempts).
3. For each tool call, the backend verifies the URL is in `{paper.pdf_url, paper.url}`,
   downloads the PDF, and checks that it contains extractable text.
"""
import json
from types import SimpleNamespace

import pytest

from helpers import make_pdf

from app.errors import AppError
from app.schemas import SearchResult
from app.services import cache, llm, pdf


class FakeMessage:
    """Simulates `response.choices[0].message` returned by the OpenAI Chat Completions SDK.

    Builds a list of fake `tool_calls` requesting `download_paper_pdf(url=...)` for each URL
    in `urls`, or sets `tool_calls = None` when `urls` is empty (simulating the model replying
    in plain text without calling any tool).
    """

    def __init__(self, urls):
        # Construct fake OpenAI tool call objects matching `call.id`, `call.function.name`, and `call.function.arguments`
        self.tool_calls = [
            SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name="download_paper_pdf", arguments=json.dumps({"url": u})))
            for i, u in enumerate(urls)
        ] or None

    def model_dump(self, **_):
        """Simulates Pydantic's `.model_dump()` when appending the assistant message to the conversation history."""
        return {"role": "assistant", "content": None, "tool_calls": []}


class FakeClient:
    """Each create() call returns the next scripted list of URLs the 'model' asks to download.

    For example:
    - `FakeClient([[PAPER.pdf_url]])` simulates 1 turn where the model calls `download_paper_pdf(PAPER.pdf_url)`.
    - `FakeClient([[PAPER.pdf_url], [PAPER.url]])` simulates turn 1 trying `pdf_url`, and turn 2 falling back to `url`.
    - `FakeClient([[]])` simulates the model returning no tool calls at all.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0  # Tracks how many times `chat.completions.create()` was invoked
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        """Simulates `client.chat.completions.create(...)` by popping the next turn's URLs from `self.script`."""
        self.calls += 1
        urls = self.script.pop(0) if self.script else []
        return SimpleNamespace(choices=[SimpleNamespace(message=FakeMessage(urls))])


def use_fake(monkeypatch, script) -> FakeClient:
    """Replaces `llm._client()` with a `FakeClient` running the given `script` of tool calls."""
    client = FakeClient(script)
    monkeypatch.setattr(llm, "_client", lambda: client)
    return client


# Sample search result with both a direct `pdf_url` and a fallback `url`
PAPER = SearchResult(title="T", pdf_url="https://host/a.pdf", url="https://host/landing")


def test_success_on_first_call(monkeypatch):
    """Verifies that if the model calls `download_paper_pdf` with `pdf_url` on the first turn
    and the download returns a valid PDF with text, `fetch_pdf_with_tool` immediately returns
    those PDF bytes after just 1 LLM call.
    """
    client = use_fake(monkeypatch, [[PAPER.pdf_url]])
    monkeypatch.setattr(pdf, "download_pdf", lambda url: make_pdf("hello world"))
    assert llm.fetch_pdf_with_tool(PAPER)[:5] == b"%PDF-"
    assert client.calls == 1


def test_falls_back_to_landing_url_after_failure(monkeypatch):
    """Verifies that when the first tool call (`pdf_url`) fails with HTTP 403, the error is fed
    back to the model, allowing it to try the fallback `PAPER.url` on turn 2 and succeed.
    """
    client = use_fake(monkeypatch, [[PAPER.pdf_url], [PAPER.url]])

    def download(url):
        if url == PAPER.pdf_url:
            raise pdf.PdfError("server returned HTTP 403")
        return make_pdf("hello world")

    monkeypatch.setattr(pdf, "download_pdf", download)
    assert llm.fetch_pdf_with_tool(PAPER)[:5] == b"%PDF-"
    assert client.calls == 2


def test_rejects_urls_the_model_invents(monkeypatch):
    """Security check: Verifies that if the LLM hallucinates or invents a URL not present in
    the paper's own `{pdf_url, url}`, the backend refuses to call `pdf.download_pdf` at all
    and raises HTTP 422.
    """
    client = use_fake(monkeypatch, [["https://evil.example/x.pdf"]])
    called = []
    monkeypatch.setattr(pdf, "download_pdf", lambda url: called.append(url) or make_pdf("hello world"))
    with pytest.raises(AppError) as e:
        llm.fetch_pdf_with_tool(PAPER)
    assert e.value.status == 422 and called == []


def test_gives_up_after_three_failed_calls(monkeypatch):
    """Verifies that the tool loop is strictly capped at `MAX_TOOL_CALLS` (3 attempts) even if
    the model keeps trying, and surfaces the last download error reason in the HTTP 422 message.
    """
    client = use_fake(monkeypatch, [[PAPER.pdf_url]] * 5)

    def fail(url):
        raise pdf.PdfError("server returned HTTP 403")

    monkeypatch.setattr(pdf, "download_pdf", fail)
    with pytest.raises(AppError) as e:
        llm.fetch_pdf_with_tool(PAPER)
    assert e.value.status == 422 and "403" in e.value.message
    assert client.calls == 3


def test_success_is_decided_by_the_backend_not_the_model(monkeypatch):
    """Verifies that if the model stops calling tools and replies in plain text without the
    backend having obtained valid PDF bytes, the backend raises HTTP 422.
    """
    client = use_fake(monkeypatch, [[]])  # model answers in prose without calling the tool
    with pytest.raises(AppError) as e:
        llm.fetch_pdf_with_tool(PAPER)
    assert e.value.status == 422


def test_pdf_without_text_counts_as_failure(monkeypatch):
    """Verifies that downloading a blank/scanned PDF (with no extractable text) is treated as a
    failed download (`pdf.check_has_text` fails) and raises HTTP 422.
    """
    client = use_fake(monkeypatch, [[PAPER.pdf_url]])
    monkeypatch.setattr(pdf, "download_pdf", lambda url: make_pdf())
    with pytest.raises(AppError):
        llm.fetch_pdf_with_tool(PAPER)


def test_ensure_pdf_caches(monkeypatch):
    """Verifies that `llm.ensure_pdf` returns cached PDF bytes on a cache hit without calling
    OpenAI (`llm._client`) at all.
    """
    paper = SearchResult(title="C", pdf_url="https://host/c.pdf", external_id="cache-loop")
    cache.put(cache.cache_key(paper), b"cached")
    monkeypatch.setattr(llm, "_client", lambda: pytest.fail("LLM must not be called on a cache hit"))
    assert llm.ensure_pdf(paper) == b"cached"


def test_non_object_tool_arguments_are_handled(monkeypatch):
    """Verifies defensive parsing when the model returns valid JSON that is not a JSON object
    (e.g. a JSON list `["not", "an", "object"]`) in `call.function.arguments`: it should fail
    cleanly with HTTP 422 instead of crashing with `AttributeError`.
    """
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
