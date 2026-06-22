"use client";

import { useEffect, useId, useState } from "react";

import { FieldHint } from "@/components/FieldHint";
import { getRoleCatalog, type CustomRoleRow } from "@/lib/api-client";
import {
  ROLE_CATEGORY_OPTIONS,
  ROLE_SENIORITY_OPTIONS,
  type CustomRoleInput,
  type RoleCategory,
  type RoleSeniority,
} from "@/lib/schemas";

interface Props {
  value: CustomRoleInput[];
  onChange: (next: CustomRoleInput[]) => void;
  /** Lock every control — used while the AG-UI roster agent is proposing a
   *  tailored team, so the user doesn't edit a roster that's about to be
   *  replaced. Re-enabled once the proposal lands (or the run fails). */
  disabled?: boolean;
}

// Valid-value sets for guarding catalog tags against the canonical option lists (from lib/schemas).
const CATEGORY_VALUES = new Set<string>(ROLE_CATEGORY_OPTIONS.map((o) => o.value));
const SENIORITY_VALUES = new Set<string>(ROLE_SENIORITY_OPTIONS.map((o) => o.value));

/** Parse a raw input value into a whole integer in [0, 100].
 *
 *  Non-numeric / NaN / negative → 0. Above 100 → 100. Fractional → truncated.
 *  Used by the per-row number input so users can only commit valid whole shares.
 */
export function clampPercentage(value: number | string): number {
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n)) return 0;
  return Math.max(0, Math.min(100, Math.trunc(n)));
}

/** Rebalance after a single row's percentage is committed.
 *
 *  Returns a new roster where the changed row holds `clampPercentage(newValue)`
 *  and the other rows proportionally absorb the remaining `100 - newValue` so the
 *  total stays at exactly 100. When the other rows are all zero, the remainder
 *  is split evenly across them. All percentages end up as whole integers; any
 *  rounding drift is dropped onto the largest "other" row.
 *
 *  A single-row-anchored rebalance utility. (The roster editor no longer rebalances
 *  implicitly on blur — see `normalizeShares` + the "Auto-adjust to 100%" button.)
 *  Exported for testing / reuse.
 */
export function rebalanceOnEdit(
  roles: CustomRoleInput[],
  changedIndex: number,
  newValue: number
): CustomRoleInput[] {
  if (roles.length === 0) return roles;
  if (roles.length === 1) {
    return [{ ...roles[0], percentage: 100 }];
  }

  const clamped = clampPercentage(newValue);
  const next = roles.map((r, i) =>
    i === changedIndex ? { ...r, percentage: clamped } : { ...r }
  );

  const otherIdxs = next.map((_, i) => i).filter((i) => i !== changedIndex);
  const remaining = 100 - clamped;
  const otherSum = otherIdxs.reduce((acc, i) => acc + roles[i].percentage, 0);

  if (otherSum <= 0) {
    const share = remaining / otherIdxs.length;
    otherIdxs.forEach((i) => {
      next[i].percentage = share;
    });
  } else {
    otherIdxs.forEach((i) => {
      next[i].percentage = (roles[i].percentage / otherSum) * remaining;
    });
  }

  for (const r of next) r.percentage = Math.round(r.percentage);
  const total = next.reduce((a, r) => a + r.percentage, 0);
  if (total !== 100 && otherIdxs.length > 0) {
    const drift = 100 - total;
    const largestOther = otherIdxs.reduce((a, b) =>
      next[a].percentage >= next[b].percentage ? a : b
    );
    next[largestOther].percentage += drift;
  }
  return next;
}

/** Generate a stable slug for new rows so role_id stays human-readable. */
function makeRoleId(existing: CustomRoleInput[]): string {
  let i = existing.length + 1;
  while (existing.some((r) => r.role_id === `role_${i}`)) i += 1;
  return `role_${i}`;
}

/** Append a new role row, "stealing" up to 10% from whichever row has the most to give so the
 *  sum stays at 100 without re-typing every other field. The caller supplies the role's fields
 *  (minus role_id + percentage, which are assigned here). Shared by `addRow` (a blank role) and
 *  `addRoleFromCatalog` (a prefilled rate-card role). */
