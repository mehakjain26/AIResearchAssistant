# AI Research Assistant: Application Requirements (Draft v0.3)

Source: project brief. Every requirement below is stated in, or directly implied by, the brief.

## 1. Mandatory Components

- A graphical user interface
- Frontend and backend logic
- Integration with at least one external API for paper search or related functionality
- A local database to store and retrieve papers
- PDF upload and processing
- Integration with an LLM API to summarize papers and answer user questions

## 2. Functional Requirements

### FR-1 Search for Research Papers
- FR-1.1 The user can enter keywords or a research topic and search for relevant papers, using a public paper-search API or service.
- FR-1.2 Each search result shows:
  - Paper title
  - Authors
  - Publication year
  - Abstract
  - Link to the paper, when available

### FR-2 Save Papers to a Local Library
- FR-2.1 The user can select papers from the search results and save them to a local database.
- FR-2.2 Saved papers are still available after the application is restarted or the page is refreshed.
- FR-2.3 The user can browse previously saved papers.
- FR-2.4 Saving a paper stores the paper's PDF itself, not only its metadata, whenever the PDF is retrievable from the paper's link. The full text is extracted from the stored PDF when it is needed.
- FR-2.5 If the PDF cannot be downloaded, the save fails, nothing is added to the library, and the user is notified.

### FR-3 Upload PDF Papers
- FR-3.1 The user can upload a research paper in PDF format and add it to the local database.
- FR-3.2 After upload, the application extracts the paper's title, authors, publication year, and abstract.
- FR-3.3 The uploaded paper is rendered in the application in the same way as papers in search results.

### FR-4 Summarize Papers with an LLM
- FR-4.1 The user can select a paper and ask an LLM to generate a summary.
- FR-4.2 The summary is based on the content of the selected paper, not only its title or metadata.
- FR-4.3 The summary uses the full text of the paper.
- FR-4.4 Summaries can be requested for search results that have not been saved. The paper's PDF is fetched on demand through an LLM tool call; if it cannot be fetched, the user is told and no summary is produced.

### FR-5 Ask Questions About a Paper
- FR-5.1 The user can ask natural-language questions about a selected paper and receive an answer from an LLM.
- FR-5.2 Answers use the full text of the paper.
- FR-5.3 Questions can be asked about search results that have not been saved, with the PDF fetched on demand as in FR-4.4.
- FR-5.4 Example questions the application should handle:
  - What problem does this paper address?
  - What is the main idea of the proposed approach?
  - What datasets are used?
  - What are the major limitations?
  - How does this method compare with the baselines?

## 3. Technology

The stack is free to choose. A suggestion (not a requirement):

- Frontend: React (or plain HTML/JS)
- Backend: Python FastAPI
- Database: SQLite
- PDF text extraction: PyMuPDF or pypdf
- PDF storage: S3-compatible object store (SeaweedFS locally)
- Paper search: arXiv API (free, no key)
- LLM: OpenAI API

## 4. Open Questions

- The brief ends mid-sentence ("Your application does not need to be…"). Is there a remaining constraint, such as being hosted or deployed?
