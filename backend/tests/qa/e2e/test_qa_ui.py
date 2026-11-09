"""End-to-end user journeys in Chromium against the real frontend and backend."""
import re

import pytest
from playwright.sync_api import Page, expect

from qa_support import ATTENTION_ENTRY, atom_feed, make_pdf

pytestmark = pytest.mark.e2e

PDF_URL = "https://arxiv.org/pdf/1706.03762"
LONG_ABSTRACT = "Long abstract sentence. " * 40


def search(page: Page, query: str):
    page.goto("/")
    page.get_by_label("Search papers").fill(query)
    page.get_by_role("button", name="Search").click()


def card(page: Page, title: str):
    return page.locator("article.card").filter(has=page.get_by_role("heading", name=title))


# --------------------------------------------------------------------------- search page

def test_search_shows_result_details(page: Page, fake_arxiv):
    fake_arxiv.queue(200, atom_feed(ATTENTION_ENTRY))
    search(page, "attention is all you need")
    result = card(page, "Attention Is All You Need")
    expect(result).to_have_count(1)
    expect(result.locator(".meta")).to_have_text("Ashish Vaswani, Noam Shazeer · 2017")
    expect(result.locator(".abstract")).to_have_text("The dominant sequence transduction models.")
    expect(result.get_by_role("link", name="View paper ↗")).to_have_attribute("href", "https://arxiv.org/abs/1706.03762")
    assert fake_arxiv.queries == ['all:"attention is all you need"']


def test_result_with_missing_metadata_shows_placeholders(page: Page, fake_arxiv):
    fake_arxiv.queue(200, atom_feed({"id": "http://arxiv.org/abs/9999.0001v1", "title": "Bare paper"}))
    search(page, "bare")
    result = card(page, "Bare paper")
    expect(result.locator(".meta")).to_have_text("Unknown authors · Year unknown")
    expect(result.locator(".abstract")).to_have_text("Abstract not available.")


def test_search_with_no_results(page: Page, fake_arxiv):
    fake_arxiv.queue(200, atom_feed()).queue(200, atom_feed())
    search(page, "zzqqxx")
    expect(page.get_by_text("No papers found.")).to_be_visible()
    expect(page.locator("article.card")).to_have_count(0)


def test_blank_query_does_not_search(page: Page, fake_arxiv):
    """The frontend itself must not send a blank search (the backend would reject it and show an error)."""
    api_calls = []
    page.on("request", lambda req: "/api/search" in req.url and api_calls.append(req.url))
    search(page, "    ")
    # then a real search, so we know the page had time to send (and render) anything the blank one caused
    fake_arxiv.queue(200, atom_feed(ATTENTION_ENTRY))
    page.get_by_label("Search papers").fill("attention")
    page.get_by_role("button", name="Search").click()
    expect(card(page, "Attention Is All You Need")).to_have_count(1)
    assert api_calls == [f"{page.url.rstrip('/')}/api/search?q=attention"]
    expect(page.get_by_role("alert")).to_have_count(0)


def test_search_error_is_shown_to_the_user(page: Page, fake_arxiv):
    fake_arxiv.queue(429)
    search(page, "graphs")
    expect(page.get_by_role("alert")).to_have_text("arXiv rate limit reached. Wait a few seconds and try again.")


def test_long_abstract_can_be_expanded_and_collapsed(page: Page, fake_arxiv):
    fake_arxiv.queue(200, atom_feed({**ATTENTION_ENTRY, "summary": LONG_ABSTRACT}))
    search(page, "attention")
    abstract = page.locator(".abstract")
    expect(abstract).to_have_class(re.compile(r"\bclamped\b"))
    page.get_by_role("button", name="Show full abstract").click()
    expect(abstract).not_to_have_class(re.compile(r"\bclamped\b"))
    page.get_by_role("button", name="Show less").click()
    expect(abstract).to_have_class(re.compile(r"\bclamped\b"))


def test_save_from_search_then_find_it_in_library(page: Page, fake_arxiv, fake_openai, downloads, paper_rows):
    downloads.files[PDF_URL] = make_pdf("Transformer body text")
    fake_arxiv.queue(200, atom_feed(ATTENTION_ENTRY))
    search(page, "attention")
    result = card(page, "Attention Is All You Need")
    result.get_by_role("button", name="Save").click()
    expect(result.locator(".tag")).to_have_text("Saved")
    [row] = paper_rows()
    # once saved, "View paper" points at the stored PDF instead of arXiv
    expect(result.get_by_role("link", name="View paper ↗")).to_have_attribute("href", f"/api/papers/{row.id}/pdf")

    page.get_by_role("link", name="Library").click()
    expect(card(page, "Attention Is All You Need")).to_have_count(1)


