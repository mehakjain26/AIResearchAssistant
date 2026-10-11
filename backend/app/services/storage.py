import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from ..config import settings
from ..errors import AppError

_client = None
_bucket_ready = False


def _s3():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name="us-east-1",
            config=Config(s3={"addressing_style": "path"}, connect_timeout=5, retries={"max_attempts": 1}),
        )
    return _client


def key_for(paper_id: int) -> str:
    return f"papers/{paper_id}.pdf"


def ensure_bucket() -> None:
    """Creates the bucket on first use; later calls are free."""
    global _bucket_ready
    if _bucket_ready:
        return
    s3 = _s3()
    try:
        s3.head_bucket(Bucket=settings.s3_bucket)
    except ClientError:
        s3.create_bucket(Bucket=settings.s3_bucket)
    _bucket_ready = True


def _storage_error(e: Exception) -> AppError:
    return AppError(503, f"Object storage is unavailable: {e}")


def put_pdf(paper_id: int, data: bytes) -> None:
    try:
        ensure_bucket()
        _s3().put_object(Bucket=settings.s3_bucket, Key=key_for(paper_id), Body=data, ContentType="application/pdf")
    except (BotoCoreError, ClientError) as e:
        raise _storage_error(e)


def get_pdf(paper_id: int) -> bytes:
    try:
        obj = _s3().get_object(Bucket=settings.s3_bucket, Key=key_for(paper_id))
        return obj["Body"].read()
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            raise AppError(404, "The stored PDF for this paper is missing.")
        raise _storage_error(e)
    except BotoCoreError as e:
        raise _storage_error(e)


def delete_pdf(paper_id: int) -> None:
    try:
        _s3().delete_object(Bucket=settings.s3_bucket, Key=key_for(paper_id))
    except (BotoCoreError, ClientError):
        pass  # best-effort cleanup
