"""Application configuration loaded from environment variables.

Single source of truth for settings. Never read os.environ elsewhere.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict

# Well-known placeholder. Refused outside dev so a deploy that forgets to set SECRET_KEY
# cannot silently sign session JWTs with a value anyone reading this file already knows.
DEFAULT_SECRET_KEY = "change-me-in-production-use-openssl-rand-hex-32"
MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---
    ENV: Literal["dev", "staging", "prod"] = "dev"
    DEBUG: bool = True
    APP_NAME: str = "RecallAI"
    API_V1_PREFIX: str = "/api/v1"
    FRONTEND_URL: str = "http://localhost:3000"
    BACKEND_URL: str = "http://localhost:8000"
    CORS_ORIGINS: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # --- Security ---
    SECRET_KEY: str = DEFAULT_SECRET_KEY
    SESSION_COOKIE_NAME: str = "recall_session"
    # "lax" works when the SPA and the API share a registrable domain (localhost:3000 ->
    # localhost:8000, app.example.com -> api.example.com). A genuinely cross-site split
    # needs "none", which browsers only honour together with Secure.
    SESSION_COOKIE_SAMESITE: Literal["lax", "strict", "none"] = "lax"
    SESSION_COOKIE_DOMAIN: str | None = None
    JWT_ALG: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days
    COOKIE_SECURE: bool = False  # True behind HTTPS in prod

    # --- Database ---
    DATABASE_URL: PostgresDsn = (
        "postgresql+asyncpg://recall:recall@localhost:5432/recall"  # type: ignore[assignment]
    )
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_ECHO: bool = False

    # --- Redis / Queue ---
    REDIS_URL: RedisDsn = "redis://localhost:6379/0"  # type: ignore[assignment]

    # --- OAuth (identity providers) ---
    # A provider is "enabled" only when both id and secret are non-empty; /auth/providers
    # advertises exactly that set, so a half-configured provider never reaches a redirect.
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/api/v1/auth/google/callback"

    # Facebook Login. Also the door to Instagram: the same app/token gains
    # instagram_basic + pages_show_list once the Instagram product is added, so the
    # provider tokens stored here are what a later Instagram extractor will use.
    FACEBOOK_CLIENT_ID: str = ""
    FACEBOOK_CLIENT_SECRET: str = ""
    FACEBOOK_REDIRECT_URI: str = "http://localhost:8000/api/v1/auth/facebook/callback"
    FACEBOOK_API_VERSION: str = "v21.0"
    # Sign-in asks for the bare minimum. Instagram permissions are deliberately NOT here:
    # they need Meta App Review, and putting them on the sign-in consent screen would ask
    # every new user for access to their business Pages just to log in.
    FACEBOOK_SCOPES: str = "public_profile,email"

    # --- Instagram: CONNECT (via the same Meta app, Facebook Login) ---
    # Requested only when an already-signed-in user links Instagram to read their media.
    # instagram_basic     -> read the IG Business account + its media
    # pages_show_list     -> find which Facebook Page the IG account hangs off
    # business_management -> resolve Pages owned through a Business portfolio
    INSTAGRAM_CONNECT_SCOPES: str = "instagram_basic,pages_show_list,business_management"
    INSTAGRAM_CONNECT_REDIRECT_URI: str = (
        "http://localhost:8000/api/v1/integrations/instagram/callback"
    )

    # --- Instagram: SIGN-IN (Instagram Login, a separate app identity) ---
    # This is NOT the Facebook app. Meta app -> Instagram product -> "Instagram API setup
    # with Instagram login" issues its own Instagram App ID / Secret. Instagram Basic
    # Display was shut down 2024-12-04; this replaces it.
    #
    # Instagram rejects plaintext redirect URIs -- the callback MUST be https, so local
    # development needs a tunnel (ngrok/cloudflared) rather than http://localhost.
    INSTAGRAM_APP_ID: str = ""
    INSTAGRAM_APP_SECRET: str = ""
    INSTAGRAM_LOGIN_REDIRECT_URI: str = ""
    INSTAGRAM_LOGIN_SCOPES: str = "instagram_business_basic"

    # Fernet key (urlsafe base64, 32 bytes) encrypting provider access/refresh tokens at
    # rest. Empty in dev -> tokens are simply not stored. Required outside dev.
    TOKEN_ENCRYPTION_KEY: str = ""

    # --- AI (Gemini) ---
    AI_PROVIDER: Literal["gemini", "openai", "claude"] = "gemini"
    GEMINI_API_KEY: str = ""
    GEMINI_TEXT_MODEL: str = "gemini-2.0-flash"
    GEMINI_EMBED_MODEL: str = "text-embedding-004"
    EMBEDDING_DIM: int = 1536

    # --- Storage (Cloudflare R2, S3-compatible) ---
    R2_ACCOUNT_ID: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET: str = "recall-ai"
    R2_PUBLIC_URL: str = ""

    # --- Rate limiting ---
    RATE_LIMIT_PER_MINUTE: int = 60

    @property
    def enabled_oauth_providers(self) -> list[str]:
        """Providers with both a client id and secret configured, in display order."""
        pairs = [
            ("google", self.GOOGLE_CLIENT_ID, self.GOOGLE_CLIENT_SECRET),
            ("facebook", self.FACEBOOK_CLIENT_ID, self.FACEBOOK_CLIENT_SECRET),
            ("instagram", self.INSTAGRAM_APP_ID, self.INSTAGRAM_APP_SECRET),
        ]
        return [name for name, client_id, secret in pairs if client_id and secret]

    @property
    def docs_enabled(self) -> bool:
        """Expose /docs, /redoc and /openapi.json everywhere except production."""
        return self.ENV != "prod"

    @property
    def database_url_str(self) -> str:
        return str(self.DATABASE_URL)

    @property
    def redis_url_str(self) -> str:
        return str(self.REDIS_URL)


def validate_deployment_config(config: Settings) -> None:
    """Fail closed on boot if a non-dev environment has an unsafe signing key.

    SECRET_KEY signs the session cookie, so a known or short key lets anyone forge a
    session for any user. Dev is exempt to keep local setup frictionless.

    Deliberately NOT a pydantic validator: pydantic embeds the whole settings input in
    ValidationError, which would spill live secrets from the environment into crash logs.
    Nothing below interpolates the key itself -- only its length.
    """
    if config.ENV == "dev":
        return
    if config.SECRET_KEY == DEFAULT_SECRET_KEY:
        raise RuntimeError(
            f"SECRET_KEY is still the built-in placeholder but ENV={config.ENV}. "
            "Generate one with: openssl rand -hex 32"
        )
    if len(config.SECRET_KEY) < MIN_SECRET_KEY_LENGTH:
        raise RuntimeError(
            f"SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters when "
            f"ENV={config.ENV} (got {len(config.SECRET_KEY)}). Use: openssl rand -hex 32"
        )

    # Without Secure the session cookie rides plaintext HTTP and can be lifted in transit.
    if not config.COOKIE_SECURE:
        raise RuntimeError(
            f"COOKIE_SECURE must be true when ENV={config.ENV}; the session cookie would "
            "otherwise be sent over plaintext HTTP."
        )

    # CORSMiddleware runs with allow_credentials=True. Starlette answers a wildcard origin by
    # echoing the caller's own Origin plus Access-Control-Allow-Credentials, so "*" here lets
    # any site read authenticated responses.
    if "*" in config.CORS_ORIGINS:
        raise RuntimeError(
            f'CORS_ORIGINS may not contain "*" when ENV={config.ENV}; credentials are enabled, '
            "so list the exact frontend origins instead."
        )

    # Browsers silently drop a SameSite=None cookie that is not Secure, which reads in
    # the app as "login does nothing" -- fail loudly at boot instead.
    if config.SESSION_COOKIE_SAMESITE == "none" and not config.COOKIE_SECURE:
        raise RuntimeError(
            "SESSION_COOKIE_SAMESITE=none requires COOKIE_SECURE=true; browsers reject "
            "a cross-site cookie without the Secure attribute."
        )

    # Provider refresh tokens are long-lived credentials for a third-party account. If any
    # OAuth provider is configured, refuse to run without a key to encrypt them at rest.
    any_oauth = any(
        (config.GOOGLE_CLIENT_ID, config.FACEBOOK_CLIENT_ID, config.INSTAGRAM_APP_ID)
    )
    if any_oauth and not config.TOKEN_ENCRYPTION_KEY:
        raise RuntimeError(
            f"TOKEN_ENCRYPTION_KEY is required when ENV={config.ENV} and any OAuth provider "
            "is configured. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )

    if config.ENV == "prod":
        unsafe = [
            origin
            for origin in config.CORS_ORIGINS
            if not origin.startswith("https://")
            or origin.startswith(("https://localhost", "https://127.0.0.1"))
        ]
        if unsafe:
            raise RuntimeError(
                f"CORS_ORIGINS must be https and non-local in prod; rejected: {unsafe}"
            )


@lru_cache
def get_settings() -> Settings:
    config = Settings()
    validate_deployment_config(config)
    return config


settings = get_settings()
