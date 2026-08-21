"""Application configuration loaded from environment variables.

Single source of truth for settings. Never read os.environ elsewhere.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    SECRET_KEY: str = "change-me-in-production-use-openssl-rand-hex-32"
    SESSION_COOKIE_NAME: str = "recall_session"
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

    # --- Google OAuth ---
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/api/v1/auth/google/callback"

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
    def database_url_str(self) -> str:
        return str(self.DATABASE_URL)

    @property
    def redis_url_str(self) -> str:
        return str(self.REDIS_URL)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
