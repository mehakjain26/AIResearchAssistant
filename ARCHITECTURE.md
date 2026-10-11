# AI Research Assistant: Architecture Plan (Draft v0.5)

Implements `REQUIREMENTS.md` (FR-1 to FR-5, including FR-2.4, FR-4.3, FR-5.2). Items marked **[design]** are implementation decisions needed to satisfy a requirement, not new features.

## 1. System Overview

```
┌──────────────────┐  HTTP/JSON  ┌────────────────────────────────┐
│ Browser (React)  │ ──────────► │ FastAPI backend                │
│ Search / Library │ ◄────────── │ routers → services             │
│ Paper detail     │             └─┬─────────┬──────────┬─────────┘
└──────────────────┘               │         │          │
                                   ▼         ▼          ▼
                          ┌──────────┐ ┌───────────┐ ┌────────────────────┐
                          │ SQLite   │ │ SeaweedFS │ │ External           │
                          │ metadata │ │ (S3)      │ │ arXiv (search),    │
                          │          │ │ PDFs      │ │ paper PDF hosts,   │
                          └──────────┘ └───────────┘ │ OpenAI API         │
                                                     └────────────────────┘
```

The browser only talks to our backend. API keys and storage credentials stay on the backend.

## 2. Technology Choices

| Layer | Choice | Why |
|-------|--------|-----|
| Frontend framework | **React 18** with **Vite** | Fast setup, suits three pages |
| Frontend routing | **React Router** | Search / Library / Paper pages |
| Frontend styling | Plain **CSS** (one stylesheet) | No extra framework |
| Backend language | **Python 3.11+** | Strong PDF and LLM library support |
| Backend framework | **FastAPI** with **Uvicorn** | Typed API, simple file upload |
| Validation / settings | **Pydantic v2**, **pydantic-settings** | Models and `.env` config |
| Upload parsing | **python-multipart** | Required by FastAPI for uploads |
| Database | **SQLite** | Local, persistent, no server |
| DB access | **SQLAlchemy 2.0** | Models and queries |
| Object store | **SeaweedFS** (S3-compatible, Apache-2.0, run with Docker Compose) | Stores PDFs outside the DB; same S3 API as AWS S3, so any S3-compatible store works |
| Object store client | **boto3** | S3 API client |
| Paper search | **arXiv API** (`export.arxiv.org`, Atom XML) | Free, no key; returns title, authors, year, abstract and every result has a PDF |
| HTTP client | **httpx** | Search calls and PDF downloads |
| PDF text extraction | **PyMuPDF** | Fast, accurate text extraction |
| LLM | **OpenAI API** via the official **`openai`** SDK | Summaries, Q&A, metadata extraction, tool calling |
| LLM model | **`gpt-4.1`**, set by `OPENAI_MODEL` | ~1M-token context, so the full paper text fits in one request |
| Token counting | **tiktoken** | Checks the paper fits the context window before calling |
| Testing | **pytest** | Core service tests |

## 3. Project Layout

```
AIResearchAssistant/
├── docker-compose.yml         # storage + backend + frontend
├── .env.example               # settings read by docker compose
├── README.md, ARCHITECTURE.md, REQUIREMENTS.md
├── mockups/index.html         # UI mockups
├── .claude/agents/            # code-reviewer, requirements-validator, docs-checker
├── backend/
│   ├── app/
│   │   ├── main.py            # FastAPI app, router registration, error handler
│   │   ├── config.py          # Settings from env / .env
│   │   ├── errors.py          # AppError (HTTP status + message)
│   │   ├── db.py              # Engine, session
│   │   ├── models.py          # SQLAlchemy models
│   │   ├── schemas.py         # Pydantic models
│   │   ├── routers/
│   │   │   ├── search.py      # GET /api/search
│   │   │   ├── papers.py      # library list/get, save, upload
│   │   │   ├── deps.py        # get_paper_or_404
│   │   │   └── analysis.py    # summarize, ask
│   │   └── services/
│   │       ├── arxiv.py       # arXiv search client
│   │       ├── storage.py     # S3 put/get/delete (SeaweedFS)
│   │       ├── pdf.py         # download (with URL safety checks), text extraction
│   │       ├── cache.py       # in-memory LRU of downloaded PDFs
│   │       └── llm.py         # summarize, answer, metadata extraction, download tool, prompts
│   ├── tests/                 # pytest (own temporary bucket and database); helpers.py builds test PDFs
│   ├── requirements.txt, Dockerfile
└── frontend/
    ├── package.json, vite.config.js, index.html, nginx.conf, Dockerfile
    └── src/
        ├── main.jsx, App.jsx, styles.css, api.js
        ├── pages/             # SearchPage, LibraryPage, PaperPage
        └── components/        # PaperCard, PaperActions, UploadButton, SummaryPanel, ChatPanel, Loading, ErrorAlert
```

## 4. Data Model (SQLite)

**papers**

