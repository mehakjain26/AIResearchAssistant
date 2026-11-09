"""Fixtures for the hermetic QA suite.

Every test here runs against:
- a fresh in-memory SQLite database (via a get_db override),
- an in-process fake S3 (moto) instead of the real object store,
- an empty PDF cache,
- no network: arXiv, PDF downloads and OpenAI are replaced by fakes when a test asks for them.
"""
import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.models import Paper
from botocore.exceptions import EndpointConnectionError

from app.services import arxiv, cache, llm, pdf, storage
from qa_support import FakeArxiv, FakeOpenAI, WordEncoding, make_pdf


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: browser tests (need npm and Playwright's Chromium)")


@pytest.fixture(autouse=True)
def s3(monkeypatch):
    """A fresh fake S3 per test. Returns a boto3 client for inspecting what the app stored."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    # A misconfigured client must fail fast, not hang probing the EC2 metadata service for credentials or a region.
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setattr(settings, "s3_endpoint", None)  # default AWS endpoint, which moto intercepts
    monkeypatch.setattr(storage, "_client", None)
    monkeypatch.setattr(storage, "_bucket_ready", False)
    with mock_aws():
        yield boto3.client("s3", region_name="us-east-1")


@pytest.fixture(autouse=True)
def empty_cache():
    cache._pdfs.clear()
    yield
    cache._pdfs.clear()


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    """Tests start without an OpenAI key; fake_openai replaces the client when a test needs the model."""
    monkeypatch.setattr(settings, "openai_api_key", "")


@pytest.fixture(autouse=True)
def session_factory():
    """A fresh in-memory database wired into the app. Use it to seed and inspect rows."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)

    def override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    yield factory
    app.dependency_overrides.pop(get_db, None)
    engine.dispose()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def lenient_client():
    """Returns 500 responses instead of re-raising unhandled server errors, like a real deployment."""
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def fake_openai(monkeypatch) -> FakeOpenAI:
    fake = FakeOpenAI()
    monkeypatch.setattr(llm, "_client", lambda: fake)
    return fake


@pytest.fixture
def fake_arxiv(monkeypatch) -> FakeArxiv:
    fake = FakeArxiv()
    monkeypatch.setattr(arxiv.httpx, "get", fake)
    monkeypatch.setattr(arxiv, "MIN_INTERVAL", 0)
    return fake


@pytest.fixture
def downloads(monkeypatch):
    """Replaces the real PDF download. Map URL -> bytes or exception in `downloads.files`; calls are recorded."""
    class Downloads:
        files: dict = {}
        calls: list = []

    d = Downloads()
    d.files, d.calls = {}, []

    def fake(url):
        d.calls.append(url)
        result = d.files.get(url, pdf.PdfError("server returned HTTP 404"))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(pdf, "download_pdf", fake)
    return d


@pytest.fixture
def seed(session_factory, s3, client):
    """Inserts a library paper (and optionally its stored PDF). Returns the new id."""
    def _seed(title="Seeded paper", pdf_bytes: bytes | None = None, **fields) -> int:
        with session_factory() as db:
            row = Paper(title=title, authors=fields.pop("authors", []), **fields)
            db.add(row)
            db.commit()
            pid = row.id
        if pdf_bytes is not None:
            storage.put_pdf(pid, pdf_bytes)
        return pid

    return _seed


@pytest.fixture
def stored_keys(s3):
    def _keys() -> list[str]:
        try:
            return [o["Key"] for o in s3.list_objects_v2(Bucket=settings.s3_bucket).get("Contents", [])]
        except s3.exceptions.NoSuchBucket:
            return []
    return _keys


@pytest.fixture
def paper_rows(session_factory):
    def _rows() -> list[Paper]:
        with session_factory() as db:
            return list(db.scalars(select(Paper).order_by(Paper.id)))
    return _rows


@pytest.fixture
def text_pdf():
    return make_pdf("Transformers replace recurrence with attention.")


@pytest.fixture
def word_tokens(monkeypatch):
    """Counts one token per word, so token-limit tests need no tiktoken download and have exact boundaries."""
    monkeypatch.setattr(llm, "_encoding", lambda model: WordEncoding())


class UnreachableS3:
    """An S3 client whose every call fails the way boto3 does when the store is down (connection refused)."""
    calls: list

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(**kwargs):
            self.calls.append(name)
            raise EndpointConnectionError(endpoint_url="http://storage:8333")
        return call


@pytest.fixture
def storage_down(monkeypatch) -> UnreachableS3:
    """Object storage is unreachable from now on (a real outage, not a missing bucket)."""
    down = UnreachableS3()
    monkeypatch.setattr(storage, "_client", down)
    monkeypatch.setattr(storage, "_bucket_ready", False)
    return down
