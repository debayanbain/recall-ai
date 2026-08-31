"""Round-trip budget for the read paths.

Every one of these guards a property that fails *silently* rather than loudly. The API
talks to a managed database in another region, where one statement is one network round
trip (~290ms measured), so a second query added to a listing is not a code-review nit --
it is the difference between a page that appears and a page the user waits for. Nothing
here needs a database: the statements are compiled, not executed.
"""
from __future__ import annotations

import time
import uuid

import pytest
from sqlalchemy.dialects import postgresql
from sqlmodel import col, func, select

from app.api.deps import (
    _USER_CACHE_MAX,
    _cache_key,
    _cached_user,
    _remember_user,
    clear_user_cache,
    forget_cached_user,
)
from app.core.config import settings
from app.models.user import User
from app.models.vault import VaultItem
from app.repositories.vault import VaultRepository
from app.schemas.vault import VaultItemRead


def _compile(stmt: object) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]


def test_listing_carries_its_own_total() -> None:
    """The page and its `total` must arrive in ONE statement.

    The obvious shape is `SELECT count(*)` followed by `SELECT ... LIMIT`, which doubles
    the cost of every list request for a number that a window function computes inside
    the scan it is already doing.
    """
    base = select(VaultItem).where(VaultItem.user_id == uuid.uuid4())
    stmt = base.add_columns(func.count().over().label("total"))
    assert "count(*) OVER ()" in _compile(stmt)


def test_card_columns_cover_every_listed_field() -> None:
    """`_CARD_COLUMNS` must serve all of `VaultItemRead`.

    The list query loads only those columns, with `raiseload=True`. Adding a field to the
    list response without adding it here does not fail at import or at type-check: it
    fails at runtime, on a request, with `InvalidRequestError`. This is the check that
    turns that into a test failure.
    """
    needed = set(VaultItemRead.model_fields) - {"id"}  # the primary key always loads
    missing = needed - set(VaultRepository._CARD_COLUMNS)
    assert not missing, f"VaultItemRead fields not loaded by the list query: {missing}"


def test_card_columns_are_real_columns() -> None:
    for name in VaultRepository._CARD_COLUMNS:
        assert hasattr(VaultItem, name), f"{name} is not a VaultItem column"


def test_card_columns_exclude_the_heavy_bodies() -> None:
    """Listings must not drag article bodies, blocks and highlights across the wire."""
    for heavy in ("content", "item_metadata", "ai_highlights"):
        assert heavy not in VaultRepository._CARD_COLUMNS


def test_listing_filters_are_bound_parameters() -> None:
    """The filters are values, never interpolated SQL."""
    stmt = (
        select(VaultItem)
        .where(VaultItem.user_id == uuid.uuid4())
        .where(col(VaultItem.title).ilike("%'; drop table vault_items; --%"))
    )
    sql = _compile(stmt)
    assert "drop table" not in sql.lower()


# --- the current-user cache ------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    clear_user_cache()


def _user() -> User:
    return User(
        email=f"{uuid.uuid4().hex}@example.test",
        auth_provider="google",
        provider_account_id="1",
    )


def test_cache_is_keyed_by_token_not_by_user() -> None:
    """A different access token must never read another token's entry."""
    user = _user()
    _remember_user(_cache_key("token-a"), user)
    assert _cached_user(_cache_key("token-a")) is not None
    assert _cached_user(_cache_key("token-b")) is None


def test_cache_key_is_a_digest_not_the_token() -> None:
    """The map is reachable from a heap dump; a raw session token there is a credential."""
    assert "supersecret" not in _cache_key("supersecret")


def test_cached_user_round_trips_the_listed_fields() -> None:
    user = _user()
    _remember_user(_cache_key("t"), user)
    got = _cached_user(_cache_key("t"))
    assert got is not None
    assert got.id == user.id
    assert got.email == user.email
    assert got.plan == user.plan
    # Only live users are cached, so this is the value every entry must carry.
    assert got.deleted_at is None


def test_entry_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AUTH_USER_CACHE_SECONDS", 30)
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base)
    _remember_user(_cache_key("t"), _user())
    assert _cached_user(_cache_key("t")) is not None
    monkeypatch.setattr(time, "monotonic", lambda: base + 31)
    assert _cached_user(_cache_key("t")) is None


def test_zero_ttl_disables_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AUTH_USER_CACHE_SECONDS", 0)
    _remember_user(_cache_key("t"), _user())
    assert _cached_user(_cache_key("t")) is None


def test_deleting_an_account_drops_its_entries_immediately() -> None:
    """Deletion must not have to wait out the TTL."""
    user = _user()
    other = _user()
    _remember_user(_cache_key("a"), user)
    _remember_user(_cache_key("b"), user)
    _remember_user(_cache_key("c"), other)
    forget_cached_user(user.id)
    assert _cached_user(_cache_key("a")) is None
    assert _cached_user(_cache_key("b")) is None
    assert _cached_user(_cache_key("c")) is not None


def test_cache_is_bounded() -> None:
    """An entry is only written for a token that already verified, but the map still
    needs a ceiling: a miss costs one query, an unbounded dict costs the process."""
    for i in range(_USER_CACHE_MAX + 5):
        _remember_user(_cache_key(f"t{i}"), _user())
    from app.api.deps import _user_cache

    assert len(_user_cache) <= _USER_CACHE_MAX


def test_ttl_stays_well_inside_the_access_token_lifetime() -> None:
    """The cache must sit inside the revocation window that already exists, never widen
    it: nothing consults the database while an access token is valid, so deletion already
    takes up to ACCESS_TOKEN_EXPIRE_MINUTES and this must not add to that."""
    assert settings.AUTH_USER_CACHE_SECONDS <= settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60 // 4
