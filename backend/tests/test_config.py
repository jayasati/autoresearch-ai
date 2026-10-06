"""Configuration: defaults, environment loading, and derived values."""

import pytest

from app.core.config import Settings, get_settings
from app.core.constants import ResearchMode


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

    def test_placeholder_does_not_count_as_configured(self):
        s = Settings(OPENAI_API_KEY="sk-replace-me", TAVILY_API_KEY="tvly-replace-me")
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
        assert Settings(OPENAI_API_KEY="  SK-REPLACE-ME  ").integration_status["openai"] is False

    def test_keyless_integrations_are_always_available(self):
        """Semantic Scholar's API is public; ChromaDB is embedded and local."""
        status = Settings(_env_file=None).integration_status
        assert status["semantic_scholar"] is True
        assert status["chromadb"] is True

    def test_missing_credentials_names_the_env_vars(self):
        s = Settings(OPENAI_API_KEY="sk-replace-me", TAVILY_API_KEY="tvly-real-key")
        assert s.missing_credentials == ["OPENAI_API_KEY"]

    def test_nothing_missing_when_all_are_set(self):
        s = Settings(OPENAI_API_KEY="sk-real", TAVILY_API_KEY="tvly-real")
        assert s.missing_credentials == []

    def test_the_two_views_never_disagree(self):
        """integration_status and missing_credentials must derive from one rule."""
        s = Settings(OPENAI_API_KEY="sk-replace-me", TAVILY_API_KEY="tvly-real")
        for name in ("OPENAI_API_KEY", "TAVILY_API_KEY"):
            integration = name.split("_")[0].lower()
            assert s.integration_status[integration] is (name not in s.missing_credentials)


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
