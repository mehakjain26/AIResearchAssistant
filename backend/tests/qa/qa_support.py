"""Fakes and builders shared by the hermetic QA suite (no network, no real S3, no OpenAI key)."""
import json
from types import SimpleNamespace

import httpx
import pymupdf

from app.schemas import PaperMetadata


def make_pdf(*pages: str | None, metadata: dict | None = None) -> bytes:
    """A PDF with one page per argument. None makes a blank page (no extractable text)."""
    doc = pymupdf.open()
    for text in pages or (None,):
        page = doc.new_page()
        if text is not None:
            page.insert_text((72, 72), text)
    if metadata:
        doc.set_metadata(metadata)
    return doc.tobytes()


def atom_feed(*entries: dict) -> str:
    """An arXiv Atom response. Each entry: id, title, summary, published, authors."""
    parts = []
    for e in entries:
        authors = "".join(f"<author><name>{a}</name></author>" for a in e.get("authors", []))
        published = f"<published>{e['published']}</published>" if "published" in e else ""
        parts.append(
            f"<entry><id>{e.get('id', '')}</id>{published}<title>{e.get('title', '')}</title>"
            f"<summary>{e.get('summary', '')}</summary>{authors}</entry>"
        )
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{"".join(parts)}</feed>'


ATTENTION_ENTRY = {
    "id": "http://arxiv.org/abs/1706.03762v7",
    "published": "2017-06-12T17:57:34Z",
    "title": "Attention Is All You Need",
    "summary": "The dominant sequence transduction models.",
    "authors": ["Ashish Vaswani", "Noam Shazeer"],
}


class FakeArxiv:
    """Stands in for httpx.get inside app.services.arxiv. Returns queued responses in order."""

    def __init__(self):
        self.responses: list = []  # (status, text) tuples or exceptions
        self.requests: list[dict] = []

    def queue(self, status: int = 200, text: str = "") -> "FakeArxiv":
        self.responses.append((status, text))
        return self

    def queue_error(self, exc: Exception) -> "FakeArxiv":
        self.responses.append(exc)
        return self

    def __call__(self, url, params=None, headers=None, **kwargs):
        self.requests.append({"url": url, "params": params, "headers": headers, "options": kwargs})
        item = self.responses.pop(0) if self.responses else (200, atom_feed())
        if isinstance(item, Exception):
            raise item
        status, text = item
        return SimpleNamespace(status_code=status, text=text)

    @property
    def queries(self) -> list[str]:
        return [r["params"]["search_query"] for r in self.requests]


class _ToolMessage:
    def __init__(self, urls, raw_args=None):
        self.content = None
        self.tool_calls = [
            SimpleNamespace(
                id=f"call{i}",
                function=SimpleNamespace(name="download_paper_pdf",
                                         arguments=raw_args if raw_args is not None else json.dumps({"url": u})),
            )
            for i, u in enumerate(urls)
        ] or None

    def model_dump(self, **_):
        return {"role": "assistant", "content": None, "tool_calls": []}


class InvalidOpenAIRequest(AssertionError):
    """The app sent a request the real OpenAI API would reject."""


ROLES = {"system", "user", "assistant", "tool"}


def check_request(kind: str, kw: dict) -> None:
    """The parts of the Chat Completions contract the app relies on. The real API answers 400 to these."""
    def require(ok, why):
        if not ok:
            raise InvalidOpenAIRequest(why)

    require(isinstance(kw.get("model"), str) and kw["model"], f"model must be a non-empty string, got {kw.get('model')!r}")
    msgs = kw.get("messages")
    require(isinstance(msgs, list) and msgs, "messages must be a non-empty list")
    for m in msgs:
        require(m.get("role") in ROLES, f"unknown role in {m}")
        if m["role"] in ("system", "user"):
            require(isinstance(m.get("content"), str) and m["content"], f"{m['role']} message needs text content")
        if m["role"] == "tool":
            require(m.get("tool_call_id") and isinstance(m.get("content"), str), f"bad tool message {m}")
    if "tools" in kw:
        require(isinstance(kw["tools"], list) and kw["tools"], "tools must be a non-empty list")
        require(all(t.get("type") == "function" and t.get("function", {}).get("name") for t in kw["tools"]),
                "each tool needs type 'function' and a name")
        require(kw.get("tool_choice") in ("auto", "required", "none"), f"bad tool_choice {kw.get('tool_choice')!r}")
    if kind == "parse":
        require(kw.get("response_format") is not None, "parse() needs a response_format")


class FakeOpenAI:
    """Minimal stand-in for openai.OpenAI covering chat.completions.create and .parse.

    - Every request is checked against the API contract (check_request); a malformed one raises.
    - Requests with `tools` (the PDF-fetch loop) pop the next list of URLs from `tool_script`;
      by default the model asks for the paper's pdf_url.
    - Other create() calls return `reply`.
    - parse() returns `metadata`.
    - If `error` is set, every call raises it.
    """

    def __init__(self):
        self.reply: str | None = "Fake model reply."
        self.metadata: PaperMetadata | None = PaperMetadata(
            title="Extracted Title", authors=["Ada Lovelace"], year=2024, abstract="Extracted abstract.")
        self.tool_script: list[list[str] | str] | None = None
        self.error: Exception | None = None
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create, parse=self._parse))

    def _record(self, kind, kwargs):
        self.calls.append({"kind": kind, **kwargs})
        check_request(kind, kwargs)
        if self.error:
            raise self.error

    def _create(self, **kwargs):
        self._record("create", kwargs)
        if "tools" in kwargs:
            if self.tool_script is None:  # default: ask for the pdf_url found in the user message
                details = json.loads(kwargs["messages"][1]["content"].split(": ", 1)[1])
                urls = [details["pdf_url"] or details["url"]]
            else:
                urls = self.tool_script.pop(0) if self.tool_script else []
            if isinstance(urls, str):  # a raw arguments string, e.g. malformed JSON
                return SimpleNamespace(choices=[SimpleNamespace(message=_ToolMessage([""], raw_args=urls))])
            return SimpleNamespace(choices=[SimpleNamespace(message=_ToolMessage(urls))])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))])

    def _parse(self, **kwargs):
        self._record("parse", kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=self.metadata))])

    def calls_of(self, kind: str, with_tools: bool | None = None) -> list[dict]:
        out = [c for c in self.calls if c["kind"] == kind]
        if with_tools is not None:
            out = [c for c in out if ("tools" in c) == with_tools]
        return out

    @property
    def last_user_prompt(self) -> str:
        return self.calls[-1]["messages"][-1]["content"]


def mock_http(handler):
    """An httpx.Client factory that routes every request through `handler` (an httpx.MockTransport handler).
    The options each client was built with are kept in `factory.options`."""
    real_client = httpx.Client

    def factory(*args, **kwargs):
        factory.options.append(kwargs)
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    factory.options = []
    return factory


def chunked(*parts: bytes):
    """A response body delivered in several network chunks."""
    return iter(parts)


class WordEncoding:
    """A deterministic stand-in for a tiktoken encoding: one token per whitespace-separated word."""
    name = "words"

    def encode(self, text, disallowed_special=()):
        return text.split()