function appendRole(
  roles: CustomRoleInput[],
  fields: Omit<CustomRoleInput, "role_id" | "percentage">,
): CustomRoleInput[] {
  const stealFromIdx =
    roles.length === 0
      ? -1
      : roles.reduce(
          (best, r, i, arr) => (r.percentage > arr[best].percentage ? i : best),
          0
        );
  const stolen = stealFromIdx >= 0 ? Math.min(10, roles[stealFromIdx].percentage) : 100;
  const next = roles.map((r) => ({ ...r }));
  if (stealFromIdx >= 0) {
    next[stealFromIdx].percentage -= stolen;
  }
  next.push({ ...fields, role_id: makeRoleId(roles), percentage: stolen });
  return next;
}

/** Add a new blank role row at the bottom. Exported for testing. */
export function addRow(roles: CustomRoleInput[]): CustomRoleInput[] {
  return appendRole(roles, {
    description: "New role",
    category: "other",
    seniority: "other",
    rate_per_hour: 150,
  });
}

/** Add a role prefilled from an admin-defined rate-card catalog entry (label → description, plus
 *  its category / seniority / rate). Exported for testing. */
export function addRoleFromCatalog(
  roles: CustomRoleInput[],
  entry: CustomRoleRow,
): CustomRoleInput[] {
  // The catalog comes over the wire; clamp its tags to the known option sets (falling back to
  // "other") so a backend enum the frontend hasn't shipped — or a stale row — can't inject an
  // out-of-range category/seniority that the row's <select> couldn't render.
  const category = (CATEGORY_VALUES.has(entry.category) ? entry.category : "other") as RoleCategory;
  const seniority = (
    SENIORITY_VALUES.has(entry.seniority) ? entry.seniority : "other"
  ) as RoleSeniority;
  return appendRole(roles, {
    description: entry.label,
    category,
    seniority,
    rate_per_hour: entry.rate,
  });
}

/** Remove a row and proportionally return its share to the remaining rows so
 *  the sum stays at 100. Drift from integer rounding lands on the row with the
 *  largest remaining share. Exported for testing. */
export function removeRow(roles: CustomRoleInput[], index: number): CustomRoleInput[] {
  if (roles.length <= 1) return roles;
  const removed = roles[index];
  const remaining = roles.filter((_, i) => i !== index);
  const otherSum = remaining.reduce((a, r) => a + r.percentage, 0);
  if (otherSum <= 0) {
    const share = Math.round(100 / remaining.length);
    const out = remaining.map((r) => ({ ...r, percentage: share }));
    // Drift correction when 100 doesn't divide evenly (e.g. 3 rows → 33+33+34).
    const drift = 100 - out.reduce((a, r) => a + r.percentage, 0);
    if (drift !== 0) out[0].percentage += drift;
    return out;
  }
  const redistributed = remaining.map((r) => ({
    ...r,
    percentage: r.percentage + (r.percentage / otherSum) * removed.percentage,
  }));
  for (const r of redistributed) r.percentage = Math.round(r.percentage);
  const total = redistributed.reduce((a, r) => a + r.percentage, 0);
  if (total !== 100) {
    const drift = 100 - total;
    const largest = redistributed.reduce(
      (best, _r, i) => (redistributed[i].percentage >= redistributed[best].percentage ? i : best),
      0
    );
    redistributed[largest].percentage += drift;
  }
  return redistributed;
}

/** Proportionally rescale every row's share so they total exactly 100 (whole
 *  integers). When all rows are zero, splits evenly. Drift from rounding lands on
 *  the largest row. Invoked by the "Auto-adjust to 100%" button — never implicitly.
 */
export function normalizeShares(roles: CustomRoleInput[]): CustomRoleInput[] {
  if (roles.length === 0) return roles;
  if (roles.length === 1) return [{ ...roles[0], percentage: 100 }];

  const sum = roles.reduce((acc, r) => acc + r.percentage, 0);
  const next =
    sum <= 0
      ? roles.map((r) => ({ ...r, percentage: Math.round(100 / roles.length) }))
      : roles.map((r) => ({
          ...r,
          percentage: Math.round((r.percentage / sum) * 100),
        }));

  const total = next.reduce((acc, r) => acc + r.percentage, 0);
  if (total !== 100) {
    const drift = 100 - total;
    const largest = next.reduce(
      (best, _r, i) => (next[i].percentage >= next[best].percentage ? i : best),
      0,
    );
    next[largest].percentage += drift;
  }
  return next;
}

