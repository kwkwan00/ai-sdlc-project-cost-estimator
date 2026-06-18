"""Generic ``app_settings`` key→value store (string-valued admin settings).

``get_app_setting(key, default)`` returns the stored value or ``default`` (Postgres off / key
unset); ``set_app_setting(key, value)`` upserts one key. Both honor the never-raise contract via
the shared ``_common`` helpers. Mirrors the other keyed-KV repos.
"""

from __future__ import annotations

from db.orm_models import AppSetting
from db.repositories._common import fetch_all_rows, upsert_keyed


async def get_app_setting(key: str, default: str) -> str:
    """The string value for ``key``, or ``default`` when Postgres is disabled / the key is unset."""
    for row in await fetch_all_rows(AppSetting):
        if row.key == key:
            return row.value
    return default


async def get_app_settings_map() -> dict[str, str]:
    """All ``app_settings`` rows as a ``{key: value}`` map in a SINGLE full-table read.

    Lets a caller resolve several keys from one snapshot instead of one full-table read per
    key. Returns ``{}`` when Postgres is disabled / unreachable — the same degrade semantics
    as :func:`get_app_setting` (callers then fall back to their per-key defaults)."""
    return {row.key: row.value for row in await fetch_all_rows(AppSetting)}


def _apply_setting(row: AppSetting, item: tuple[str, str]) -> None:
    row.value = item[1]


def _make_setting(item: tuple[str, str]) -> AppSetting:
    return AppSetting(key=item[0], value=item[1])


async def set_app_setting(key: str, value: str) -> bool:
    """Upsert one ``(key, value)`` setting. Returns True when persisted, False when Postgres is
    disabled or the write fails."""
    return await upsert_keyed(
        AppSetting,
        [(key, value)],
        key_of=lambda i: i[0],
        key_of_row=lambda r: r.key,
        apply=_apply_setting,
        make=_make_setting,
    )
