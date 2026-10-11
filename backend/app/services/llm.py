import contextlib
import functools
import json

import openai
import tiktoken

from ..config import settings
from ..errors import AppError
from ..schemas import PaperMetadata, SearchResult
from . import cache, pdf

MAX_TOOL_CALLS = 3

DOWNLOAD_TOOL = {
    "type": "function",
    "function": {
        "name": "download_paper_pdf",
        "description": "Download a paper's PDF from a URL.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "URL to download the PDF from"}},
            "required": ["url"],
        },
    },
}

FETCH_SYSTEM = (
    "You retrieve the PDF of a research paper. Call download_paper_pdf with the paper's pdf_url first. "
    "If that fails, try its url. Only use URLs given in the paper details."
)

QA_SYSTEM = (
    "You answer questions about one research paper. The full text of the paper is provided between "
    "<paper> tags. Base every answer only on that text. If the paper does not contain the answer, say so "
    "instead of guessing."
)

SUMMARY_PROMPT = (
    "Summarize this paper for a researcher. Cover: the problem it addresses, the proposed approach, "
    "the key results, and the main limitations. Use short sections and bullet points."
)


@functools.lru_cache(maxsize=1)
def _openai(api_key: str) -> openai.OpenAI:
    return openai.OpenAI(api_key=api_key)  # one client, so connections are reused


def _client() -> openai.OpenAI:
    if not settings.openai_api_key:
        raise AppError(503, "OPENAI_API_KEY is not configured on the server.")
    return _openai(settings.openai_api_key)


@contextlib.contextmanager
def _openai_errors():
    try:
        yield
    except openai.OpenAIError as e:
        raise AppError(502, f"OpenAI request failed: {e}")


def fetch_pdf_with_tool(paper: SearchResult) -> bytes:
    """Let the model call download_paper_pdf (max 3 calls). Success means the backend
    holds validated PDF bytes, regardless of what the model says."""
    allowed = {u for u in (paper.pdf_url, paper.url) if u}
    if not allowed:
        raise AppError(422, "This paper has no link to download a PDF from.")

    details = json.dumps({"title": paper.title, "pdf_url": paper.pdf_url, "url": paper.url})
    messages = [
        {"role": "system", "content": FETCH_SYSTEM},
        {"role": "user", "content": f"Fetch the PDF for this paper: {details}"},
    ]
    client = _client()
    calls = 0
    last_error = "the model did not request a download"
    with _openai_errors():
        for turn in range(MAX_TOOL_CALLS):
            resp = client.chat.completions.create(
                model=settings.openai_model,
                messages=messages,
                tools=[DOWNLOAD_TOOL],
                tool_choice="required" if turn == 0 else "auto",
            )
            msg = resp.choices[0].message
            if not msg.tool_calls:
                break
            messages.append(msg.model_dump(exclude_none=True))
            for call in msg.tool_calls:
                calls += 1
                result = "error: too many download attempts"
                if calls <= MAX_TOOL_CALLS:
                    try:
                        args = json.loads(call.function.arguments)
                        url = args.get("url", "") if isinstance(args, dict) else ""
                        if url not in allowed:
                            raise pdf.PdfError("that URL is not one of the paper's links")
                        data = pdf.download_pdf(url)
                        pdf.check_has_text(data)
                        return data
                    except (pdf.PdfError, json.JSONDecodeError) as e:
                        last_error = str(e)
                        result = f"error: {e}"
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            if calls >= MAX_TOOL_CALLS:
                break
    raise AppError(422, f"Could not fetch the paper's PDF: {last_error}.")


def ensure_pdf(paper: SearchResult) -> bytes:
    """PDF bytes for a search result: from the in-memory cache, else via the tool call."""
    key = cache.cache_key(paper)
    data = cache.get(key)
    if data is None:
        data = fetch_pdf_with_tool(paper)
        cache.put(key, data)
    return data


@functools.lru_cache(maxsize=1)
def _encoding(model: str):
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        return tiktoken.get_encoding("o200k_base")


def _check_length(text: str) -> None:
    # A token is at least one byte, so text no longer than the limit in bytes can't exceed it in tokens.
    if len(text.encode()) <= settings.max_input_tokens:
        return
    tokens = len(_encoding(settings.openai_model).encode(text, disallowed_special=()))
    if tokens > settings.max_input_tokens:
        raise AppError(
            422, f"This paper is too long for the model ({tokens:,} tokens; limit {settings.max_input_tokens:,})."
        )


def _complete(system: str, user: str) -> str:
    client = _client()
    with _openai_errors():
        resp = client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
    return resp.choices[0].message.content or ""


def summarize(text: str) -> str:
    _check_length(text)
    return _complete(QA_SYSTEM, f"<paper>\n{text}\n</paper>\n\n{SUMMARY_PROMPT}")


def answer(text: str, question: str) -> str:
    _check_length(text)
    return _complete(QA_SYSTEM, f"<paper>\n{text}\n</paper>\n\nQuestion: {question}")


def extract_metadata(first_pages_text: str, pdf_meta: dict) -> PaperMetadata:
    client = _client()
    prompt = (
        "Extract the title, authors, publication year and abstract of this research paper from the text of its "
        "first pages. Use null for year or abstract if they are not present. PDF metadata hint: "
        f"{json.dumps(pdf_meta)}\n\n{first_pages_text[:20000]}"
    )
    with _openai_errors():
        resp = client.chat.completions.parse(
            model=settings.openai_model,
            messages=[{"role": "user", "content": prompt}],
            response_format=PaperMetadata,
        )
    parsed = resp.choices[0].message.parsed
    if parsed is None:
        raise AppError(502, "The model did not return paper metadata.")
    return parsed
