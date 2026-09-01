from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(alias="DATABASE_URL")

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
