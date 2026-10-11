import threading
from collections import OrderedDict

MAX_ENTRIES = 20
MAX_BYTES = 200 * 1024 * 1024

_lock = threading.Lock()
_pdfs: "OrderedDict[str, bytes]" = OrderedDict()


def cache_key(paper) -> str | None:
    return paper.external_id or paper.pdf_url or paper.url


def get(key: str | None) -> bytes | None:
    if not key:
        return None
    with _lock:
        data = _pdfs.get(key)
        if data is not None:
            _pdfs.move_to_end(key)
        return data


def put(key: str | None, data: bytes) -> None:
    if not key:
        return
    with _lock:
        _pdfs[key] = data
        _pdfs.move_to_end(key)
        while len(_pdfs) > 1 and (len(_pdfs) > MAX_ENTRIES or sum(map(len, _pdfs.values())) > MAX_BYTES):
            _pdfs.popitem(last=False)
