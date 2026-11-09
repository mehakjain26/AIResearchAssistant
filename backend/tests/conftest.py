"""Pytest configuration and global test environment setup.

Pytest automatically loads `conftest.py` before running any test files.
This module isolates the test suite from production/development data by:
1. Pointing SQLAlchemy (`DATABASE_URL`) at a fresh temporary SQLite file.
2. Clearing `OPENAI_API_KEY` so tests never accidentally make paid OpenAI API calls.
3. Pointing S3 (`S3_BUCKET`) at a unique throwaway bucket in SeaweedFS and deleting
   that bucket when the test session finishes.
"""
import os
import sys
import tempfile
import uuid
from pathlib import Path

# Add `backend/` to sys.path so tests can import `from app...` directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Use a brand-new temporary SQLite database file for this test run.
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/test.db"

# Ensure no real OpenAI key is set during tests unless explicitly mocked/overridden.
os.environ["OPENAI_API_KEY"] = ""

# Never touch the real library's bucket: tests write PDFs under ids that collide with real papers.
TEST_BUCKET = f"test-{uuid.uuid4().hex[:12]}"
os.environ["S3_BUCKET"] = TEST_BUCKET


def pytest_sessionfinish(session, exitstatus):
    """Pytest hook that runs automatically once all tests finish.

    Cleans up the temporary S3 bucket (`TEST_BUCKET`) by deleting every stored
    PDF object inside it and then deleting the bucket itself.
    """
    try:
        from app.config import settings
        from app.services import storage

        s3 = storage._s3()
        # List and delete all objects in the temporary bucket (S3 requires buckets to be empty before deletion).
        for obj in s3.list_objects_v2(Bucket=settings.s3_bucket).get("Contents", []):
            s3.delete_object(Bucket=settings.s3_bucket, Key=obj["Key"])
        s3.delete_bucket(Bucket=settings.s3_bucket)
    except Exception:
        pass  # store may be unavailable; nothing to clean up
