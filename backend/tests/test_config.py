"""Configuration: defaults, environment loading, and derived values."""

import os

import pytest

from app.core.config import Settings, get_settings
from app.core.constants import PipelineVersion, ResearchMode


class TestDefaults:
    def test_sensible_defaults_without_any_env(self, clean_env):
        s = Settings(_env_file=None)
        assert s.APP_NAME == "AutoResearch AI"
        assert s.APP_ENV == "development"
        assert s.API_PREFIX == "/api"

    def test_no_credential_is_invented(self, clean_env):
        """Absent keys must be None, never a placeholder that looks usable."""
        s = Settings(_env_file=None)
        assert s.OPENAI_API_KEY is None
        assert s.TAVILY_API_KEY is None

    def test_budget_guardrails_are_set(self, clean_env):
        s = Settings(_env_file=None)
        assert s.MAX_LLM_CALLS_PER_RUN > 0
        assert s.MAX_SOURCES_PER_RUN > 0
        assert s.MAX_SUBQUESTIONS > 0


class TestEnvironmentLoading:
    def test_values_come_from_the_environment(self, monkeypatch, clean_env):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
        monkeypatch.setenv("MAX_SUBQUESTIONS", "3")
        s = Settings(_env_file=None)
        assert s.APP_ENV == "production"
        assert s.OPENAI_MODEL == "gpt-4o"
        assert s.MAX_SUBQUESTIONS == 3

    def test_field_names_are_case_insensitive(self, monkeypatch, clean_env):
        monkeypatch.setenv("openai_model", "gpt-4.1-mini")
        assert Settings(_env_file=None).OPENAI_MODEL == "gpt-4.1-mini"

    def test_unknown_variables_are_ignored(self, monkeypatch, clean_env):
        """An unrelated variable in the shell must not stop the app booting."""
        monkeypatch.setenv("SOMETHING_UNRELATED", "x")
        assert Settings(_env_file=None).APP_NAME == "AutoResearch AI"

    def test_invalid_environment_name_is_rejected(self, monkeypatch, clean_env):
        monkeypatch.setenv("APP_ENV", "staging")
        with pytest.raises(Exception, match="APP_ENV|validation"):
            Settings(_env_file=None)

    def test_non_numeric_budget_is_rejected(self, monkeypatch, clean_env):
        monkeypatch.setenv("MAX_LLM_CALLS_PER_RUN", "lots")
        with pytest.raises(Exception, match="MAX_LLM_CALLS_PER_RUN|validation"):
            Settings(_env_file=None)


class TestDerivedValues:
    def test_version_prefixes_follow_the_root_prefix(self):
        """Renaming the root must not leave a half-renamed version path behind."""
        s = Settings(API_PREFIX="/backend-api")
        assert s.API_V1_PREFIX == "/backend-api/v1"
        assert s.HEALTH_URL == "/backend-api/health"

    def test_cors_origins_parse_from_a_comma_separated_string(self):
        s = Settings(CORS_ORIGINS="http://a.test, http://b.test ,, ")
        assert s.cors_origin_list == ["http://a.test", "http://b.test"]

    def test_empty_cors_yields_no_origins(self):
        assert Settings(CORS_ORIGINS="").cors_origin_list == []

    def test_is_production_flag(self):
        assert Settings(APP_ENV="production").is_production is True
        assert Settings(APP_ENV="development").is_production is False