def test_save_failure_is_reported_and_button_stays(page: Page, fake_arxiv, fake_openai, downloads, paper_rows):
    fake_arxiv.queue(200, atom_feed(ATTENTION_ENTRY))  # downloads.files is empty: every download 404s
    search(page, "attention")
    result = card(page, "Attention Is All You Need")
    result.get_by_role("button", name="Save").click()
    expect(result.get_by_role("alert")).to_contain_text("Could not save: Could not fetch the paper's PDF")
    expect(result.get_by_role("button", name="Save")).to_be_enabled()
    assert paper_rows() == []


def test_summarize_unsaved_result_renders_markdown(page: Page, fake_arxiv, fake_openai, downloads):
    downloads.files[PDF_URL] = make_pdf("Transformer body text")
    fake_openai.reply = "## Key results\n\n- **28.4** BLEU"
    fake_arxiv.queue(200, atom_feed(ATTENTION_ENTRY))
    search(page, "attention")
    card(page, "Attention Is All You Need").get_by_role("button", name="Summarize").click()
    summary = page.locator(".summary")
    expect(summary.get_by_role("heading", name="Key results")).to_be_visible()
    expect(summary.locator("strong")).to_have_text("28.4")
    assert "Transformer body text" in fake_openai.last_user_prompt


def test_summary_of_unfetchable_result_tells_the_user(page: Page, fake_arxiv, fake_openai, downloads):
    """FR-4.4: if the PDF cannot be fetched, the user is told and no summary is produced."""
    fake_arxiv.queue(200, atom_feed(ATTENTION_ENTRY))  # downloads.files is empty: every download 404s
    search(page, "attention")
    result = card(page, "Attention Is All You Need")
    result.get_by_role("button", name="Summarize").click()
    expect(result.get_by_role("alert")).to_have_text("Could not fetch the paper's PDF: server returned HTTP 404.")
    expect(result.locator(".summary")).to_have_count(0)
    assert fake_openai.calls_of("create", with_tools=False) == []  # the model was never asked to summarize


def test_panels_toggle_without_calling_the_api_again(page: Page, fake_arxiv, fake_openai, downloads):
    downloads.files[PDF_URL] = make_pdf("Transformer body text")
    fake_openai.reply = "Cached summary."
    fake_arxiv.queue(200, atom_feed(ATTENTION_ENTRY))
    search(page, "attention")
    result = card(page, "Attention Is All You Need")
    summarize = result.get_by_role("button", name="Summarize")
    summary = result.locator(".summary")

    summarize.click()
    expect(summary).to_have_text("Cached summary.")
    summarize.click()  # second click closes the panel
    expect(summary).to_be_hidden()
    result.get_by_role("button", name="Ask", exact=True).click()  # switching opens the other panel
    expect(result.get_by_label("Question")).to_be_visible()
    expect(summary).to_be_hidden()
    summarize.click()  # reopening shows the same summary
    expect(summary).to_have_text("Cached summary.")
    assert len(fake_openai.calls_of("create", with_tools=False)) == 1  # summarized once


# --------------------------------------------------------------------------- library page

def test_empty_library(page: Page):
    page.goto("/library")
    expect(page.get_by_text("No papers yet. Save one from Search or upload a PDF.")).to_be_visible()


def test_upload_pdf_adds_card_and_persists(page: Page, fake_openai, paper_rows, stored_keys):
    fake_openai.metadata = fake_openai.metadata.model_copy(update={"title": "My Uploaded Paper", "year": 2023})
    page.goto("/library")
    page.locator("input[type=file]").set_input_files(
        files=[{"name": "paper.pdf", "mimeType": "application/pdf", "buffer": make_pdf("Uploaded body")}])
    uploaded = card(page, "My Uploaded Paper")
    # FR-3.3: rendered like a search result: title, authors · year, abstract, a link to the paper, the actions
    expect(uploaded.locator(".meta")).to_have_text("Ada Lovelace · 2023")
    expect(uploaded.locator(".abstract")).to_have_text("Extracted abstract.")
    [row] = paper_rows()
    expect(uploaded.get_by_role("link", name="View paper ↗")).to_have_attribute("href", f"/api/papers/{row.id}/pdf")
    expect(uploaded.get_by_role("button", name="Summarize")).to_be_visible()
    expect(uploaded.get_by_role("button", name="Ask", exact=True)).to_be_visible()
    assert stored_keys() == [f"papers/{row.id}.pdf"]

    page.reload()  # it comes back from the server, not just local state
    expect(card(page, "My Uploaded Paper")).to_have_count(1)


