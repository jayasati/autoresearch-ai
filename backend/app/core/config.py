"""
Application configuration.

Loads settings from environment variables / .env file.
No business logic here -- this module only declares the configuration
surface that later stages (retrieval, evidence, evaluation) will read from.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
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
    # None means "do not send the parameter": some models (the reasoning family)
    # reject temperature outright, and the provider's default is then the right one.
    # The benchmark will want 0.0 here, for runs that are as repeatable as the
    # provider allows.
    OPENAI_TEMPERATURE: float | None = None

    # --- Foundation model (Azure OpenAI) ---
    # The alternative to OPENAI_API_KEY: when the endpoint, key AND deployment name
    # are all set, the LLM service talks to this Azure resource instead, and the
    # deployment name is what "model" means. Azure keys are opaque strings with no
    # `sk-` prefix, which is how one in OPENAI_API_KEY was mis-diagnosed once —
    # hence separate, explicitly named settings. Timeout, retries and temperature
    # are shared with the OPENAI_* settings; only the credentials differ.
    AZURE_OPENAI_ENDPOINT: str | None = None
    AZURE_OPENAI_API_KEY: str | None = None
    AZURE_OPENAI_API_VERSION: str = "2024-12-01-preview"
    AZURE_OPENAI_DEPLOYMENT_NAME: str | None = None

    # --- Embeddings (Sentence Transformers, local) ---
    EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"
    EMBEDDING_DIM: int = 384
    EMBEDDING_BATCH_SIZE: int = 32

    # --- Vector store (ChromaDB) ---
    CHROMA_PERSIST_DIR: str = "../data/vectorstore"
    CHROMA_COLLECTION: str = "autoresearch_evidence"

    # --- Web search (Tavily) ---
    TAVILY_API_KEY: str | None = None
    TAVILY_BASE_URL: str = "https://api.tavily.com"

    # "basic" is one round of retrieval; "advanced" costs more Tavily credits and
    # digs deeper. Configurable because the benchmark will want to hold it constant
    # across modes, and because it is a real cost lever.
    TAVILY_SEARCH_DEPTH: Literal["basic", "advanced"] = "basic"
    TAVILY_MAX_RESULTS: int = 8

    # Total time allowed for one search, including connect. A search that has not
    # answered in this long will not become useful by waiting longer, and the
    # orchestrator has a whole run to get through.
    TAVILY_TIMEOUT_SECONDS: float = 20.0

    # Attempts, not retries: 3 means one try plus two more. Only timeouts, 429s and
    # 5xx are retried -- a bad API key will never succeed, so retrying it just
    # delays the error.
    TAVILY_MAX_ATTEMPTS: int = 3
    TAVILY_BACKOFF_BASE_SECONDS: float = 0.5
    TAVILY_BACKOFF_MAX_SECONDS: float = 10.0

    # --- Academic search (Semantic Scholar) ---
    # The public API works without a key; a key raises the quota. So an absent key is
    # not a misconfiguration here, unlike every other credential in this file.
    SEMANTIC_SCHOLAR_API_KEY: str | None = None
    SEMANTIC_SCHOLAR_BASE_URL: str = "https://api.semanticscholar.org"
    SEMANTIC_SCHOLAR_MAX_RESULTS: int = 10

    # Semantic Scholar allows ONE REQUEST PER SECOND, cumulative across all
    # endpoints, and asks callers to stay below that threshold. This is a client-side
    # throttle applied before sending, not just 429 handling: with a limit this tight,
    # firing six sub-question searches at once would collect five rejections and spend
    # the run's budget backing off from a limit it could simply have respected.
    #
    # 1.25s, not 1.1s, on evidence: three real requests spaced 1.1s apart still drew
    # one 429. The retry and the shared penalty absorbed it, but a rejection costs an
    # extra round trip and a backoff, so a little more headroom is cheaper than the
    # retry it avoids.
    SEMANTIC_SCHOLAR_MIN_INTERVAL_SECONDS: float = 1.25

    # Longer than Tavily's: Semantic Scholar is slower, and the throttle means a
    # queued request may wait a second or more before it is even sent.
    SEMANTIC_SCHOLAR_TIMEOUT_SECONDS: float = 30.0

    # One more attempt than Tavily, with a longer floor, because a 429 here is an
    # expected condition rather than an anomaly.
    SEMANTIC_SCHOLAR_MAX_ATTEMPTS: int = 4
    SEMANTIC_SCHOLAR_BACKOFF_BASE_SECONDS: float = 1.0
    SEMANTIC_SCHOLAR_BACKOFF_MAX_SECONDS: float = 30.0

    # --- Structured data (PostgreSQL) ---
    # The single source of the connection string. Never assembled from separate
    # host/user/password settings: one URL is what every tool already understands
    # (psql, Alembic, a container platform's secret injection), and splitting it
    # would create four places a credential could be hardcoded instead of one.
    #
    # The default points at a local database with placeholder credentials, so a
    # fresh checkout fails to connect rather than silently reaching something real.
    DATABASE_URL: str = "postgresql+psycopg://postgres:CHANGEME@localhost:5432/autoresearch"

    # Connection pool. Defaults sized for a single-process dev server; a deployment
    # running N workers multiplies these, so PostgreSQL's max_connections must be
    # at least N * (POOL_SIZE + MAX_OVERFLOW).
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 1800  # seconds; below any server-side idle timeout
    DB_ECHO: bool = False  # log every statement -- noisy, useful when debugging
    # Seconds to wait for a TCP connect. Without a bound, an unreachable host can
    # block until the OS gives up -- which would hang the readiness probe, the one
    # request that must always answer promptly.
    DB_CONNECT_TIMEOUT: int = 5

    # --- Benchmark dataset ---
    # The question sets live in the repo (data/benchmarks), versioned in git, because
    # the dataset is part of the method: results against an unversioned dataset are
    # not reproducible claims.
    BENCHMARK_DATA_DIR: str = "../data/benchmarks"

    # --- Retrieval / chunking defaults ---
    CHUNK_SIZE: int = 900
    CHUNK_OVERLAP: int = 150
    TOP_K: int = 6
    # How SEARCH_GROUNDED picks its top-k evidence passages. "similarity" is the
    # original plain cosine top-k; "mmr" adds a redundancy penalty (lambda 0.7),
    # which measured better on the development pools (P@4 0.458 -> 0.583,
    # distinct sources 2.83 -> 3.5, recall unchanged; stage 26). Kept optional so
    # the change is a recorded configuration, never a silent redefinition.
    EVIDENCE_SELECTION: Literal["similarity", "mmr"] = "similarity"

    # --- Orchestration limits (cost guardrails) ---
    MAX_SUBQUESTIONS: int = 6
    MAX_SOURCES_PER_RUN: int = 25
    MAX_LLM_CALLS_PER_RUN: int = 40

    # --- report-quality-v1 (docs/REPORT_QUALITY_PIPELINE.md) -------------------
    # Declared in stage 27 (S0) and read by NOTHING yet: no code path selects the
    # pipeline, so these values change no behaviour. Every number below is an
    # unmeasured starting hypothesis from the design, to be tuned on the
    # development set only and frozen before validation (design §10, D6/D7).
    # Later stages copy the effective values into the run configuration, so a
    # changed knob is a changed, recorded configuration.
    #
    # Server-side gate: even once the request field exists, the pipeline is
    # refused unless this is switched on. Off by default.
    REPORT_QUALITY_ENABLED: bool = False
    # Hard per-run budgets, by depth. Attempts count every LLM request sent,
    # retries included; tokens are prompt + completion.
    REPORT_QUALITY_MAX_LLM_ATTEMPTS_QUICK: int = Field(default=60, gt=0)
    REPORT_QUALITY_MAX_LLM_ATTEMPTS_MEDIUM: int = Field(default=100, gt=0)
    REPORT_QUALITY_MAX_LLM_ATTEMPTS_DEEP: int = Field(default=160, gt=0)
    REPORT_QUALITY_MAX_TOTAL_TOKENS_QUICK: int = Field(default=120_000, gt=0)
    REPORT_QUALITY_MAX_TOTAL_TOKENS_MEDIUM: int = Field(default=220_000, gt=0)
    REPORT_QUALITY_MAX_TOTAL_TOKENS_DEEP: int = Field(default=400_000, gt=0)
    # Claims beyond the cap are recorded as not evaluated — never silently dropped.
    REPORT_QUALITY_MAX_CLAIMS_VERIFIED_QUICK: int = Field(default=30, gt=0)
    REPORT_QUALITY_MAX_CLAIMS_VERIFIED_MEDIUM: int = Field(default=40, gt=0)
    REPORT_QUALITY_MAX_CLAIMS_VERIFIED_DEEP: int = Field(default=50, gt=0)
    REPORT_QUALITY_RUN_DEADLINE_SECONDS_QUICK: int = Field(default=600, gt=0)
    REPORT_QUALITY_RUN_DEADLINE_SECONDS_MEDIUM: int = Field(default=900, gt=0)
    REPORT_QUALITY_RUN_DEADLINE_SECONDS_DEEP: int = Field(default=1500, gt=0)
    # Share of the claim budget held back for re-verifying revised sentences.
    REPORT_QUALITY_REVERIFY_RESERVE_FRACTION: float = Field(default=0.20, ge=0.0, le=1.0)
    # Final-report outcome thresholds over body_loss_ratio (design §3).
    REPORT_QUALITY_DEGRADED_BODY_LOSS_RATIO: float = Field(default=0.10, ge=0.0, le=1.0)
    REPORT_QUALITY_FAIL_BODY_LOSS_RATIO: float = Field(default=0.25, ge=0.0, le=1.0)
    REPORT_QUALITY_MIN_CORE_COVERAGE: float = Field(default=0.5, ge=0.0, le=1.0)
    # Claim-to-sentence alignment and meaning-change detection (design §1.2, V2).
    REPORT_QUALITY_ALIGNMENT_MIN_MARGIN: float = Field(default=0.05, ge=0.0, le=1.0)
    REPORT_QUALITY_MEANING_CHANGE_MIN_COSINE: float = Field(default=0.90, ge=0.0, le=1.0)
    # Quantitative audit (audit-only in v1): relative tolerance for "about X".
    REPORT_QUALITY_QUANT_APPROX_REL_TOLERANCE: float = Field(default=0.10, ge=0.0, le=1.0)
    # Added in stage 28 (S1), equally provisional: number-masked cosine at which two
    # claims are compared as the same metric, and the cosine at which two body
    # sentences are flagged (never removed) as near-duplicates.
    REPORT_QUALITY_QUANT_GROUP_MIN_COSINE: float = Field(default=0.85, ge=0.0, le=1.0)
    REPORT_QUALITY_DUPLICATE_MIN_COSINE: float = Field(default=0.92, ge=0.0, le=1.0)
    # Added in stage 30 (S3), approved as provisional defaults: planned subquestions
    # and selected evidence passages per depth (passages are split across the
    # subquestions), and the cosine at which two planned subquestions are merged.
    REPORT_QUALITY_SUBQUESTIONS_QUICK: int = Field(default=3, gt=0)
    REPORT_QUALITY_SUBQUESTIONS_MEDIUM: int = Field(default=4, gt=0)
    REPORT_QUALITY_SUBQUESTIONS_DEEP: int = Field(default=6, gt=0)
    REPORT_QUALITY_EVIDENCE_PASSAGES_QUICK: int = Field(default=8, gt=0)
    REPORT_QUALITY_EVIDENCE_PASSAGES_MEDIUM: int = Field(default=12, gt=0)
    REPORT_QUALITY_EVIDENCE_PASSAGES_DEEP: int = Field(default=18, gt=0)
    REPORT_QUALITY_PLAN_DUPLICATE_MIN_COSINE: float = Field(default=0.90, ge=0.0, le=1.0)
    # Added in stage 31 (S4), approved as provisional: output tokens for the structured
    # (JSON) draft, ~1.5x the legacy markdown limits for the per-sentence structure.
    REPORT_QUALITY_DRAFT_MAX_TOKENS_QUICK: int = Field(default=1400, gt=0)
    REPORT_QUALITY_DRAFT_MAX_TOKENS_MEDIUM: int = Field(default=2400, gt=0)
    REPORT_QUALITY_DRAFT_MAX_TOKENS_DEEP: int = Field(default=4200, gt=0)

    @model_validator(mode="after")
    def report_quality_thresholds_are_ordered(self) -> "Settings":
        """A report cannot fail before it degrades: degraded <= fail."""
        if self.REPORT_QUALITY_DEGRADED_BODY_LOSS_RATIO > self.REPORT_QUALITY_FAIL_BODY_LOSS_RATIO:
            raise ValueError(
                "REPORT_QUALITY_DEGRADED_BODY_LOSS_RATIO must not exceed "
                "REPORT_QUALITY_FAIL_BODY_LOSS_RATIO"
            )
        for depth in ("QUICK", "MEDIUM", "DEEP"):
            subquestions = getattr(self, f"REPORT_QUALITY_SUBQUESTIONS_{depth}")
            passages = getattr(self, f"REPORT_QUALITY_EVIDENCE_PASSAGES_{depth}")
            if passages < subquestions:
                raise ValueError(
                    f"REPORT_QUALITY_EVIDENCE_PASSAGES_{depth} must be at least "
                    f"REPORT_QUALITY_SUBQUESTIONS_{depth}: every subquestion needs a passage slot"
                )
        return self

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

    # --- Database URL handling -------------------------------------------------

    @field_validator("DATABASE_URL")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        """Pin the driver, so one URL cannot mean two different things.

        `postgresql://` leaves the driver to SQLAlchemy's default, which is
        psycopg2 -- a package this project does not install. The failure is a
        confusing ImportError at first connection rather than a clear
        configuration error, and it happens to whoever pastes a connection string
        from a hosting dashboard, since that is the form they all hand out.
        """
        if value.startswith("postgres://"):
            # Heroku-style URLs; SQLAlchemy dropped support for this scheme.
            value = value.replace("postgres://", "postgresql+psycopg://", 1)
        elif value.startswith("postgresql://"):
            value = value.replace("postgresql://", "postgresql+psycopg://", 1)
        return value

    @property
    def database_is_postgres(self) -> bool:
        return self.DATABASE_URL.startswith("postgresql")

    @property
    def database_backend(self) -> str:
        """The backend the URL names: "postgresql", "sqlite", or whatever it says.

        Reported in health responses. Labelling a SQLite connection "postgresql"
        would be a small lie told at exactly the moment someone is diagnosing a
        misconfigured environment.
        """
        scheme = self.DATABASE_URL.split("://", 1)[0]
        return scheme.split("+", 1)[0]

    @property
    def database_url_safe(self) -> str:
        """The connection string with its password removed, for logs and responses."""
        url = self.DATABASE_URL
        if "@" not in url or "://" not in url:
            return url
        scheme, rest = url.split("://", 1)
        credentials, host = rest.rsplit("@", 1)
        user = credentials.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"

    @property
    def database_credentials_look_unset(self) -> bool:
        """True while DATABASE_URL still carries the shipped placeholder."""
        return "CHANGEME" in self.DATABASE_URL

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
    def llm_provider(self) -> Literal["azure_openai", "openai"] | None:
        """Which foundation-model provider the credentials select, if any.

        Azure wins when both are configured: setting three deliberate values
        (endpoint, key, deployment) is a stronger statement of intent than one.
        All three are required — an endpoint without a deployment name has nothing
        to address a request to, and must read as "not configured", not "half works".
        """
        azure_complete = (
            bool(self.AZURE_OPENAI_ENDPOINT)
            and self._is_real_credential(self.AZURE_OPENAI_API_KEY)
            and bool(self.AZURE_OPENAI_DEPLOYMENT_NAME)
        )
        if azure_complete:
            return "azure_openai"
        if self._is_real_credential(self.OPENAI_API_KEY):
            return "openai"
        return None

    @property
    def llm_default_model(self) -> str:
        """What "model" means by default: the Azure deployment name, or OPENAI_MODEL."""
        if self.llm_provider == "azure_openai" and self.AZURE_OPENAI_DEPLOYMENT_NAME:
            return self.AZURE_OPENAI_DEPLOYMENT_NAME
        return self.OPENAI_MODEL

    @property
    def integration_status(self) -> dict[str, bool]:
        """Which external integrations have usable credentials.

        Semantic Scholar and ChromaDB are always true: the first has a public API
        where a key only raises rate limits, the second is embedded and local.
        """
        return {
            # Either provider counts: "openai" here means "a foundation model is
            # reachable", which is the question the capabilities endpoint answers.
            "openai": self.llm_provider is not None,
            "tavily": self._is_real_credential(self.TAVILY_API_KEY),
            # True regardless of the key: the public API is usable without one, and a
            # key only raises the quota. Reporting False would tell the user to go and
            # fix something that is not broken.
            "semantic_scholar": True,
            # A DATABASE_URL still holding the shipped CHANGEME placeholder is not
            # a configured database, for the same reason sk-replace-me is not a key.
            "postgres": not self.database_credentials_look_unset,
            "chromadb": True,
        }

    @property
    def missing_credentials(self) -> list[str]:
        """Names of the environment variables still needing a real value.

        OPENAI_API_KEY is listed only when NO foundation-model provider is
        configured — a working Azure setup must not produce a warning telling the
        user to go and fix something that is not broken.
        """
        missing: list[str] = []
        if self.llm_provider is None:
            missing.append("OPENAI_API_KEY")
        if not self._is_real_credential(self.TAVILY_API_KEY):
            missing.append("TAVILY_API_KEY")
        if self.database_credentials_look_unset:
            missing.append("DATABASE_URL")
        return missing


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor. Use as a FastAPI dependency."""
    return Settings()
