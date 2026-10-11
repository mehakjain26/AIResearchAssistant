import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/test.db"
os.environ["OPENAI_API_KEY"] = ""
# Never touch the real library's bucket: tests write PDFs under ids that collide with real papers.
TEST_BUCKET = f"test-{uuid.uuid4().hex[:12]}"
os.environ["S3_BUCKET"] = TEST_BUCKET


def pytest_sessionfinish(session, exitstatus):
    try:
        from app.config import settings
        from app.services import storage

        s3 = storage._s3()
        for obj in s3.list_objects_v2(Bucket=settings.s3_bucket).get("Contents", []):
            s3.delete_object(Bucket=settings.s3_bucket, Key=obj["Key"])
        s3.delete_bucket(Bucket=settings.s3_bucket)
    except Exception:
        pass  # store may be unavailable; nothing to clean up
