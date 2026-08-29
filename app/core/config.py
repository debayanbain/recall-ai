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
    #: Reconcile the Telegram webhook registration and probe for a live worker at boot.
    #: Both are outbound network calls, which is why they are switchable: a test suite
    #: must not talk to Telegram, and a deployment whose webhook is managed by its
    #: infrastructure should not have the app quietly re-point it.
    STARTUP_SELF_CHECK: bool = True
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
    # --- Session lifetimes ---
    # The access token is a stateless JWT: nothing checks it against the database, so a
    # revoked session keeps working until this expires. Minutes, not days, is what keeps
    # that window small enough to be acceptable.
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    # The refresh token is server-side and rotates on every use, so this is a *sliding*
    # window: a user who comes back within 7 days is silently re-authenticated and gets
    # another 7 days. Only a 7-day absence sends them back through the provider.
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    # Hard ceiling on a rotation chain regardless of activity. Without it a stolen
    # refresh token that is rotated forever is a permanent account key.
    REFRESH_TOKEN_ABSOLUTE_DAYS: int = 90
    # Sent only to the auth routes (Path=/api/v1/auth), never to /vault -- so an XSS on
    # any other endpoint's response has no path that would even echo it.
    REFRESH_COOKIE_NAME: str = "recall_refresh"
    #: Non-secret marker that this browser holds a refresh token. Readable by the Next
    #: proxy so its route gate can outlive the 15-minute access cookie -- see
    #: `_set_session_cookies`. It authorises nothing; the API still verifies the JWT.
    SESSION_HINT_COOKIE_NAME: str = "recall_signed_in"
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
    # Prefork: one OS process per concurrent task. Kept modest because tasks are I/O
    # bound and the slow part now happens on Apify's servers, not in ours.
    CELERY_CONCURRENCY: int = 4
    # Hard ceiling per task. Only the *trigger* and the *finalize* run here now — the
    # scrape itself is fire-and-forget — so this no longer has to cover a 5-minute crawl.
    CELERY_TASK_TIME_LIMIT: int = 300

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

    # --- Apify (Instagram scraping) ---
    # Instagram serves a login wall to server-side fetches, so a generic HTML fetch gets
    # nothing. Apify runs a real browser and returns the caption, hashtags, media URLs and
    # engagement counts, which is what the AI pipeline then summarizes and tags.
    APIFY_TOKEN: str = ""
    # Actor ids use `~` in the API path, not `/`.
    APIFY_INSTAGRAM_ACTOR: str = "apify~instagram-scraper"
    # Configurable because the first-party actor documents `startUrls` as *page*
    # URLs; a store actor that resolves a single reel can be swapped in here.
    APIFY_FACEBOOK_ACTOR: str = "apify~facebook-reels-scraper"
    # Off because Facebook still serves a reel's Open Graph tags to a non-browser
    # User-Agent, and those carry the whole caption for free. Turn it on only with an
    # actor that accepts a *single reel* URL (the first-party one walks a page), and
    # only when comments or engagement counts are worth paying per run for.
    FACEBOOK_USE_APIFY: bool = False
    # Kept under the worker's job_timeout (120s) so a slow scrape fails as a scrape rather
    # than as an opaque job timeout.
    APIFY_TIMEOUT_SECONDS: float = 90.0
    # Instagram blocks datacenter IPs, so real-world success needs Apify's RESIDENTIAL
    # proxy group — a paid feature. Off by default so a free account is not billed for
    # a proxy it cannot use.
    APIFY_USE_PROXY: bool = False
    # Apify POSTs here when a run finishes. Must be publicly reachable — in development
    # that is the tunnel, which is why it defaults to empty rather than to localhost:
    # registering an unreachable webhook silently strands every run.
    PUBLIC_BASE_URL: str = ""
    # Shared secret in the webhook path. The payload itself is never trusted for data —
    # it only names a run id, which the worker then re-fetches from Apify with our own
    # token — but the endpoint still must not be an open trigger for background work.
    APIFY_WEBHOOK_SECRET: str = ""
    # A run that has neither called back nor finished by now is swept by the beat task.
    EXTRACTION_RUN_TIMEOUT_MINUTES: int = 20

    # --- Telegram capture bot ---
    # From @BotFather. The token is the bot's whole identity: anyone holding it can read
    # every message sent to the bot and reply as it.
    TELEGRAM_BOT_TOKEN: str = ""
    # Without the @. Only used to build the t.me deep link the connect page hands out.
    TELEGRAM_BOT_USERNAME: str = ""
    # Verified in BOTH the webhook path segment and Telegram's own
    # X-Telegram-Bot-Api-Secret-Token header, so a leaked URL alone is not a trigger.
    TELEGRAM_WEBHOOK_SECRET: str = ""
    # The link token is a one-shot hand-off between two devices; it only has to outlive
    # the walk from the browser to the phone.
    TELEGRAM_LINK_TOKEN_TTL_MINUTES: int = 10
    # Telegram's own ceiling for getFile is 20 MB. Asking for more fails at their API,
    # not ours, so the limit is enforced here to give the user a real message.
    TELEGRAM_MAX_FILE_MB: int = 20
    # Conversation context lives in Redis, not Postgres: it is disposable, and a chat
    # that has gone quiet for an hour should start fresh rather than resume mid-thought.
    TELEGRAM_CHAT_HISTORY_TTL_SECONDS: int = 3600
    TELEGRAM_RECALL_TOP_K: int = 8
    # Per-Telegram-user hourly caps. A recall costs an embedding plus two model calls, so
    # it is capped harder than a capture.
    TELEGRAM_CAPTURES_PER_HOUR: int = 60
    TELEGRAM_RECALLS_PER_HOUR: int = 20

    # --- Recall answering guard rails ---
    # A retrieved memory carries a relevance score: 1 - cosine distance, so 1.0 is
    # identical and 0.0 is unrelated. Top-k is not truth -- a vector search always
    # returns its k nearest rows, however far away they are, and handing the answer
    # model a distant memory is how "you saved something about Sweden" gets attached to
    # a note about Norway. Anything below the floor is dropped before the prompt is
    # built, and a question left with nothing above it is answered by a fixed sentence
    # with no model call at all.
    #
    # **The right value depends on the embedding model and must be tuned against real
    # data.** Gemini's text-embedding-004 sits high and close together -- unrelated text
    # still scores around 0.5 -- while OpenAI's text-embedding-3-small spreads much
    # lower; roughly 0.25 / 0.40 is the equivalent pair there. The defaults below are
    # for the default provider. Setting the floor to 0 disables it, which is a decision
    # to make deliberately rather than by leaving a field blank.
    RECALL_MIN_SCORE: float = 0.55
    # Above this a memory is treated as answering the question; between the two the
    # answer is still generated, but told to say plainly that the match is weak.
    RECALL_STRONG_SCORE: float = 0.68
    # Provider-independent second filter: drop memories much weaker than the best hit,
    # even when they clear the floor. Keeps a single strong match from being diluted by
    # seven mediocre ones the model would otherwise try to connect.
    RECALL_SCORE_MARGIN: float = 0.15
    # A grounded answer about a handful of cards has no honest reason to be long, and an
    # answer that runs away from its evidence is the shape a fabrication takes. Clipped
    # rather than rejected -- the first sentences are the answer.
    RECALL_ANSWER_MAX_CHARS: int = 1500
    # The conversation lane answers greetings and questions about the bot itself, and
    # nothing it can honestly say needs more room than this. It is the last gate in front
    # of a model that has been told to stay in scope and asked politely: a jailbroken
    # reply gets clipped mid-essay rather than delivered whole. Deliberately smaller than
    # the recall cap -- an answer here has no evidence behind it to be long about.
    CHAT_REPLY_MAX_CHARS: int = 600

    # --- AI (Gemini) ---
    AI_PROVIDER: Literal["gemini", "openai", "claude"] = "gemini"

    # --- OpenAI ---
    # text-embedding-3-small emits 1536 dims natively, which matches EMBEDDING_DIM exactly
    # — unlike Gemini's 768, which has to be zero-padded to fit the Vector(1536) column.
    OPENAI_API_KEY: str = ""
    OPENAI_TEXT_MODEL: str = "gpt-4o-mini"
    OPENAI_EMBED_MODEL: str = "text-embedding-3-small"
    GEMINI_API_KEY: str = ""
    GEMINI_TEXT_MODEL: str = "gemini-2.0-flash"
    GEMINI_EMBED_MODEL: str = "text-embedding-004"
    EMBEDDING_DIM: int = 1536

    # --- Storage (Backblaze B2, S3-compatible) ---
    # The bucket is PRIVATE. There is no public-URL setting on purpose: downloads are
    # short-lived presigned GETs minted per request, after the row's owner is checked.
    # Endpoint and region come from the bucket page in the B2 console and must match --
    # the region is the `004` in `s3.us-west-004.backblazeb2.com`.
    B2_ENDPOINT_URL: str = "https://s3.us-west-004.backblazeb2.com"
    B2_REGION: str = "us-west-004"
    B2_BUCKET: str = ""
    # An *application key* scoped to this one bucket, never the master key: the master
    # key can create and delete buckets, and it cannot be scoped or rotated per service.
    B2_KEY_ID: str = ""
    B2_APPLICATION_KEY: str = ""
    # Hard ceiling on one upload. Enforced by reading one byte past it, not by trusting
    # Content-Length, which the client writes.
    MAX_UPLOAD_MB: int = 25
    # Download links are minted on demand, so they only have to outlive the click.
    DOWNLOAD_LINK_TTL_SECONDS: int = 300

    # --- Rate limiting ---
    RATE_LIMIT_PER_MINUTE: int = 60

    # --- Centralized log files (development only) ---
    # In dev every structlog event from the API, the worker and beat is appended as a
    # JSON line under LOG_DIR -- one file per source per day -- so three processes share
    # one greppable place and `request_id` correlates a request across all of them.
    # Nothing is written outside ENV=dev (see `file_logging_enabled`): a deployed
    # container's filesystem is ephemeral and unmonitored, so files there would be a PII
    # spill nobody reads. stdout remains the transport everywhere.
    LOG_DIR: str = "logs"
    LOG_FILES_ENABLED: bool = True
    LOG_FILE_LEVEL: Literal["debug", "info", "warning", "error", "critical"] = "info"
    # Retention: the sink deletes `<source>-<date>.jsonl` files older than this when it
    # rolls over to a new day, so it holds with no worker and no beat running.
    LOG_RETENTION_DAYS: int = 15

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
    def storage_enabled(self) -> bool:
        """True only when every B2 credential is present.

        All-or-nothing: a half-configured bucket would fail at upload time with a provider
        error instead of the API simply reporting that uploads are unavailable.
        """
        return bool(
            self.B2_BUCKET
            and self.B2_KEY_ID
            and self.B2_APPLICATION_KEY
            and self.B2_ENDPOINT_URL
        )

    @property
    def telegram_enabled(self) -> bool:
        """True only when the bot token, its username and the webhook secret are all set.

        All-or-nothing for the same reason as `storage_enabled`: a half-configured bot
        would hand the user a deep link to a bot that cannot answer, or register a
        webhook nothing can authenticate.
        """
        return bool(
            self.TELEGRAM_BOT_TOKEN
            and self.TELEGRAM_BOT_USERNAME
            and self.TELEGRAM_WEBHOOK_SECRET
        )

    @property
    def file_logging_enabled(self) -> bool:
        """Log files are a development affordance, gated on the environment itself.

        Hard-gated rather than merely defaulted off: `LOG_FILES_ENABLED=true` in a
        deployed environment must not start writing user request trails to a disk that
        nobody rotates, backs up or reads.
        """
        return self.ENV == "dev" and self.LOG_FILES_ENABLED

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
    # Checked in every environment: a zero or negative retention makes the purge task
    # delete rows it has just written, which looks like "logging is broken".
    if config.LOG_RETENTION_DAYS < 1:
        raise RuntimeError(
            f"LOG_RETENTION_DAYS must be >= 1 (got {config.LOG_RETENTION_DAYS})."
        )

    # A refresh window shorter than the access token makes the access cookie outlive the
    # credential that renews it: sessions would die at random points inside their window.
    if config.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 <= config.ACCESS_TOKEN_EXPIRE_MINUTES:
        raise RuntimeError(
            "REFRESH_TOKEN_EXPIRE_DAYS must exceed ACCESS_TOKEN_EXPIRE_MINUTES "
            f"(got {config.REFRESH_TOKEN_EXPIRE_DAYS}d vs "
            f"{config.ACCESS_TOKEN_EXPIRE_MINUTES}m)."
        )
    if config.REFRESH_TOKEN_ABSOLUTE_DAYS < config.REFRESH_TOKEN_EXPIRE_DAYS:
        raise RuntimeError(
            "REFRESH_TOKEN_ABSOLUTE_DAYS must be >= REFRESH_TOKEN_EXPIRE_DAYS "
            f"(got {config.REFRESH_TOKEN_ABSOLUTE_DAYS} vs "
            f"{config.REFRESH_TOKEN_EXPIRE_DAYS})."
        )

    # Partial storage config is refused rather than silently disabling uploads: a deploy
    # that sets two of the three values means to have a bucket, and finding out at the
    # first user upload is worse than finding out at boot.
    b2_values = (config.B2_BUCKET, config.B2_KEY_ID, config.B2_APPLICATION_KEY)
    if any(b2_values) and not all(b2_values):
        missing = [
            name
            for name, value in zip(
                ("B2_BUCKET", "B2_KEY_ID", "B2_APPLICATION_KEY"), b2_values, strict=True
            )
            if not value
        ]
        raise RuntimeError(f"Backblaze storage is partly configured; missing: {missing}")

    # Same reasoning for the bot: setting the token means intending to run it, and a
    # missing username or webhook secret only surfaces when a user taps a dead link.
    telegram_values = (
        config.TELEGRAM_BOT_TOKEN,
        config.TELEGRAM_BOT_USERNAME,
        config.TELEGRAM_WEBHOOK_SECRET,
    )
    if any(telegram_values) and not all(telegram_values):
        missing = [
            name
            for name, value in zip(
                (
                    "TELEGRAM_BOT_TOKEN",
                    "TELEGRAM_BOT_USERNAME",
                    "TELEGRAM_WEBHOOK_SECRET",
                ),
                telegram_values,
                strict=True,
            )
            if not value
        ]
        raise RuntimeError(f"Telegram is partly configured; missing: {missing}")

    if config.ENV == "dev":
        return
    # The webhook path segment is the only thing standing between the public internet
    # and a queue producer, so it gets the same length floor as SECRET_KEY. Never print
    # the value itself -- this function exists so secrets stay out of crash logs.
    if config.telegram_enabled and len(config.TELEGRAM_WEBHOOK_SECRET) < MIN_SECRET_KEY_LENGTH:
        raise RuntimeError(
            f"TELEGRAM_WEBHOOK_SECRET must be at least {MIN_SECRET_KEY_LENGTH} characters "
            f"when ENV={config.ENV} (got {len(config.TELEGRAM_WEBHOOK_SECRET)}). "
            "Use: openssl rand -hex 32"
        )
    # Telegram refuses to register a plaintext webhook, so an unset or http:// base URL
    # fails at setWebhook with an unhelpful error rather than here.
    if config.telegram_enabled and not config.PUBLIC_BASE_URL.startswith("https://"):
        raise RuntimeError(
            f"PUBLIC_BASE_URL must be an https:// URL when Telegram is enabled and "
            f"ENV={config.ENV}; Telegram will not deliver updates to a plaintext webhook."
        )
    # A long-lived access token is a revocation hole: nothing consults the database while
    # it is valid, so "sign out everywhere" would not take effect for its whole lifetime.
    if config.ACCESS_TOKEN_EXPIRE_MINUTES > 60:
        raise RuntimeError(
            f"ACCESS_TOKEN_EXPIRE_MINUTES must be <= 60 when ENV={config.ENV} (got "
            f"{config.ACCESS_TOKEN_EXPIRE_MINUTES}); revocation only takes effect when "
            "the access token expires."
        )
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
