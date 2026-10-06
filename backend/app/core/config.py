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
    API_V1_PREFIX: str = "/api/v1"
    LOG_LEVEL: str = "INFO"

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
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor. Use as a FastAPI dependency."""
    return Settings()
