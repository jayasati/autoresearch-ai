"""
Application configuration.

Loads settings from environment variables / .env file.
No business logic here -- this module only declares the configuration
surface that later stages (retrieval, evidence, evaluation) will read from.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---
    APP_NAME: str = "AutoResearch AI"
    APP_ENV: Literal["development", "test", "production"] = "development"
    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"

    # --- API routing ---
    # Only the root prefix is configurable; version segments are derived, so a
    # version can never be half-renamed.
    API_PREFIX: str = "/api"

    # --- CORS (frontend origin) ---
    CORS_ORIGINS: str = "http://localhost:5173"

    # --- Foundation model (OpenAI) ---
    OPENAI_API_KEY: str | None = None
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_TIMEOUT_SECONDS: int = 90
    OPENAI_MAX_RETRIES: int = 3

    # --- Embeddings (Sentence Transformers, local) ---
    EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"
    EMBEDDING_DIM: int = 384
    EMBEDDING_BATCH_SIZE: int = 32

    # --- Vector store (ChromaDB) ---
    CHROMA_PERSIST_DIR: str = "../data/vectorstore"
    CHROMA_COLLECTION: str = "autoresearch_evidence"

    # --- Web search (Tavily) ---
    TAVILY_API_KEY: str | None = None
    TAVILY_MAX_RESULTS: int = 8

    # --- Academic search (Semantic Scholar) ---
    SEMANTIC_SCHOLAR_API_KEY: str | None = None
    SEMANTIC_SCHOLAR_MAX_RESULTS: int = 10

    # --- Structured data (PostgreSQL) ---
    DATABASE_URL: str = "postgresql+psycopg://postgres:postgres@localhost:5432/autoresearch"

    # --- Retrieval / chunking defaults ---
    CHUNK_SIZE: int = 900
    CHUNK_OVERLAP: int = 150
    TOP_K: int = 6

    # --- Orchestration limits (cost guardrails) ---
    MAX_SUBQUESTIONS: int = 6
    MAX_SOURCES_PER_RUN: int = 25
    MAX_LLM_CALLS_PER_RUN: int = 40

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS_ORIGINS is a comma-separated string so it can live in a .env file."""
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def API_V1_PREFIX(self) -> str:
        return f"{self.API_PREFIX}/v1"

    @property
    def HEALTH_URL(self) -> str:
        return f"{self.API_PREFIX}/health"

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    # --- Credential status -----------------------------------------------------
    # "Configured" is defined once, here, and read by both the startup warning and
    # /api/v1/system/capabilities. When the two disagreed, the endpoint cheerfully
    # reported `openai: true` for a .env still holding `sk-replace-me`.

    @staticmethod
    def _is_real_credential(value: str | None) -> bool:
        """A value counts only if it is present and not a template placeholder.

        .env.example ships `sk-replace-me` so the file is self-documenting, which
        means a merely non-empty string is not evidence of a usable credential.
        """
        if not value:
            return False
        return not value.strip().lower().endswith("replace-me")

    @property
    def integration_status(self) -> dict[str, bool]:
        """Which external integrations have usable credentials.

        Semantic Scholar and ChromaDB are always true: the first has a public API
        where a key only raises rate limits, the second is embedded and local.
        """
        return {
            "openai": self._is_real_credential(self.OPENAI_API_KEY),
            "tavily": self._is_real_credential(self.TAVILY_API_KEY),
            "semantic_scholar": True,
            "postgres": bool(self.DATABASE_URL),
            "chromadb": True,
        }

    @property
    def missing_credentials(self) -> list[str]:
        """Names of the environment variables still needing a real value."""
        return [
            name
            for name, value in (
                ("OPENAI_API_KEY", self.OPENAI_API_KEY),
                ("TAVILY_API_KEY", self.TAVILY_API_KEY),
            )
            if not self._is_real_credential(value)
        ]


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor. Use as a FastAPI dependency."""
    return Settings()
