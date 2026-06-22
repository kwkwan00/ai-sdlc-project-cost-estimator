"""Default rate-card surface: pricing.resolve_rate, effective-rate merging, validation, endpoints.

The DB-override merge path is covered in test_postgres_layer.py (needs the aiosqlite fixture);
here we cover pricing.resolve_rate, the Postgres-disabled defaults path + validation + routes."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import db.postgres_adapter as postgres_adapter
from models.twin_outputs import RoleCategory as RC
from models.twin_outputs import RoleSeniority as RS
from pricing import DEFAULT_RATES, resolve_rate
from rate_card_admin import (
    CustomRoleInputRow,
    RateCardUpdate,
    RateInput,
    _validate_custom_roles,
    get_effective_rates,
    get_role_catalog,
    update_rates,
)


def test_resolve_rate_override_then_default() -> None:
    # Code default when no override is supplied.
    assert resolve_rate(RC.ENGINEERING, RS.SENIOR) == DEFAULT_RATES[(RC.ENGINEERING, RS.SENIOR)]
    # A DB override for a cell wins.
    assert resolve_rate(RC.ENGINEERING, RS.SENIOR, {(RC.ENGINEERING, RS.SENIOR): 999.0}) == 999.0
    # A cell absent from the overrides falls back to the code default.
    assert (
        resolve_rate(RC.QA, RS.JUNIOR, {(RC.ENGINEERING, RS.SENIOR): 999.0})
        == DEFAULT_RATES[(RC.QA, RS.JUNIOR)]
    )


def test_default_rate_card_completeness_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import pricing

    # Anchor cells match RoleRoster.default() so a vanilla proposal round-trips with same rates.
    assert resolve_rate(RC.ENGINEERING, RS.SENIOR) == 240.0
    assert resolve_rate(RC.PRODUCT, RS.JUNIOR) == 140.0
    # Every category × seniority cell is covered (no zero/missing rate).
    assert len(DEFAULT_RATES) == 28
    assert all(resolve_rate(cat, sen) > 0 for cat in RC for sen in RS)
    # The fallback is defensive for future enum additions: empty the table → RATE_FALLBACK.
    monkeypatch.setattr(pricing, "DEFAULT_RATES", {})
    assert resolve_rate(RC.QA, RS.MID) == pricing.RATE_FALLBACK


@pytest.mark.asyncio
async def test_effective_rates_default_shape() -> None:
    postgres_adapter._reset_for_tests()  # Postgres disabled → no overrides
    resp = await get_effective_rates()
    assert len(resp.rates) == 28  # 7 categories × 4 seniorities
    eng = next(r for r in resp.rates if r.category == "engineering" and r.seniority == "senior")
    assert (eng.rate, eng.default_rate, eng.is_override) == (240.0, 240.0, False)
    assert (resp.min_rate, resp.max_rate) == (0.0, 1000.0)


@pytest.mark.asyncio
async def test_update_rejects_out_of_bounds_rate() -> None:
    with pytest.raises(HTTPException) as exc:
        await update_rates(
            RateCardUpdate(rates=[RateInput(category="engineering", seniority="senior", rate=5000.0)])
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_update_rejects_unknown_role_cell() -> None:
    with pytest.raises(HTTPException):
        await update_rates(
            RateCardUpdate(rates=[RateInput(category="wizard", seniority="senior", rate=200.0)])
        )


# --- custom roles --------------------------------------------------------------------------


def _crow(**kw: object) -> CustomRoleInputRow:
    base = {"label": "Principal Architect", "category": "engineering", "seniority": "senior",
            "rate": 300.0}
    return CustomRoleInputRow(**{**base, **kw})  # type: ignore[arg-type]


def test_validate_custom_roles_slugifies_and_dedups_ids() -> None:
    recs = _validate_custom_roles(
        [_crow(), _crow(label="Principal Architect!", rate=305.0)], lo=0.0, hi=1000.0
    )
    # Same-ish labels → unique slug ids (the 2nd gets a numeric suffix); label kept verbatim.
    assert [r.role_id for r in recs] == ["principal_architect", "principal_architect_2"]
    assert recs[0].label == "Principal Architect"
    # A supplied slug id round-trips idempotently (so edits target the same row).
    assert _validate_custom_roles([_crow(role_id="scrum_master", label="Scrum Master")],
                                  lo=0.0, hi=1000.0)[0].role_id == "scrum_master"


def test_validate_custom_roles_existing_id_is_not_re_keyed_by_a_new_collision() -> None:
    # An existing role (carries its role_id) keeps its slug even when a brand-new row placed BEFORE
    # it slugifies onto the same base — the new row gets the suffix, not the existing one. This keeps
    # the existing role's primary key stable (no delete+recreate / identity churn under prune).
    recs = _validate_custom_roles(
        [
            _crow(role_id=None, label="Principal Architect", rate=300.0),  # NEW, listed first
            _crow(role_id="principal_architect", label="Principal Architect", rate=310.0),  # existing
        ],
        lo=0.0,
        hi=1000.0,
    )
    by_rate = {r.rate: r.role_id for r in recs}
    assert by_rate[310.0] == "principal_architect"  # existing role keeps its id
    assert by_rate[300.0] == "principal_architect_2"  # the NEW row takes the suffix


def test_validate_custom_roles_keeps_supplied_id_verbatim() -> None:
    # A supplied (existing) role_id is reserved EXACTLY, never re-slugified — so a non-canonical
    # stored id (e.g. a hand-edited/imported 'my-role') round-trips unchanged and replace_rate_card
    # treats the edit as an in-place update, not a delete+recreate.
    recs = _validate_custom_roles(
        [_crow(role_id="my-role", label="My Role")], lo=0.0, hi=1000.0
    )
    assert recs[0].role_id == "my-role"  # NOT re-slugged to "my_role"


def test_validate_custom_roles_rejects_duplicate_supplied_id() -> None:
    # Two rows carrying the SAME explicit role_id is a malformed request — reject it rather than
    # silently splitting into two roles (the 2nd would otherwise get a "_2" suffix).
    with pytest.raises(HTTPException) as exc:
        _validate_custom_roles(
            [
                _crow(role_id="principal_architect", label="A"),
                _crow(role_id="principal_architect", label="B"),
            ],
            lo=0.0,
            hi=1000.0,
        )
    assert exc.value.status_code == 422


def test_validate_custom_roles_rejects_blank_label() -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_custom_roles([_crow(label="   ")], lo=0.0, hi=1000.0)
    assert exc.value.status_code == 422


def test_validate_custom_roles_rejects_bad_rate_and_tags() -> None:
    with pytest.raises(HTTPException):
        _validate_custom_roles([_crow(rate=5000.0)], lo=0.0, hi=1000.0)
    with pytest.raises(HTTPException):
        _validate_custom_roles([_crow(category="wizard")], lo=0.0, hi=1000.0)


@pytest.mark.asyncio
async def test_effective_rates_includes_custom_roles_field() -> None:
    postgres_adapter._reset_for_tests()  # Postgres disabled → no custom roles
    resp = await get_effective_rates()
    assert resp.custom_roles == []


@pytest.mark.asyncio
async def test_role_catalog_empty_when_disabled() -> None:
    postgres_adapter._reset_for_tests()
    resp = await get_role_catalog()
    assert resp.roles == []


def _client() -> TestClient:
    from main import app

    return TestClient(app)


def test_get_endpoint_returns_rates() -> None:
    res = _client().get("/admin/default-rates")
    assert res.status_code == 200
    body = res.json()
    assert len(body["rates"]) == 28
    assert "editable" in body


def test_put_endpoint_rejects_negative_rate() -> None:
    res = _client().put(
        "/admin/default-rates",
        json={"rates": [{"category": "engineering", "seniority": "senior", "rate": -10.0}]},
    )
    assert res.status_code == 422


def test_put_endpoint_rejects_blank_custom_role_label() -> None:
    res = _client().put(
        "/admin/default-rates",
        json={
            "rates": [],
            "custom_roles": [
                {"label": "  ", "category": "engineering", "seniority": "senior", "rate": 200.0}
            ],
        },
    )
    assert res.status_code == 422


def test_role_catalog_endpoint_returns_roles_key() -> None:
    res = _client().get("/role-catalog")
    assert res.status_code == 200
    assert "roles" in res.json()