| Column | Type | Notes |
|--------|------|-------|
| id | integer PK | |
| title | text | |
| authors | JSON list | |
| year | integer, nullable | |
| abstract | text, nullable | |
| url | text, nullable | Link to paper |

Only metadata is stored in SQLite. Every saved paper has a PDF in SeaweedFS (bucket `papers`, key `papers/{id}.pdf`, derived from the row `id`), because saving fails without one. Full text is not stored: it is extracted from the PDF with PyMuPDF whenever it is needed (summarize and ask). Extraction takes tens to a few hundred milliseconds, which is negligible next to the LLM call, and it keeps the PDF as the single source of truth.

`pdf_url` and `external_id` from the search result are used only inside requests (to tell the download tool where to fetch from, and as the in-memory cache key) and are not stored. Library order is `id` descending (newest first).

## 5. API Design

| Method | Endpoint | Satisfies | Behavior |
|--------|----------|-----------|----------|
| GET | `/api/search?q=` | FR-1 | Query arXiv (top 10), return title, authors, year, abstract, url, pdf_url |
| POST | `/api/papers` | FR-2.1, FR-2.4 | Save a search result: fetch PDF via tool call, check it has extractable text, store in SeaweedFS, insert row. Returns 422 and saves nothing if the download fails |
| GET | `/api/papers` | FR-2.3 | List saved and uploaded papers |
| GET | `/api/papers/{id}` | FR-4, FR-5 | One saved paper |
| GET | `/api/papers/{id}/pdf` | FR-3.3 | The stored PDF, served inline; the "View paper" link for every saved or uploaded paper |
| POST | `/api/papers/upload` | FR-3 | Accept PDF, extract text and metadata, store PDF in SeaweedFS, insert row |
| POST | `/api/summarize` | FR-4 | Body `{paper_id}` or `{paper: {title, url, pdf_url, external_id}}`; summary from the paper's full text. 422 if an unsaved paper's PDF can't be fetched |
| POST | `/api/ask` | FR-5 | Body `{paper_id | paper, question}`; answer from full text. Same 422 rule |

**Errors** (JSON `{"detail": "..."}`): 404 unknown paper or missing stored PDF; 413 upload over 50 MB; 422 PDF can't be fetched, not a PDF, no extractable text, empty question, or paper over `MAX_INPUT_TOKENS`; 429 arXiv rate limit; 502 arXiv or OpenAI failure; 503 `OPENAI_API_KEY` missing or object storage unavailable.

## 6. Key Flows

### 6.1 Search (FR-1)
1. `GET /api/search?q=...` → `arxiv.py` calls `https://export.arxiv.org/api/query`.
2. **[design]** The query is first sent as an exact phrase (`all:"graph attention networks"`). If that returns nothing, it is retried as an AND of the individual words with common stop words removed (arXiv does not index words like "is" or "all", so requiring them matches nothing). Requests are spaced at least 3 seconds apart, as arXiv asks.
3. Each Atom entry is normalized: title and abstract (whitespace collapsed), authors, year from the published date, `url` = `https://arxiv.org/abs/{id}`, `pdf_url` = `https://arxiv.org/pdf/{id}`, `external_id` = the arXiv id without its version. Results are rendered as `PaperCard`s.

### 6.2 Save (FR-2): the PDF must be fetched or the save fails
1. `POST /api/papers` with the selected result. The backend first gets the PDF (steps 2-3). No database row exists yet.
2. If the PDF for this paper is in the in-memory cache (from an earlier summarize or ask, see 6.4), it is used and nothing is downloaded.
3. Otherwise the backend calls OpenAI with the paper's metadata (title, `url`, `pdf_url`) and one function tool, `download_paper_pdf(url)`. The model calls the tool, starting with `pdf_url` and then `url` (at most 3 calls). For each call the backend checks that the URL is one of the paper's own links, downloads it (see the download rules below) and checks that the PDF has extractable text. The loop ends at the first valid PDF; success is decided by the backend holding valid PDF bytes, not by what the model says.
4. Only then: insert the row and flush (to get its `id`), upload the PDF to SeaweedFS as `papers/{id}.pdf`, and commit. If the upload or commit fails, the transaction is rolled back and the object is deleted.
5. **Save fails if the PDF can't be fetched.** If every attempt fails (no link, blocked host, not a PDF, no extractable text), the API returns HTTP 422 with the reason, e.g. "Could not fetch the paper's PDF: server returned HTTP 404.", and nothing is stored. The UI shows it on that paper's card as "Could not save: ...". Saving the same paper twice creates two entries; duplicates are allowed.

**Download rules** (`pdf.py`): only http(s) links; the host must resolve to a public address (no localhost, private or link-local addresses), checked again on every redirect; browser-like User-Agent, 30 s timeout; the response must start with `%PDF-` and is capped at 50 MB.

