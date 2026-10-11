import re
import threading
import time
import xml.etree.ElementTree as ET

import httpx

from ..errors import AppError
from ..schemas import SearchResult

API = "https://export.arxiv.org/api/query"
NS = {"a": "http://www.w3.org/2005/Atom"}
MIN_INTERVAL = 3.0  # arXiv asks for at most one request every 3 seconds

_lock = threading.Lock()
_last_request = 0.0


# arXiv does not index very common words, so requiring them with AND matches nothing.
STOP_WORDS = {"a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it", "of", "on", "or",
              "the", "to", "with", "all", "you", "we", "this", "that"}


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+", text)


def _phrase_query(text: str) -> str:
    """The words as an exact phrase: 'graph attention networks' -> all:"graph attention networks"."""
    words = _words(text)
    return f'all:"{" ".join(words)}"' if words else ""


def _terms_query(text: str) -> str:
    """Fallback: every meaningful word must appear somewhere, in any order."""
    words = [w for w in _words(text) if w.lower() not in STOP_WORDS] or _words(text)
    return " AND ".join(f"all:{w}" for w in words)


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def parse(xml_text: str) -> list[SearchResult]:
    root = ET.fromstring(xml_text)
    results = []
    for e in root.findall("a:entry", NS):
        abs_url = _clean(e.findtext("a:id", namespaces=NS))
        arxiv_id = re.sub(r"v\d+$", "", abs_url.rsplit("/abs/", 1)[-1])
        if not arxiv_id:
            continue
        published = e.findtext("a:published", namespaces=NS) or ""
        results.append(
            SearchResult(
                title=_clean(e.findtext("a:title", namespaces=NS)) or "Untitled",
                authors=[_clean(n.text) for n in e.findall("a:author/a:name", NS) if _clean(n.text)],
                year=int(published[:4]) if published[:4].isdigit() else None,
                abstract=_clean(e.findtext("a:summary", namespaces=NS)) or None,
                url=f"https://arxiv.org/abs/{arxiv_id}",
                pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
                external_id=arxiv_id,
            )
        )
    return results


def _fetch(query: str, limit: int) -> list[SearchResult]:
    global _last_request
    with _lock:  # keep to arXiv's request-rate guideline
        wait = MIN_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = httpx.get(
                API,
                params={"search_query": query, "start": 0, "max_results": limit, "sortBy": "relevance"},
                timeout=20,
                follow_redirects=True,
            )
        except httpx.HTTPError as e:
            raise AppError(502, f"Could not reach arXiv: {e}")
        finally:
            _last_request = time.monotonic()
    if resp.status_code == 429:
        raise AppError(429, "arXiv rate limit reached. Wait a few seconds and try again.")
    if resp.status_code != 200:
        raise AppError(502, f"arXiv returned HTTP {resp.status_code}")
    try:
        return parse(resp.text)
    except ET.ParseError:
        raise AppError(502, "arXiv returned an unreadable response.")


def search(query: str, limit: int = 10) -> list[SearchResult]:
    phrase = _phrase_query(query)
    if not phrase:
        return []
    results = _fetch(phrase, limit)
    if not results:  # no paper contains the exact phrase; match the words in any order
        results = _fetch(_terms_query(query), limit)
    return results
