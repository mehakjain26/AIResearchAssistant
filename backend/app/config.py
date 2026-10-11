from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    # Not required at startup, so search and the library work without a key.
    # LLM calls fail with a clear error when it is missing.
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1"
    max_input_tokens: int = 900_000

    s3_endpoint: str = "http://localhost:8333"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "papers"

    database_url: str = f"sqlite:///{BACKEND_DIR / 'data' / 'library.db'}"


settings = Settings()
