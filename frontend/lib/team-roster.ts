import type { CustomRoleInput } from "./schemas";

/** A roster role resolved to an individual team member, with a stable display label. */
export interface TeamMember extends CustomRoleInput {
  /** A/B/C… disambiguator when this role description appears more than once; null when unique. */
  designation: string | null;
  /** Display name — role description, plus the designation when more than one seat shares it. */
  label: string;
}

/** Spreadsheet-style designations: 0→A, 1→B, … 25→Z, 26→AA, 27→AB. Lets a team of arbitrary size
 *  stay uniquely labeled when several members share the same role. */
export function memberDesignation(index: number): string {
  let n = index + 1;
  let s = "";
  while (n > 0) {
    const rem = (n - 1) % 26;
    s = String.fromCharCode(65 + rem) + s;
    n = Math.floor((n - 1) / 26);
  }
  return s;
}

/** Core A/B disambiguation, parameterized on how to read each item's display description. Items
 *  sharing a description get an A/B/C… designation in order; a unique description gets `null`.
 *  Returns `{ designation, label }` per item, in input order — the single source of truth reused by
 *  both the roster labeler (below) and the WBS Gantt/work-breakdown labeler (`teamMemberLabels`). */
export function designateLabels<T>(
  items: T[],
  describe: (item: T) => string,
): { designation: string | null; label: string }[] {
  const counts = new Map<string, number>();
  for (const it of items) counts.set(describe(it), (counts.get(describe(it)) ?? 0) + 1);

  const seen = new Map<string, number>();
  return items.map((it) => {
    const desc = describe(it);
    let designation: string | null = null;
    if ((counts.get(desc) ?? 0) > 1) {
      const idx = seen.get(desc) ?? 0;
      seen.set(desc, idx + 1);
      designation = memberDesignation(idx);
    }
    return { designation, label: designation ? `${desc} ${designation}` : desc };
  });
}

/** Turn a roster into individual team members. Members whose role `description` appears more than
 *  once get an A/B/C… suffix (in roster order); a role that appears exactly once keeps its plain
 *  description and a null `designation`. */
export function designateTeamMembers(roster: CustomRoleInput[]): TeamMember[] {
  const labels = designateLabels(roster, (r) => r.description);
  return roster.map((r, i) => ({ ...r, ...labels[i] }));
}