def test_upload_non_pdf_shows_error(page: Page, fake_openai, paper_rows):
    page.goto("/library")
    page.locator("input[type=file]").set_input_files(
        files=[{"name": "notes.pdf", "mimeType": "application/pdf", "buffer": b"just text"}])
    expect(page.get_by_role("alert")).to_have_text("The uploaded file is not a PDF.")
    expect(page.get_by_role("button", name="Upload PDF")).to_be_enabled()
    assert paper_rows() == []


def test_delete_requires_confirmation(page: Page, seed, paper_rows):
    seed("Paper to delete", pdf_bytes=make_pdf("x"))
    page.goto("/library")
    target = card(page, "Paper to delete")

    page.once("dialog", lambda d: d.dismiss())
    target.get_by_role("button", name="Delete").click()
    expect(target).to_have_count(1)
    assert len(paper_rows()) == 1

    messages = []
    page.once("dialog", lambda d: (messages.append(d.message), d.accept()))
    target.get_by_role("button", name="Delete").click()
    expect(target).to_have_count(0)
    expect(page.get_by_text("No papers yet. Save one from Search or upload a PDF.")).to_be_visible()
    assert paper_rows() == []
    assert messages == ['Delete "Paper to delete" and its stored PDF? This cannot be undone.']


# --------------------------------------------------------------------------- paper page

def test_paper_page_summary_and_chat(page: Page, seed, fake_openai):
    pid = seed("Deep Paper", pdf_bytes=make_pdf("The method uses residual blocks."), abstract=LONG_ABSTRACT)
    page.goto(f"/papers/{pid}")
    expect(page.locator(".abstract")).not_to_have_class(re.compile(r"\bclamped\b"))  # full abstract on this page
    expect(page.get_by_role("button", name="Show full abstract")).to_have_count(0)

    fake_openai.reply = "A short summary."
    page.get_by_role("button", name="Summarize").click()
    expect(page.locator(".summary")).to_have_text("A short summary.")
    fake_openai.reply = "A regenerated summary."
    page.get_by_role("button", name="Regenerate").click()
    expect(page.locator(".summary")).to_have_text("A regenerated summary.")
    assert len(fake_openai.calls) == 2  # Regenerate asked the model again

    ask_button = page.get_by_role("button", name="Ask", exact=True)
    expect(ask_button).to_be_disabled()  # nothing typed yet
    fake_openai.reply = "Residual blocks."
    page.get_by_label("Question").fill("What does the method use?")
    ask_button.click()
    expect(page.locator(".msg.user")).to_have_text("What does the method use?")
    expect(page.locator(".msg.bot")).to_have_text("Residual blocks.")
    expect(page.get_by_label("Question")).to_have_value("")
    assert fake_openai.last_user_prompt.endswith("Question: What does the method use?")
    assert "residual blocks" in fake_openai.last_user_prompt


def test_chat_error_is_shown_inline(page: Page, seed):
    pid = seed("No key paper", pdf_bytes=make_pdf("text"))  # no fake_openai: server has no API key
    page.goto(f"/papers/{pid}")
    page.get_by_label("Question").fill("Anything?")
    page.get_by_role("button", name="Ask", exact=True).click()
    expect(page.locator(".chat").get_by_role("alert")).to_have_text("OPENAI_API_KEY is not configured on the server.")
    expect(page.locator(".msg.user")).to_have_text("Anything?")


def test_unknown_paper_page(page: Page):
    page.goto("/papers/424242")
    expect(page.get_by_role("alert")).to_have_text("Paper not found.")


def test_delete_from_paper_page_returns_to_library(page: Page, seed, paper_rows):
    pid = seed("Detail delete", pdf_bytes=make_pdf("x"))
    page.goto(f"/papers/{pid}")
    page.once("dialog", lambda d: d.accept())
    page.get_by_role("button", name="Delete").click()
    expect(page).to_have_url(re.compile(r"/library$"))
    expect(page.get_by_text("No papers yet. Save one from Search or upload a PDF.")).to_be_visible()
    assert paper_rows() == []
