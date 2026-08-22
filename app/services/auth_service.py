"""OAuth login flow and user provisioning, provider-agnostic.

The provider-specific half lives in `app.services.oauth`; this service only ever sees a
normalised `OAuthIdentity`. Two rules drive everything here:

1. **Linking by email requires a verified address.** Otherwise anyone who can set an
   arbitrary email on a throwaway account at provider B could claim the account a victim
   created through provider A. An unverified (or absent) address always creates or keeps a
   separate user.
2. **Provider tokens are never stored in the clear.** They go through `app.core.crypto`,
   which drops them entirely when no encryption key is configured.
"""
from __future__ import annotations

from datetime import UTC, datetime

from app.core.crypto import encrypt_token
from app.models.oauth_account import OAuthAccount
from app.models.user import User
from app.repositories.oauth_account import OAuthAccountRepository
from app.repositories.user import UserRepository
from app.services.oauth import OAuthIdentity

# Non-routable domain for providers that do not release an email -- today that is a
# phone-only Facebook account. Keeps `users.email` NOT NULL + UNIQUE without inventing
# a real address that might collide with someone's actual inbox.
PLACEHOLDER_EMAIL_DOMAIN = "users.noreply.recall.invalid"


def placeholder_email(identity: OAuthIdentity) -> str:
    return f"{identity.provider}-{identity.account_id}@{PLACEHOLDER_EMAIL_DOMAIN}"


def is_placeholder_email(email: str | None) -> bool:
    return bool(email) and email.endswith(f"@{PLACEHOLDER_EMAIL_DOMAIN}")  # type: ignore[union-attr]


class AuthService:
    def __init__(self, users: UserRepository, accounts: OAuthAccountRepository) -> None:
        self.users = users
        self.accounts = accounts

    async def login(self, identity: OAuthIdentity) -> User:
        """Resolve an identity to a user, creating or linking as needed."""
        user = await self._resolve_user(identity)
        await self._upsert_account(user, identity)
        return user

    async def _resolve_user(self, identity: OAuthIdentity) -> User:
        # 1. Already linked -- the only lookup that is proof of identity on its own.
        existing = await self.accounts.get_by_provider(identity.provider, identity.account_id)
        if existing is not None:
            user = await self.users.get(existing.user_id)
            if user is not None:
                return await self._refresh_profile(user, identity)

        # 2. Verified email matches an existing account -> link this provider to it.
        if identity.email and identity.email_verified:
            by_email = await self.users.get_by_email(identity.email)
            if by_email is not None:
                return await self._refresh_profile(by_email, identity)

        # 3. Brand new user. An unverified address is kept for display only if nobody else
        #    already owns it; otherwise fall back to the placeholder so it cannot squat.
        email = identity.email
        if email and not identity.email_verified and await self.users.get_by_email(email):
            email = None

        return await self.users.create(
            User(
                email=email or placeholder_email(identity),
                email_verified=bool(email) and identity.email_verified,
                name=identity.name,
                avatar_url=identity.avatar_url,
                auth_provider=identity.provider,
                provider_account_id=identity.account_id,
            )
        )

    async def _refresh_profile(self, user: User, identity: OAuthIdentity) -> User:
        """Update the display snapshot on an existing user. Never widens access."""
        user.auth_provider = identity.provider
        user.provider_account_id = identity.account_id
        user.name = user.name or identity.name
        user.avatar_url = identity.avatar_url or user.avatar_url
        # Upgrade a placeholder address only once a provider vouches for a real one.
        if identity.email and identity.email_verified and is_placeholder_email(user.email):
            if await self.users.get_by_email(identity.email) is None:
                user.email = identity.email
                user.email_verified = True
        elif identity.email and identity.email_verified and user.email == identity.email:
            user.email_verified = True
        return await self.users.create(user)

    async def _upsert_account(self, user: User, identity: OAuthIdentity) -> OAuthAccount:
        account = await self.accounts.get_by_provider(identity.provider, identity.account_id)
        if account is None:
            account = OAuthAccount(
                user_id=user.id,
                provider=identity.provider,
                provider_account_id=identity.account_id,
            )
        account.email = identity.email
        account.email_verified = identity.email_verified
        account.name = identity.name
        account.avatar_url = identity.avatar_url
        account.username = identity.username
        account.scopes = identity.scopes
        account.expires_at = identity.expires_at
        account.access_token_encrypted = encrypt_token(identity.access_token)
        # Providers omit the refresh token on re-consent; keep the one already stored
        # rather than blanking a still-valid credential.
        new_refresh = encrypt_token(identity.refresh_token)
        if new_refresh is not None:
            account.refresh_token_encrypted = new_refresh
        account.updated_at = datetime.now(UTC)
        return await self.accounts.add(account)

    async def linked_providers(self, user: User) -> list[str]:
        return [a.provider for a in await self.accounts.list_for_user(user.id)]
