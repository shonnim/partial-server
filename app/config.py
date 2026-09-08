from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(alias="DATABASE_URL")

    # RustFS speaks the S3 API on its own port; the value in the environment
    # must be the API endpoint, not the web console.
    rustfs_endpoint_url: str = Field(alias="RUSTFS_ENDPOINT_URL")
    rustfs_access_key: str = Field(alias="RUSTFS_ACCESS_KEY")
    rustfs_secret_key: str = Field(alias="RUSTFS_SECRET_KEY")
    rustfs_region: str = Field(default="us-east-1", alias="RUSTFS_REGION")

    bucket_documents: str = Field(alias="BUCKET_DOCUMENTS")
    bucket_derived: str = Field(alias="BUCKET_DERIVED")

    # The OpenAI-compatible endpoint used to turn a pasted table of contents
    # into structured headers. Self-hosted, so the base URL is not optional.
    llm_base_url: str = Field(alias="LLM_BASE_URL")
    llm_api_key: str = Field(alias="LLM_API_KEY")
    llm_model: str = Field(alias="LLM_MODEL")
    llm_timeout_seconds: float = Field(default=180.0, alias="LLM_TIMEOUT_SECONDS")

    # The document parsing service the Pages method sends a contents page to.
    # One VLM pass over a few pages is slow, so the timeout is generous.
    doc_extract_base_url: str = Field(
        default="https://doc-extract-api.ihopper.co.kr", alias="DOC_EXTRACT_BASE_URL"
    )
    doc_extract_timeout_seconds: float = Field(
        default=600.0, alias="DOC_EXTRACT_TIMEOUT_SECONDS"
    )

    # The MinerU service the Parsing page submits whole documents to. Tasks are
    # asynchronous there: submit, poll, then fetch a zip of the output.
    mineru_base_url: str = Field(
        default="https://cl1.gpuhub.nhncloud.com:50128/ZP4VnibPzP/mineru",
        alias="MINERU_BASE_URL",
    )
    # Uploading a PDF is the slow request; polling and health are quick.
    mineru_submit_timeout_seconds: float = Field(
        default=600.0, alias="MINERU_SUBMIT_TIMEOUT_SECONDS"
    )
    mineru_poll_timeout_seconds: float = Field(
        default=30.0, alias="MINERU_POLL_TIMEOUT_SECONDS"
    )
    # The result is a zip of markdown, JSON, and every page crop as an image.
    mineru_result_timeout_seconds: float = Field(
        default=600.0, alias="MINERU_RESULT_TIMEOUT_SECONDS"
    )

    @property
    def sqlalchemy_url(self) -> str:
        """DATABASE_URL with the psycopg (v3) driver made explicit.

        A bare ``postgresql://`` URL resolves to the psycopg2 dialect, which is
        not installed; this project uses psycopg 3.
        """
        url = self.database_url
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+psycopg://", 1)
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
