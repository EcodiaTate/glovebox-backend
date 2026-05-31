"""Shared fixtures.

`TestClient` + `fake_supabase` + `authed_user` are the three primitives every
route-level test in this suite needs. Keeping them here means new test files
don't re-author the auth-bypass dance.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Iterable

import pytest

# Tests live in `backend/tests/`; put `backend/` on sys.path so `app` imports.
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Minimum env the settings module needs to import without a real Supabase /
# Stripe environment present. CI sets the same values; this is the offline
# default so a developer can `pytest tests/ -q` cold.
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon-key")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("STRIPE_PRICE_ID", "price_test_dummy")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test_dummy")
os.environ.setdefault("OSRM_BASE_URL", "http://osrm.invalid")
os.environ.setdefault("MAPBOX_TOKEN", "test")
os.environ.setdefault("SUPA_URL", "https://example.supabase.co")
os.environ.setdefault("SUPA_SERVICE_ROLE_KEY", "test-service-role-key")


# ── In-memory Supabase double ──────────────────────────────────────────────


class _Result:
    """Mimics the supabase-py execute() return object's `data` attribute."""

    def __init__(self, data: list[dict[str, Any]] | None) -> None:
        self.data = data
        self.error = None


class _Query:
    """Records the chained call sequence and resolves it against in-memory rows.

    The supabase-py client exposes a fluent builder:
        supa.table(T).select(...).eq("k", v).order(...).limit(n).execute()
    This double captures filters in order and applies them on `execute()`.
    Only the verbs the production code paths under test actually call are
    implemented.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        # Hold the actual list (NOT a copy) so upserts mutate the table the
        # FakeSupabase exposes via `.tables[name]`. Reads still snapshot
        # locally inside execute() to avoid filter-mutates-source surprises.
        self._rows = rows
        self._filters: list[tuple[str, Any]] = []
        self._order: list[tuple[str, bool, bool]] = []  # (col, desc, nulls_first)
        self._limit: int | None = None
        self._select_cols: list[str] | None = None
        self._upsert_pending: list[dict[str, Any]] | None = None
        self._on_conflict: str | None = None

    def select(self, cols: str = "*", *_a: Any, **_kw: Any) -> "_Query":
        self._select_cols = (
            None if cols.strip() == "*" else [c.strip() for c in cols.split(",")]
        )
        return self

    def eq(self, col: str, val: Any) -> "_Query":
        self._filters.append((col, val))
        return self

    def order(
        self, col: str, *, desc: bool = False, nullsfirst: bool = False
    ) -> "_Query":
        self._order.append((col, desc, nullsfirst))
        return self

    def limit(self, n: int) -> "_Query":
        self._limit = n
        return self

    def upsert(
        self,
        row: dict[str, Any] | list[dict[str, Any]],
        on_conflict: str | None = None,
        **_kw: Any,
    ) -> "_Query":
        self._upsert_pending = row if isinstance(row, list) else [row]
        self._on_conflict = on_conflict
        return self

    def maybeSingle(self) -> "_Query":  # noqa: N802 - supabase-py uses camelCase
        return self

    def execute(self) -> _Result:
        if self._upsert_pending is not None:
            return self._do_upsert()
        rows = self._apply_filters_and_order(self._rows)
        rows = self._project(rows)
        if self._limit is not None:
            rows = rows[: self._limit]
        return _Result(rows)

    def _apply_filters_and_order(
        self, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        out = [r for r in rows if all(r.get(c) == v for c, v in self._filters)]
        # Apply orderings in reverse so the FIRST order() call is the most
        # significant key (the same semantics supabase-py exposes). For each
        # key, split nulls from non-nulls, sort the non-nulls in the requested
        # direction, then concatenate so nulls land where the caller wants
        # them regardless of value-sort direction (matches Postgres ORDER BY
        # ... NULLS FIRST / NULLS LAST).
        for col, desc, nulls_first in reversed(self._order):
            nulls = [r for r in out if r.get(col) is None]
            non_nulls = [r for r in out if r.get(col) is not None]
            non_nulls.sort(key=lambda r, _c=col: r.get(_c), reverse=desc)
            out = (nulls + non_nulls) if nulls_first else (non_nulls + nulls)
        return out

    def _project(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self._select_cols is None:
            return rows
        return [{c: r.get(c) for c in self._select_cols} for r in rows]

    def _do_upsert(self) -> _Result:
        assert self._upsert_pending is not None
        landed: list[dict[str, Any]] = []
        keys = (self._on_conflict or "").split(",") if self._on_conflict else []
        keys = [k.strip() for k in keys if k.strip()]
        for new_row in self._upsert_pending:
            replaced = False
            for i, existing in enumerate(self._rows):
                if keys and all(existing.get(k) == new_row.get(k) for k in keys):
                    merged = {**existing, **new_row}
                    self._rows[i] = merged
                    landed.append(merged)
                    replaced = True
                    break
            if not replaced:
                self._rows.append(dict(new_row))
                landed.append(dict(new_row))
        return _Result(landed)


class FakeSupabase:
    """Substitute for `get_supabase_admin()` in tests.

    Holds a per-table list of dict rows. Tests seed rows directly, then call
    the route or service; assertions read back from `.tables` after the call
    to verify writes.
    """

    def __init__(self, tables: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {
            "entitlements": [],
            "user_entitlements": [],
        }
        if tables:
            for k, v in tables.items():
                self.tables[k] = list(v)

    def table(self, name: str) -> _Query:
        return _Query(self.tables.setdefault(name, []))


# ── Pytest fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def fake_supabase(monkeypatch: pytest.MonkeyPatch) -> FakeSupabase:
    """Replace `get_supabase_admin()` everywhere it's referenced.

    The production singleton is cached by `lru_cache`; we patch the symbol on
    every module that imports it directly so caching can't leak the real
    client through.
    """

    fake = FakeSupabase()

    import app.core.supabase_admin as supa_mod
    import app.services.entitlements as ent_mod
    import app.api.stripe as stripe_mod

    monkeypatch.setattr(supa_mod, "get_supabase_admin", lambda: fake)
    monkeypatch.setattr(ent_mod, "get_supabase_admin", lambda: fake)
    monkeypatch.setattr(stripe_mod, "get_supabase_admin", lambda: fake)

    return fake


@pytest.fixture
def app_with_auth_bypass(monkeypatch: pytest.MonkeyPatch):
    """Spin up the FastAPI app with `get_current_user` overridden.

    The override returns a fixed AuthUser; tests don't need to mint real JWTs.
    Yields (app, user_id) so individual tests can also wire `get_optional_user`
    when they need a no-auth variant.
    """

    from app.core.auth import AuthUser, get_current_user, get_optional_user
    from app.main import app

    USER_ID = "00000000-0000-0000-0000-000000000001"

    def _user() -> AuthUser:
        return AuthUser(id=USER_ID, email="test@example.com")

    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_optional_user] = _user
    try:
        yield app, USER_ID
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_optional_user, None)


@pytest.fixture
def client(app_with_auth_bypass: Iterable) -> Iterable:
    """TestClient bound to the auth-bypassed app."""

    from fastapi.testclient import TestClient

    app, _ = app_with_auth_bypass
    return TestClient(app)


@pytest.fixture
def authed_user_id(app_with_auth_bypass: Iterable) -> str:
    """The user id the auth-bypass fixture returns. Tests seed rows under this id."""

    _, user_id = app_with_auth_bypass
    return user_id


@pytest.fixture
def unauthed_client() -> Iterable:
    """TestClient without the auth bypass - 401 paths can be exercised."""

    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)
