"""Repository functions for the Postgres persistence layer.

Split by concern into three modules; this package re-exports their public functions
so existing call sites keep importing ``from db.repositories import X`` unchanged:

* ``history``     — denormalize an `EstimateEnvelope` into the history tables and read
  it back (`save_estimate_history`, `list_estimate_history`, `get_estimate_envelope`).
* ``calibration`` — recompute + read rolling per-(phase, industry, project_type,
  codebase-context) aggregates (`refresh_calibration_for_phase`, `get_calibration`,
  `get_calibration_for_all_phases`). The codebase-context code rides in the column
  historically named `maturity_level`.
* ``bands``       — per-(phase, tooling_level) AI-reduction guardrail bands
  (`get_reduction_bands`, `upsert_reduction_bands`).

All three concerns share the never-raise persistence contract: each function tolerates
Postgres being disabled / unreachable by returning the empty case (None / [] / {} /
0 / False) instead of raising, so the HTTP layer never fails because of persistence.
"""

from __future__ import annotations

from db.repositories.app_settings import (
    get_app_setting,
    get_app_settings_map,
    set_app_setting,
)
from db.repositories.bands import get_reduction_bands, upsert_reduction_bands
from db.repositories.calibration import (
    get_calibration,
    get_calibration_for_all_phases,
    refresh_calibration_for_phase,
)
from db.repositories.history import (
    aggregate_llm_usage,
    associate_llm_calls,
    calls_from_summary,
    count_estimate_history,
    delete_estimate_history,
    get_estimate_envelope,
    insert_llm_calls,
    list_estimate_history,
    save_estimate_history,
    save_llm_calls,
)
from db.repositories.rates import (
    CustomRoleRecord,
    get_custom_roles,
    get_default_rates,
    replace_rate_card,
)
from db.repositories.staffing import (
    get_staffing_coefficients,
    upsert_staffing_coefficients,
)

__all__ = [
    # history
    "save_estimate_history",
    "list_estimate_history",
    "count_estimate_history",
    "delete_estimate_history",
    "get_estimate_envelope",
    "aggregate_llm_usage",
    "save_llm_calls",
    "insert_llm_calls",
    "associate_llm_calls",
    "calls_from_summary",
    # calibration
    "refresh_calibration_for_phase",
    "get_calibration",
    "get_calibration_for_all_phases",
    # reduction bands
    "get_reduction_bands",
    "upsert_reduction_bands",
    # staffing coefficients
    "get_staffing_coefficients",
    "upsert_staffing_coefficients",
    # default rate card
    "get_default_rates",
    # custom rate-card roles
    "CustomRoleRecord",
    "get_custom_roles",
    "replace_rate_card",
    # generic app settings
    "get_app_setting",
    "get_app_settings_map",
    "set_app_setting",
]
