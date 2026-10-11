# AI Research Assistant

A web app for finding research papers, keeping them in a local library, and using an LLM to summarize them and answer questions about them. Answers and summaries are based on the **full text** of the paper's PDF.

## Features

| Feature | What it does |
|---------|--------------|
| **Search** | Search arXiv by keyword or topic (no account or API key needed; top 10 results). Each result shows title, authors, year, abstract and a link. |
| **Save to library** | Save a search result. The app downloads the paper's PDF, stores it, and keeps the paper in a local database. If the PDF can't be fetched, the save fails and you are told why. Saved papers persist across page refreshes and restarts. |
| **Upload a PDF** | Upload your own PDF. Its title, authors, year and abstract are extracted automatically (by the LLM, from the first pages) and the paper appears in the library like any other. |
| **Summarize** | Generate a summary (problem, approach, results, limitations) from the paper's full text. Works on library papers and on search results you haven't saved. |
| **Ask questions** | Ask natural-language questions ("What datasets are used?", "What are the limitations?"). Each answer comes from the paper's full text; the model is told to say so when the paper doesn't contain the answer. |

## How it works

```
Browser (React) ──/api──► FastAPI backend ──► SQLite        paper metadata
                                          ├─► S3 object store (SeaweedFS)   PDF files
                                          ├─► arXiv API                     paper search (no key)
                                          └─► OpenAI API                    summaries, Q&A, metadata, PDF-fetch tool call
```

- **Search** calls the arXiv API (`export.arxiv.org`). It tries your words as an exact phrase first and, if nothing matches, falls back to matching the words in any order. Every arXiv paper has a PDF, so every result can be saved. Requests are spaced at least 3 seconds apart, as arXiv asks.
- **Save** asks the OpenAI model to fetch the PDF through a `download_paper_pdf` tool. The model may only use the paper's own links, at most 3 attempts. The backend downloads the file, checks that it is a PDF with extractable text, stores it in the object store as `papers/{id}.pdf`, and inserts the row. Any failure rolls everything back.
- **Summarize / Ask** on an unsaved search result use the same tool call to fetch the PDF (kept in a small in-memory cache so follow-up questions don't download it again). On a saved paper, the PDF is read from the object store. Either way, the text is extracted with PyMuPDF and the **whole text** is sent to the model.
- **Upload** extracts the text, asks the model for the title, authors, year and abstract (structured output), stores the PDF and inserts the row.
- SQLite stores only metadata (`id, title, authors, year, abstract, url`). PDFs live in the object store; full text is re-extracted from the PDF when needed.

More detail is in [`ARCHITECTURE.md`](ARCHITECTURE.md); the requirements are in [`REQUIREMENTS.md`](REQUIREMENTS.md).

## Project structure

```
.
├── backend/                 FastAPI app (Python)
│   ├── app/
│   │   ├── main.py          app setup, error handling
│   │   ├── errors.py        AppError (HTTP status + message)
│   │   ├── config.py        settings from environment / .env
│   │   ├── db.py, models.py SQLite + SQLAlchemy model
│   │   ├── schemas.py       Pydantic request/response models
│   │   ├── routers/         search.py, papers.py (library, upload), analysis.py (summarize, ask)
│   │   └── services/        arxiv.py, pdf.py, storage.py, llm.py, cache.py
│   ├── tests/               pytest tests
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/                React + Vite app
│   ├── package.json, vite.config.js, index.html
│   ├── src/pages/           SearchPage, LibraryPage, PaperPage
│   ├── src/components/      PaperCard, PaperActions, SummaryPanel, ChatPanel, UploadButton, Loading, ErrorAlert
│   ├── src/api.js           calls to the backend
│   ├── nginx.conf, Dockerfile
├── mockups/index.html       UI mockups
├── docker-compose.yml       storage + backend + frontend
├── .env.example             settings for docker compose
├── ARCHITECTURE.md, REQUIREMENTS.md
```

## Tech stack

- **Frontend:** React 18, Vite, React Router, plain CSS
- **Backend:** Python, FastAPI, Uvicorn, Pydantic v2 (+ pydantic-settings), SQLAlchemy, python-multipart, tiktoken (token counting)
- **Database:** SQLite
- **Object store:** SeaweedFS (S3-compatible), accessed with boto3. Any S3-compatible store works by changing the `S3_*` settings.
- **PDF processing:** PyMuPDF
- **External APIs:** arXiv API (search), OpenAI API (LLM), `httpx` for downloads

## Run it (Docker only)

The project is built to run in Docker; nothing else needs to be installed. Requires Docker with Compose.

```bash
cp .env.example .env          # then put your key in OPENAI_API_KEY
docker compose up --build
```

Open **http://localhost:8080**.

This starts three containers (the backend waits for the object store to be healthy, so allow about 20 seconds on a cold start): `storage` (object store), `backend` (API) and `frontend` (nginx serving the app and proxying `/api`). The database and PDFs are kept in Docker volumes, so they survive restarts. Stop with `docker compose down` (add `-v` to also delete the data).

Without an `OPENAI_API_KEY`, search and the library still work, but save, upload, summarize and ask return an error saying the key is not configured.

## Configuration

Settings are read from `.env` in the project root (copy `.env.example`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `OPENAI_API_KEY` | none | Required for save, upload, summarize and ask |
| `OPENAI_MODEL` | `gpt-4.1` | Model used for all LLM calls. It must support tool calling and structured output, and have a context window large enough for a whole paper. |
| `MAX_INPUT_TOKENS` | `900000` | Papers longer than this return an error instead of being truncated |

The storage connection is fixed in `docker-compose.yml`. To change any other backend setting (see `backend/app/config.py`), add it under `backend.environment` there.

## API

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/api/search?q=` | Search papers |
| POST | `/api/papers` | Save a search result (422 if its PDF can't be fetched) |
| GET | `/api/papers` | List the library, newest first |
| GET | `/api/papers/{id}` | One saved paper |
| GET | `/api/papers/{id}/pdf` | The stored PDF, shown inline (the "View paper" link for saved and uploaded papers) |
| POST | `/api/papers/upload` | Upload a PDF (multipart field `file`) |
| POST | `/api/summarize` | Body `{"paper_id": 1}` or `{"paper": {...search result...}}` |
| POST | `/api/ask` | Same as above plus `"question"` |

## Tests

```bash
docker compose up -d
docker compose run --rm -v "$PWD/backend/tests:/app/tests:ro" backend python -m pytest tests -q -p no:cacheprovider
```

The tests run inside the backend container (needs internet access for the live arXiv checks). The tests use their own temporary database and a throwaway bucket, so they never touch your library. They cover search parsing, PDF download and extraction, storage, saving and the failure paths, request validation, and the PDF-fetch tool loop (using a fake OpenAI client). Calls to the real OpenAI API are not part of the test suite.

## Limitations

- Search covers arXiv only (preprints in physics, maths, computer science and related fields), not journals or conference papers that never went to arXiv. To use any other paper, download its PDF and use **Upload**.
- Downloads accept only public http(s) links, up to 50 MB (uploads are capped at 50 MB too).
- If a PDF can't be downloaded (arXiv unreachable, rate-limited, or the file is not a PDF), saving or summarizing an unsaved paper fails with a message.
- Scanned (image-only) PDFs have no extractable text and are rejected.
- Metadata extracted from uploaded PDFs comes from an LLM and can be wrong on unusual layouts.
- Questions are answered one at a time from the paper's text. The chat shown in the page is not stored or sent back as context.
- Saving the same paper twice creates two entries.
- There is no delete or edit for library papers.
