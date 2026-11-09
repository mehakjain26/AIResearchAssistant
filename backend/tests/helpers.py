"""Shared test helper utilities for generating in-memory PDFs."""
import pymupdf


def make_pdf(text: str | None = None) -> bytes:
    """A one-page PDF containing `text`, or a blank page (no extractable text) when text is None.

    Args:
        text: Optional string to write onto the PDF page.
              - Pass a string (e.g. `make_pdf("Hello")`) to simulate a valid text PDF.
              - Pass `None` (`make_pdf()`) to simulate a scanned/image-only PDF with no extractable text.

    Returns:
        Raw PDF file bytes (`b"%PDF-..."`) generated in memory by PyMuPDF.
    """
    # Create an empty in-memory PDF document
    doc = pymupdf.open()
    # Add a single blank page
    page = doc.new_page()
    # Write text at coordinates (72, 72) if text was provided
    if text is not None:
        page.insert_text((72, 72), text)
    # Serialize the PDF document to raw bytes
    return doc.tobytes()
