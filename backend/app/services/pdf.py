import ipaddress
import socket
from urllib.parse import urlparse

import httpx
import pymupdf

from ..errors import AppError

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/pdf,*/*",
}


class PdfError(AppError):
    """A PDF could not be downloaded or read. Reaches the client as HTTP 422."""

    def __init__(self, message: str):
        super().__init__(422, message)


def is_pdf(data: bytes) -> bool:
    return data.lstrip()[:5] == b"%PDF-"


MAX_PDF_BYTES = 50 * 1024 * 1024


def _check_public_url(url: str) -> None:
    """Only http(s) links to public internet hosts may be fetched (no localhost, private or link-local addresses)."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PdfError("only http(s) links can be downloaded")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise PdfError("could not resolve the link's host")
    if not infos or any(not ipaddress.ip_address(i[4][0]).is_global for i in infos):
        raise PdfError("links to private or local addresses are not allowed")


def download_pdf(url: str) -> bytes:
    try:
        # The request hook also runs for every redirect, so a redirect can't lead to an internal address.
        with httpx.Client(headers=_HEADERS, follow_redirects=True, timeout=30,
                          event_hooks={"request": [lambda r: _check_public_url(str(r.url))]}) as client:
            with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise PdfError(f"server returned HTTP {resp.status_code}")
                chunks, total = [], 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > MAX_PDF_BYTES:
                        raise PdfError(f"the file is larger than {MAX_PDF_BYTES // (1024 * 1024)} MB")
                    chunks.append(chunk)
    except httpx.HTTPError as e:
        raise PdfError(f"download failed: {e}")
    data = b"".join(chunks)
    if not is_pdf(data):
        raise PdfError("the link did not return a PDF")
    return data


def _open(data: bytes) -> pymupdf.Document:
    try:
        return pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:
        raise PdfError(f"could not read the PDF: {e}")


_NO_TEXT = "the PDF has no extractable text (scanned PDFs are not supported)"


def _has_text(doc: pymupdf.Document) -> bool:
    return any(page.get_text().strip() for page in doc)  # stops at the first page with text


def check_has_text(data: bytes) -> None:
    with _open(data) as doc:
        if not _has_text(doc):
            raise PdfError(_NO_TEXT)


def extract_text(data: bytes) -> str:
    """Full text of the PDF. Raises PdfError if there is none (e.g. a scanned PDF)."""
    with _open(data) as doc:
        text = "\n".join(page.get_text() for page in doc).replace("\x00", "").strip()
    if not text:
        raise PdfError(_NO_TEXT)
    return text


def read_upload(data: bytes, pages: int = 2) -> tuple[str, dict]:
    """Opens an uploaded PDF once: checks it has text, returns the first pages' text and the PDF's own metadata."""
    with _open(data) as doc:
        if not _has_text(doc):
            raise PdfError(_NO_TEXT)
        text = "\n".join(doc[i].get_text() for i in range(min(pages, len(doc)))).replace("\x00", "")
        return text, {k: v for k, v in (doc.metadata or {}).items() if v}
