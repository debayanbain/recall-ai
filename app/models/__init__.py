"""SQLModel table models. Import all here so Alembic autogenerate sees them."""
from app.models.audit_log import AuditLog
from app.models.collection import Collection, CollectionItem
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.instagram_account import InstagramAccount
from app.models.oauth_account import OAuthAccount
from app.models.subscription import Subscription
from app.models.user import User
from app.models.vault import VaultChunk, VaultItem

__all__ = [
    "User",
    "OAuthAccount",
    "InstagramAccount",
    "ExtractionRun",
    "RunStatus",
    "VaultItem",
    "VaultChunk",
    "Collection",
    "CollectionItem",
    "Subscription",
    "AuditLog",
]
