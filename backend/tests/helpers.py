import pymupdf


def make_pdf(text: str | None = None) -> bytes:
    """A one-page PDF containing `text`, or a blank page (no extractable text) when text is None."""
    doc = pymupdf.open()
    page = doc.new_page()
    if text is not None:
        page.insert_text((72, 72), text)
    return doc.tobytes()