class TestCredentialStatus:
    """A placeholder is not a credential.

    `.env.example` ships `sk-replace-me` so the file documents itself, which means
    "non-empty" is not evidence of a usable key. This was a real inconsistency:
    the startup warning checked for the placeholder and the capabilities endpoint
    did not, so it reported `openai: true` for an untouched .env.
    """

    # Settings also reads the developer's real .env, so tests asserting "openai is
    # NOT configured" must switch Azure off explicitly or the .env's Azure values
    # would satisfy the provider rule and the assertion would test the .env.
    NO_AZURE = {"AZURE_OPENAI_ENDPOINT": None, "AZURE_OPENAI_API_KEY": None}

    def test_placeholder_does_not_count_as_configured(self):
        s = Settings(
            OPENAI_API_KEY="sk-replace-me", TAVILY_API_KEY="tvly-replace-me", **self.NO_AZURE
        )
        assert s.integration_status["openai"] is False
        assert s.integration_status["tavily"] is False

    def test_absent_key_does_not_count(self, clean_env):
        s = Settings(_env_file=None)
        assert s.integration_status["openai"] is False

    def test_a_real_looking_key_counts(self):
        s = Settings(OPENAI_API_KEY="sk-proj-abc123", TAVILY_API_KEY="tvly-abc123")
        assert s.integration_status["openai"] is True
        assert s.integration_status["tavily"] is True

    def test_placeholder_check_ignores_case_and_whitespace(self):
        s = Settings(OPENAI_API_KEY="  SK-REPLACE-ME  ", **self.NO_AZURE)
        assert s.integration_status["openai"] is False

    def test_keyless_integrations_are_always_available(self):
        """Semantic Scholar's API is public; ChromaDB is embedded and local."""
        status = Settings(_env_file=None).integration_status
        assert status["semantic_scholar"] is True
        assert status["chromadb"] is True

    def test_missing_credentials_names_the_env_vars(self):
        s = Settings(
            OPENAI_API_KEY="sk-replace-me",
            TAVILY_API_KEY="tvly-real-key",
            DATABASE_URL="postgresql://u:realpassword@h:5432/d",
            **self.NO_AZURE,
        )
        assert s.missing_credentials == ["OPENAI_API_KEY"]

    def test_an_unset_database_url_is_listed_too(self):
        """DATABASE_URL is a credential like any other: a CHANGEME placeholder is
        not a configured database, for the same reason sk-replace-me is not a key."""
        s = Settings(OPENAI_API_KEY="sk-real", TAVILY_API_KEY="tvly-real", _env_file=None)
        assert s.missing_credentials == ["DATABASE_URL"]

    def test_nothing_missing_when_all_are_set(self):
        s = Settings(
            OPENAI_API_KEY="sk-real",
            TAVILY_API_KEY="tvly-real",
            DATABASE_URL="postgresql://u:realpassword@h:5432/d",
        )
        assert s.missing_credentials == []

    def test_the_two_views_never_disagree(self):
        """integration_status and missing_credentials must derive from one rule."""
        s = Settings(OPENAI_API_KEY="sk-replace-me", TAVILY_API_KEY="tvly-real", **self.NO_AZURE)
        for name in ("OPENAI_API_KEY", "TAVILY_API_KEY"):
            integration = name.split("_")[0].lower()
            assert s.integration_status[integration] is (name not in s.missing_credentials)


class TestLLMProvider:
    """Which foundation-model provider the credentials select.

    Azure keys are opaque strings with no `sk-` prefix — one pasted into
    OPENAI_API_KEY authenticates with nothing and is why these are separate,
    explicitly named settings.
    """

    AZURE = {
        "AZURE_OPENAI_ENDPOINT": "https://unit-test.openai.azure.invalid/",
        "AZURE_OPENAI_API_KEY": "azure-unit-test-key",
        "AZURE_OPENAI_DEPLOYMENT_NAME": "gpt-4o-deployed",
    }
    NO_OPENAI = {"OPENAI_API_KEY": None}

    def test_no_credentials_means_no_provider(self, clean_env):
        assert Settings(_env_file=None).llm_provider is None

    def test_a_plain_key_selects_openai(self, clean_env):
        s = Settings(OPENAI_API_KEY="sk-real", _env_file=None)
        assert s.llm_provider == "openai"
        assert s.llm_default_model == s.OPENAI_MODEL

    def test_complete_azure_settings_select_azure(self):
        s = Settings(**self.AZURE, **self.NO_OPENAI)
        assert s.llm_provider == "azure_openai"
        assert s.integration_status["openai"] is True
        assert "OPENAI_API_KEY" not in s.missing_credentials

    def test_on_azure_the_default_model_is_the_deployment(self):
        s = Settings(**self.AZURE, **self.NO_OPENAI)
        assert s.llm_default_model == "gpt-4o-deployed"

    def test_azure_wins_when_both_are_configured(self):
        s = Settings(**self.AZURE, OPENAI_API_KEY="sk-real")
        assert s.llm_provider == "azure_openai"

    @pytest.mark.parametrize(
        "incomplete",
        ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT_NAME"],
    )
    def test_a_partial_azure_setup_is_not_a_provider(self, incomplete):
        """Two of three values is 'not configured', never 'half works'."""
        s = Settings(**{**self.AZURE, incomplete: None}, **self.NO_OPENAI)
        assert s.llm_provider is None
        assert s.integration_status["openai"] is False

    def test_an_azure_placeholder_key_does_not_count(self):
        s = Settings(
            **{**self.AZURE, "AZURE_OPENAI_API_KEY": "azure-replace-me"}, **self.NO_OPENAI
        )
        assert s.llm_provider is None


