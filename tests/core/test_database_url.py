"""`database_url_str` is what the API, the worker and Alembic all connect with.

A managed provider hands out a libpq URL (`postgresql://...?sslmode=require&
channel_binding=require`). Used as written it fails twice over: the scheme selects
psycopg2, which is not installed, and the two query parameters are passed on to
`asyncpg.connect()`, which has neither keyword. Both failures hit `alembic upgrade`
and `create_async_engine` alike, so the fix belongs to the setting, not to a caller.
"""
from __future__ import annotations

import pytest
from sqlalchemy.engine.url import make_url

from app.core.config import Settings, validate_deployment_config

NEON = (
    "postgresql://u:pw@ep-x-pooler.c-2.ap-southeast-1.aws.neon.tech/neondb"
    "?sslmode=require&channel_binding=require"
)


def _url(database_url: str) -> str:
    return Settings(_env_file=None, DATABASE_URL=database_url).database_url_str  # type: ignore[arg-type]


@pytest.mark.parametrize("scheme", ["postgresql", "postgres"])
def test_a_libpq_scheme_becomes_asyncpg(scheme: str) -> None:
    """SQLAlchemy picks the driver from the URL, so a plain scheme means psycopg2."""
    assert _url(f"{scheme}://u:pw@db.example.com/recall").startswith("postgresql+asyncpg://")


def test_an_explicit_driver_is_left_alone() -> None:
    url = "postgresql+asyncpg://u:pw@db.example.com/recall"
    assert make_url(_url(url)).drivername == "postgresql+asyncpg"


def test_sslmode_is_renamed_to_asyncpgs_own_keyword() -> None:
    query = make_url(_url(NEON)).query
    assert query["ssl"] == "require"
    assert "sslmode" not in query


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full", "disable"])
def test_the_mode_itself_survives_the_rename(mode: str) -> None:
    """Translated, not overridden: asyncpg takes the same modes under another name."""
    assert make_url(_url(f"postgresql://u:pw@h/db?sslmode={mode}")).query["ssl"] == mode


def test_channel_binding_is_dropped() -> None:
    """A libpq-only option; asyncpg.connect() would raise TypeError on the keyword."""
    assert "channel_binding" not in make_url(_url(NEON)).query


def test_tls_is_never_invented_for_a_url_that_asked_for_none() -> None:
    assert "ssl" not in make_url(_url("postgresql://u:pw@localhost:5432/recall")).query


def test_host_database_and_password_are_preserved() -> None:
    url = make_url(_url(NEON))
    assert url.host == "ep-x-pooler.c-2.ap-southeast-1.aws.neon.tech"
    assert url.database == "neondb"
    assert url.password == "pw"  # the string is what opens the connection


def test_other_query_parameters_are_kept() -> None:
    url = _url("postgresql://u:pw@h/db?sslmode=require&prepared_statement_cache_size=0")
    assert make_url(url).query["prepared_statement_cache_size"] == "0"


# --- The boot guard ---------------------------------------------------------------

_SAFE = {"SECRET_KEY": "a" * 64, "COOKIE_SECURE": True, "CORS_ORIGINS": ["https://a.example"]}


def _check(env: str, database_url: str) -> None:
    validate_deployment_config(
        Settings(_env_file=None, ENV=env, DATABASE_URL=database_url, **_SAFE)  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("env", ["staging", "prod"])
@pytest.mark.parametrize("mode", ["", "?sslmode=prefer", "?sslmode=disable"])
def test_deployed_envs_refuse_a_database_url_without_tls(env: str, mode: str) -> None:
    """asyncpg's default falls back to plaintext without saying so."""
    with pytest.raises(RuntimeError, match="DATABASE_URL must request TLS"):
        _check(env, f"postgresql://u:pw@db.example.com/recall{mode}")


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_deployed_envs_accept_the_providers_own_url(env: str) -> None:
    _check(env, NEON)


def test_dev_is_exempt() -> None:
    _check("dev", "postgresql://u:pw@localhost:5432/recall")


def test_the_tls_guard_never_prints_the_password() -> None:
    with pytest.raises(RuntimeError) as exc:
        _check("prod", "postgresql://u:hunter2-not-in-the-log@db.example.com/recall")
    assert "hunter2" not in str(exc.value)