### 6.3 Upload (FR-3)
1. `POST /api/papers/upload` → PyMuPDF extracts the text. If there is none (for example, a scanned PDF), the upload is rejected with an error.
2. **[design]** The first ~2 pages of text go to OpenAI with Structured Outputs (a Pydantic schema for title, authors, year, abstract), which returns the four fields. PDF metadata is included as a hint.
3. The row is inserted and flushed to get its `id`, the PDF is stored in SeaweedFS as `papers/{id}.pdf`, and the transaction is committed. If the upload to SeaweedFS fails, the row is rolled back.
4. The new paper is returned; the UI renders it with the same `PaperCard`. `url` is empty for uploads. The "View paper" link of every saved paper opens its stored PDF (`GET /api/papers/{id}/pdf`); unsaved search results link to arXiv.

### 6.4 Summarize and Ask (FR-4, FR-5): saved or unsaved papers
Summarize and ask work on any paper: one in the library, or a search result that has not been saved. The request identifies the paper by `paper_id` (saved) or by the search result's metadata (unsaved).

**Saved paper (including uploads)**
1. Download `papers/{id}.pdf` from SeaweedFS and extract the full text with PyMuPDF. Every saved paper has a PDF. No tool is used.

**Unsaved search result**
1. The request carries the result's title, `url` and `pdf_url`.
2. The PDF is obtained exactly as in 6.2 steps 2-3: from the in-memory cache, else through the `download_paper_pdf` tool call (max 3 calls, same download rules).
3. **[design]** The downloaded PDF bytes (not the text) are kept in a server-side in-memory cache (LRU, at most 20 papers and 200 MB, not persisted), keyed by `external_id` (falling back to `pdf_url`, then `url`). Follow-up questions about the same paper reuse the cached PDF, and a later Save reuses it too.
4. The backend then extracts the full text from the PDF with PyMuPDF. If no PDF was obtained, the backend returns HTTP 422 saying the PDF could not be fetched. It never answers from the abstract or metadata. This check is enforced by the backend, not left to the model.

**Both cases**
5. `tiktoken` counts tokens of the full text before it is sent. The whole text goes to the model in a separate request, with the instruction (summarize, or the user's question). Each question is answered independently.
6. The system prompt says to answer only from the supplied paper and to say when the answer is not in it.
7. **[design]** If the text exceeds the model's context window, the API returns a clear error instead of silently truncating. With a ~1M-token model this should not happen for ordinary papers.

## 7. Frontend

| Page | Components | Calls |
|------|------------|-------|
| Search (`/`) | search box, `PaperCard` list with Save, Summarize and Ask buttons; Summarize/Ask open `SummaryPanel` and `ChatPanel` inline under the card | `GET /api/search`, `POST /api/papers`, `/api/summarize`, `/api/ask` |
| Library (`/library`) | `PaperCard` list with Open, Summarize and Ask (inline panels), `UploadButton` | `GET /api/papers`, `POST /api/papers/upload`, `/api/summarize`, `/api/ask` |
| Paper (`/papers/:id`) | header, `SummaryPanel`, `ChatPanel` | `GET /api/papers/{id}`, `/api/summarize`, `/api/ask` |

`SummaryPanel` and `ChatPanel` take either a saved `paper_id` or a search-result object, so both pages share them.

## 8. Configuration and Running

The project runs only in Docker. Copy `.env.example` to `.env`, set `OPENAI_API_KEY` (`OPENAI_MODEL` and `MAX_INPUT_TOKENS` are optional; they are the only settings read from this file), then `docker compose up --build`. Services: `storage` (SeaweedFS, S3 API; not published to the host), `backend` (FastAPI; not published to the host), and `frontend` (nginx serving the React build and proxying `/api` to the backend). Open http://localhost:8080. The SQLite file and the stored PDFs live in Docker volumes.

The `storage` service has a health check (its S3 API must answer), and `backend` starts only once it is healthy, so the first request after a start or restart never hits an object store that is still booting. The backend creates the bucket on startup, and again on the first upload if the store was not ready.

**Tests** run inside the backend container:
`docker compose up -d && docker compose run --rm -v "$PWD/backend/tests:/app/tests:ro" backend python -m pytest tests -q -p no:cacheprovider`
They use a throwaway bucket and a temporary database, so they never touch library data, and they need internet access.

## 9. Known Limitations

- If a PDF can't be downloaded (arXiv unreachable or rate-limited, a file over 50 MB, not a PDF), saving fails and summarize and ask on an unsaved result fail, and the user is told why. Papers from outside arXiv can be added by uploading their PDF.
- The in-memory PDF cache is lost when the backend restarts; the next request for an unsaved paper downloads it again.
- Metadata extraction from uploaded PDFs is LLM-based and can be wrong on unusual layouts.
- Scanned (image-only) PDFs have no extractable text and are not supported.
- Search covers arXiv only. arXiv asks for at most one request every 3 seconds, so the client spaces its requests; a 429 is shown to the user as a readable error.
