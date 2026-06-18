"""Default hourly rate card — the org's standard blended rates per role
``(category × seniority)``.

``DEFAULT_RATES`` below is the in-code fallback; the DB ``default_rates`` table (admin-editable
on the Settings screen) overrides any cell. ``resolve_rate`` merges them. The roster agent
assigns these rates to the proposed roster, and the user can still override any rate per estimate
in the Stage 2 roster editor — rates are a user-owned commercial input, never taken from the LLM.

Kept in this neutral module (not in ``roster_agent``) so the admin surface + repo can import the
defaults without pulling in the LLM/agent machinery.
"""

from __future__ import annotations

from models.twin_outputs import RoleCategory, RoleSeniority

# Used for any (category, seniority) cell missing from DEFAULT_RATES / the DB.
RATE_FALLBACK = 165.0

# Editable range per rate (the Settings admin validates against these).
RATE_BOUNDS: tuple[float, float] = (0.0, 1000.0)

# Standard rate card. Engineering/product senior+junior cells match ``RoleRoster.default()`` so a
# vanilla proposal round-trips identically; the rest extend the same shape to every category.
DEFAULT_RATES: dict[tuple[RoleCategory, RoleSeniority], float] = {
    (RoleCategory.PRODUCT, RoleSeniority.SENIOR): 220.0,
    (RoleCategory.PRODUCT, RoleSeniority.MID): 180.0,
    (RoleCategory.PRODUCT, RoleSeniority.JUNIOR): 140.0,
    (RoleCategory.PRODUCT, RoleSeniority.OTHER): 180.0,
    (RoleCategory.ENGINEERING, RoleSeniority.SENIOR): 240.0,
    (RoleCategory.ENGINEERING, RoleSeniority.MID): 195.0,
    (RoleCategory.ENGINEERING, RoleSeniority.JUNIOR): 150.0,
    (RoleCategory.ENGINEERING, RoleSeniority.OTHER): 195.0,
    (RoleCategory.UI_UX, RoleSeniority.SENIOR): 200.0,
    (RoleCategory.UI_UX, RoleSeniority.MID): 165.0,
    (RoleCategory.UI_UX, RoleSeniority.JUNIOR): 130.0,
    (RoleCategory.UI_UX, RoleSeniority.OTHER): 165.0,
    (RoleCategory.QA, RoleSeniority.SENIOR): 170.0,
    (RoleCategory.QA, RoleSeniority.MID): 140.0,
    (RoleCategory.QA, RoleSeniority.JUNIOR): 110.0,
    (RoleCategory.QA, RoleSeniority.OTHER): 140.0,
    (RoleCategory.DEVOPS, RoleSeniority.SENIOR): 230.0,
    (RoleCategory.DEVOPS, RoleSeniority.MID): 190.0,
    (RoleCategory.DEVOPS, RoleSeniority.JUNIOR): 150.0,
    (RoleCategory.DEVOPS, RoleSeniority.OTHER): 190.0,
    (RoleCategory.DATA, RoleSeniority.SENIOR): 235.0,
    (RoleCategory.DATA, RoleSeniority.MID): 195.0,
    (RoleCategory.DATA, RoleSeniority.JUNIOR): 150.0,
    (RoleCategory.DATA, RoleSeniority.OTHER): 195.0,
    (RoleCategory.OTHER, RoleSeniority.SENIOR): 200.0,
    (RoleCategory.OTHER, RoleSeniority.MID): 165.0,
    (RoleCategory.OTHER, RoleSeniority.JUNIOR): 130.0,
    (RoleCategory.OTHER, RoleSeniority.OTHER): 165.0,
}


def resolve_rate(
    category: RoleCategory,
    seniority: RoleSeniority,
    overrides: dict[tuple[RoleCategory, RoleSeniority], float] | None = None,
) -> float:
    """The effective hourly rate for a ``(category, seniority)`` — a DB override wins over the
    code default, which falls back to ``RATE_FALLBACK``."""
    if overrides is not None:
        cell = overrides.get((category, seniority))
        if cell is not None:
            return cell
    return DEFAULT_RATES.get((category, seniority), RATE_FALLBACK)
