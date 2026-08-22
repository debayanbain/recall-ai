"""Boot-time safety guards: signing key strength and docs exposure."""
from __future__ import annotations

import pytest

from app.core.config import (
    DEFAULT_SECRET_KEY,
    Settings,
    validate_deployment_config,
)

STRONG_KEY = "a" * 64  # stand-in for `openssl rand -hex 32`
SAFE_ORIGINS = ["https://app.recallai.example"]


def _route_paths(app: object) -> set[str]:
    """Collect route paths; not every entry in app.routes exposes .path."""
    return {p for r in app.routes if (p := getattr(r, "path", None))}  # type: ignore[attr-defined]


def _settings(**overrides: object) -> Settings:
    """Build Settings from explicit values only, ignoring any ambient .env."""
    base: dict[str, object] = {
        "SECRET_KEY": STRONG_KEY,
        "COOKIE_SECURE": True,
        "CORS_ORIGINS": SAFE_ORIGINS,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def _check(**overrides: object) -> Settings:
    """Build settings and run the boot-time deployment guard over them."""
    config = _settings(**overrides)
    validate_deployment_config(config)
    return config


def test_dev_allows_placeholder_secret() -> None:
    assert _check(ENV="dev", SECRET_KEY=DEFAULT_SECRET_KEY).SECRET_KEY == DEFAULT_SECRET_KEY


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_deployed_envs_reject_placeholder_secret(env: str) -> None:
    with pytest.raises(RuntimeError, match="placeholder"):
        _check(ENV=env, SECRET_KEY=DEFAULT_SECRET_KEY)


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_deployed_envs_reject_short_secret(env: str) -> None:
    with pytest.raises(RuntimeError, match="at least 32 characters"):
        _check(ENV=env, SECRET_KEY="tooshort")


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_deployed_envs_accept_strong_secret(env: str) -> None:
    assert _check(ENV=env).ENV == env


@pytest.mark.parametrize(
    ("env", "expected"), [("dev", True), ("staging", True), ("prod", False)]
)
def test_docs_enabled_only_outside_prod(env: str, expected: bool) -> None:
    assert _settings(ENV=env).docs_enabled is expected


def test_prod_app_serves_no_docs_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import config as config_module
    from app.main import create_app

    monkeypatch.setattr(config_module.settings, "ENV", "prod")
    assert {"/docs", "/redoc", "/openapi.json"}.isdisjoint(_route_paths(create_app()))


def test_dev_app_serves_docs_routes() -> None:
    from app.main import create_app

    assert {"/docs", "/redoc", "/openapi.json"} <= _route_paths(create_app())


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_guard_error_never_contains_the_secret(env: str) -> None:
    """A failed boot must not leak the key it rejected into logs."""
    secret = "s3cr3t-but-far-too-short"
    with pytest.raises(RuntimeError) as exc:
        _check(ENV=env, SECRET_KEY=secret)
    assert secret not in str(exc.value)


# --- COOKIE_SECURE ---


def test_dev_allows_insecure_cookie() -> None:
    assert _check(ENV="dev", COOKIE_SECURE=False).COOKIE_SECURE is False


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_deployed_envs_reject_insecure_cookie(env: str) -> None:
    with pytest.raises(RuntimeError, match="COOKIE_SECURE must be true"):
        _check(ENV=env, COOKIE_SECURE=False)


# --- CORS_ORIGINS ---


def test_dev_allows_wildcard_cors() -> None:
    assert _check(ENV="dev", CORS_ORIGINS=["*"]).CORS_ORIGINS == ["*"]


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_deployed_envs_reject_wildcard_cors(env: str) -> None:
    """allow_credentials=True + "*" makes Starlette echo any caller's Origin back."""
    with pytest.raises(RuntimeError, match="may not contain"):
        _check(ENV=env, CORS_ORIGINS=["*"])


@pytest.mark.parametrize(
    "origin",
    [
        "http://app.recallai.example",  # plaintext
        "https://localhost:3000",       # local
        "https://127.0.0.1:3000",
    ],
)
def test_prod_rejects_unsafe_cors_origins(origin: str) -> None:
    with pytest.raises(RuntimeError, match="https and non-local"):
        _check(ENV="prod", CORS_ORIGINS=[origin])


def test_staging_tolerates_plaintext_cors_origin() -> None:
    """The https/non-local rule is prod-only; staging often runs without TLS."""
    assert _check(ENV="staging", CORS_ORIGINS=["http://staging.internal"]).ENV == "staging"


def test_prod_accepts_real_https_origins() -> None:
    origins = ["https://recallai.app", "https://www.recallai.app"]
    assert _check(ENV="prod", CORS_ORIGINS=origins).CORS_ORIGINS == origins
