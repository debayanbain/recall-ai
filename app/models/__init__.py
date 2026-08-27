"""SQLModel table models. Import all here so Alembic autogenerate sees them."""
from app.models.audit_log import AuditLog
from app.models.collection import Collection, CollectionItem
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.instagram_account import InstagramAccount
from app.models.oauth_account import OAuthAccount
from app.models.subscription import Subscription
from app.models.telegram import TelegramAccount, TelegramLinkToken
from app.models.user import User
from app.models.user_session import UserSession
from app.models.vault import VaultChunk, VaultItem

__all__ = [
    "User",
    "UserSession",
    "OAuthAccount",
    "InstagramAccount",
    "TelegramAccount",
    "TelegramLinkToken",
    "ExtractionRun",
    "RunStatus",
    "VaultItem",
    "VaultChunk",
    "Collection",
    "CollectionItem",
    "Subscription",
    "AuditLog",
]