export function RoleRosterEditor({ value, onChange, disabled = false }: Props) {
  const headingId = useId();
  const total = value.reduce((acc, r) => acc + r.percentage, 0);
  const sumValid = total === 100;

  // Admin-defined custom roles offered as a quick-add catalog. Best-effort: an empty list (Postgres
  // off / none defined / fetch failed) simply hides the picker.
  const [catalog, setCatalog] = useState<CustomRoleRow[]>([]);
  useEffect(() => {
    let cancelled = false;
    getRoleCatalog()
      .then((r) => {
        if (!cancelled) setCatalog(r.roles);
      })
      .catch(() => {
        /* no catalog → just hide the picker */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const updateRow = (idx: number, patch: Partial<CustomRoleInput>) => {
    const next = value.map((r, i) => (i === idx ? { ...r, ...patch } : r));
    onChange(next);
  };

  return (
    <div className="space-y-3" aria-labelledby={headingId}>
      <div className="flex items-center justify-between gap-2">
        <h3 id={headingId} className="section-title">
          Team roster
        </h3>
        <div className="flex items-center gap-2">
          {catalog.length > 0 && (
            <select
              value=""
              disabled={disabled}
              onChange={(e) => {
                // Controlled at value="" → React resets it to the placeholder after each pick.
                const entry = catalog.find((c) => c.role_id === e.target.value);
                if (entry) onChange(addRoleFromCatalog(value, entry));
              }}
              className="select text-xs py-1 disabled:opacity-50 disabled:cursor-not-allowed"
              aria-label="Add a role from the rate-card catalog"
              title="Add a predefined custom role from the rate card"
            >
              <option value="" disabled>
                + Add from catalog…
              </option>
              {catalog.map((c) => (
                <option key={c.role_id} value={c.role_id}>
                  {c.label} (${c.rate}/h)
                </option>
              ))}
            </select>
          )}
          <button
            type="button"
            onClick={() => onChange(addRow(value))}
            disabled={disabled}
            className="btn-secondary text-xs disabled:opacity-50 disabled:cursor-not-allowed"
          >
            + Add role
          </button>
        </div>
      </div>
      <p className="text-xs muted">
        Define each resource on the team — a free-form description, category,
        seniority, hourly rate, and effort share. The category and seniority tags
        drive phase-specific role biases (e.g. Discovery is senior-biased,
        Deployment is engineering/devops-biased). Effort shares are whole-integer
        percentages from 0–100; edit them freely, then use{" "}
        <span className="font-medium">Auto-adjust to 100%</span> to rescale them
        proportionally so they total 100.
      </p>

      <div className="space-y-3">
        {value.map((row, idx) => (
          <div
            key={row.role_id}
            className="rounded-md border border-slate-200 bg-white p-3 space-y-2"
          >
            <div>
              <label className="label text-xs inline-flex items-center">
                Role description
                <FieldHint text="Free-form description of the role: responsibilities, scope, seniority context, anything that helps interpret the line. Up to 500 characters. Travels into every PhaseEstimate's role_hours and the Stage 5 staffing card." />
              </label>
              <textarea
                className="input mt-1 min-h-[5rem] resize-y leading-snug disabled:opacity-60"
                value={row.description}
                disabled={disabled}
                onChange={(e) =>
                  updateRow(idx, { description: e.target.value })
                }
                placeholder="e.g. Senior backend engineer responsible for API design, on-call rotation, and PR reviews"
                rows={3}
                maxLength={500}
              />
              <p className="mt-1 text-[10px] muted text-right">
                {row.description.length} / 500
              </p>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-12 gap-2 items-end">
              <div className="md:col-span-4">
                <label className="label text-xs inline-flex items-center">
                  Category
                  <FieldHint text="Functional category drives phase-specific role biases — e.g. Discovery is senior-biased, UX prefers product / ui_ux, Deployment prefers engineering / devops / data. Tag as 'Other' to opt out of overrides." />
                </label>
                <select
                  className="select mt-1 disabled:opacity-60"
                  value={row.category}
                  disabled={disabled}
                  onChange={(e) =>
                    updateRow(idx, { category: e.target.value as RoleCategory })
                  }
                >
                  {ROLE_CATEGORY_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </div>
              <div className="md:col-span-3">
                <label className="label text-xs inline-flex items-center">
                  Seniority
                  <FieldHint text="Senior vs Junior triggers caps in Discovery (juniors ≤ 25%) and Code Review (juniors ≤ 15%), with excess pushed to a same-category senior. Use 'Mid' or 'Other' to skip the cap." />
                </label>
                <select
                  className="select mt-1 disabled:opacity-60"
                  value={row.seniority}
                  disabled={disabled}
                  onChange={(e) =>
                    updateRow(idx, { seniority: e.target.value as RoleSeniority })
                  }
                >
                  {ROLE_SENIORITY_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </div>
              <div className="md:col-span-2">
                <label className="label text-xs inline-flex items-center">
                  $ / hr
                  <FieldHint text="Blended hourly rate for this role in USD. Applied during commercial_processing to derive per-phase and total cost. Set to 0 to exclude a role from cost (still counts toward effort)." />
                </label>
                <input
                  type="number"
                  min={0}
                  className="input mt-1 disabled:opacity-60"
                  value={row.rate_per_hour}
                  disabled={disabled}
                  onChange={(e) =>
                    updateRow(idx, {
                      rate_per_hour: Number.isFinite(Number(e.target.value))
                        ? Number(e.target.value)
                        : 0,
                    })
                  }
                />
              </div>
              <div className="md:col-span-2">
                <label className="label text-xs inline-flex items-center">
                  Share %
                  <FieldHint text="Whole-integer percentage of total effort this role consumes. Type any 0–100 value; other rows are left untouched. Use the 'Auto-adjust to 100%' button to rescale all shares proportionally so they total 100." />
                </label>
                <input
                  type="number"
                  min={0}
                  max={100}
                  step={1}
                  inputMode="numeric"
                  className="input mt-1 disabled:opacity-60"
                  value={row.percentage}
                  disabled={disabled}
                  onChange={(e) =>
                    // Edit only this row; other rows are left untouched. Use the
                    // "Auto-adjust to 100%" button to rebalance the whole roster.
                    updateRow(idx, { percentage: clampPercentage(e.target.value) })
                  }
                  aria-label={`${row.description || "role"} effort percentage`}
                />
              </div>
              <div className="md:col-span-1">
                <button
                  type="button"
                  onClick={() => onChange(removeRow(value, idx))}
                  disabled={disabled || value.length <= 1}
                  className="btn-secondary text-xs w-full flex items-center justify-center disabled:opacity-50 disabled:cursor-not-allowed mt-5"
                  aria-label={`Remove ${row.description || "role"}`}
                  title={
                    value.length <= 1
                      ? "At least one role is required"
                      : "Remove this role"
                  }
                >
                  {/* Trash bin (Heroicons "trash" outline, inlined to avoid a new dep). */}
                  <svg
                    xmlns="http://www.w3.org/2000/svg"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth={1.75}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    className="h-4 w-4"
                    aria-hidden="true"
                  >
                    <path d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
                  </svg>
                </button>
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <p
          className={
            sumValid ? "text-xs muted" : "text-xs text-rose-600 font-medium"
          }
          role="status"
          aria-live="polite"
        >
          Total: {total}% {sumValid ? "" : `(must equal 100 — adjust by ${100 - total > 0 ? `+${100 - total}` : `${100 - total}`})`}
        </p>
        <button
          type="button"
          onClick={() => onChange(normalizeShares(value))}
          disabled={disabled || sumValid || value.length === 0}
          className="btn-secondary text-xs disabled:opacity-50 disabled:cursor-not-allowed"
          title="Proportionally rescale all shares so they total 100%"
        >
          Auto-adjust to 100%
        </button>
      </div>
    </div>
  );
}