class TestSettingsCaching:
    def test_accessor_returns_one_shared_instance(self):
        assert get_settings() is get_settings()

    def test_cache_can_be_cleared(self):
        first = get_settings()
        get_settings.cache_clear()
        assert get_settings() is not first


class TestSharedVocabulary:
    def test_three_research_modes_exist(self):
        assert len(list(ResearchMode)) == 3

    def test_modes_serialise_as_their_string_values(self):
        """StrEnum members must cross the API boundary as plain strings."""
        assert ResearchMode.MODEL_ONLY == "model_only"
        assert f"{ResearchMode.HYBRID}" == "hybrid"


@pytest.fixture
def no_report_quality_env(monkeypatch):
    for key in list(os.environ):
        if key.upper().startswith("REPORT_QUALITY_"):
            monkeypatch.delenv(key, raising=False)


class TestReportQualityKnobs:
    """Stage 27 (S0): declared for later stages, inert, and the legacy contract intact."""

    def test_pipeline_is_off_by_default(self, clean_env, no_report_quality_env):
        assert Settings(_env_file=None).REPORT_QUALITY_ENABLED is False

    def test_budgets_grow_with_depth(self, clean_env, no_report_quality_env):
        s = Settings(_env_file=None)
        for knob in (
            "MAX_LLM_ATTEMPTS",
            "MAX_TOTAL_TOKENS",
            "MAX_CLAIMS_VERIFIED",
            "RUN_DEADLINE_SECONDS",
        ):
            quick, medium, deep = (
                getattr(s, f"REPORT_QUALITY_{knob}_{depth}")
                for depth in ("QUICK", "MEDIUM", "DEEP")
            )
            assert 0 < quick <= medium <= deep, knob

    def test_claim_cap_never_exceeds_the_extraction_cap(
        self, clean_env, no_report_quality_env
    ):
        """Claims the extractor never stores cannot be verified; the cap must fit."""
        from app.services.claims.models import MAX_CLAIMS_PER_REPORT

        assert Settings(_env_file=None).REPORT_QUALITY_MAX_CLAIMS_VERIFIED_DEEP <= (
            MAX_CLAIMS_PER_REPORT
        )

    def test_thresholds_are_ordered(self, clean_env, no_report_quality_env):
        s = Settings(_env_file=None)
        assert (
            s.REPORT_QUALITY_DEGRADED_BODY_LOSS_RATIO
            <= s.REPORT_QUALITY_FAIL_BODY_LOSS_RATIO
        )

    def test_degraded_above_fail_is_rejected(
        self, monkeypatch, clean_env, no_report_quality_env
    ):
        monkeypatch.setenv("REPORT_QUALITY_DEGRADED_BODY_LOSS_RATIO", "0.5")
        monkeypatch.setenv("REPORT_QUALITY_FAIL_BODY_LOSS_RATIO", "0.25")
        with pytest.raises(Exception, match="must not exceed"):
            Settings(_env_file=None)

    def test_fraction_out_of_range_is_rejected(
        self, monkeypatch, clean_env, no_report_quality_env
    ):
        monkeypatch.setenv("REPORT_QUALITY_REVERIFY_RESERVE_FRACTION", "1.5")
        with pytest.raises(Exception, match="REPORT_QUALITY_REVERIFY_RESERVE_FRACTION"):
            Settings(_env_file=None)

    def test_non_positive_budget_is_rejected(
        self, monkeypatch, clean_env, no_report_quality_env
    ):
        monkeypatch.setenv("REPORT_QUALITY_MAX_TOTAL_TOKENS_QUICK", "0")
        with pytest.raises(Exception, match="REPORT_QUALITY_MAX_TOTAL_TOKENS_QUICK"):
            Settings(_env_file=None)

    def test_pipeline_versions(self):
        assert PipelineVersion.LEGACY == "legacy"
        assert PipelineVersion.REPORT_QUALITY_V1 == "report-quality-v1"

    def test_start_request_contract_is_unchanged(self):
        """No pipeline selector exists yet: /start accepts exactly what it did."""
        from app.schemas.research import ResearchStartRequest

        assert set(ResearchStartRequest.model_fields) == {"topic", "mode", "depth"}
