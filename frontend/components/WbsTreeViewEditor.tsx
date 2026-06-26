"use client";

import EditIcon from "@mui/icons-material/Edit";
import IconButton from "@mui/material/IconButton";
import { SimpleTreeView } from "@mui/x-tree-view/SimpleTreeView";
import { TreeItem } from "@mui/x-tree-view/TreeItem";
import { useMemo, useState } from "react";

import { Modal } from "@/components/Modal";
import { type CustomRoleInput } from "@/lib/schemas";
import { designateTeamMembers, type TeamMember } from "@/lib/team-roster";
import { PHASE_LABELS, type Phase } from "@/lib/types";
import {
  addChild,
  branchIds,
  clampHours,
  countLeaves,
  dependencyTargets,
  findNode,
  isLeaf,
  moveNode,
  moveTargets,
  newLeaf,
  newPackage,
  PHASE_ORDER,
  pertMean,
  pruneDanglingDependencies,
  removeNode,
  rolledHoursMap,
  updateNode,
  type WbsTaskInput,
} from "@/lib/wbs";
import { PHASE_COLORS, PHASE_FALLBACK_COLOR } from "@/lib/wbs-colors";

interface Props {
  tree: WbsTaskInput[];
  roster: CustomRoleInput[];
  onChange: (next: WbsTaskInput[]) => void;
}

interface LeafFieldsProps {
  leaf: WbsTaskInput;
  members: TeamMember[];
  defaultRole: string;
  dependsOnOptions: { id: string; name: string }[];
  patch: (id: string, p: Partial<WbsTaskInput>) => void;
}

type HourField = "optimistic" | "most_likely" | "pessimistic";

/** Small round badge for a task's assignee — the member's A/B/C designation (when the role is
 *  shared by more than one seat) or the role's initial otherwise. Visually ties a task row to the
 *  individual in the Team roster. */
function AssigneeBadge({ member }: { member?: TeamMember }) {
  if (!member) return null;
  return (
    <span
      title={member.label}
      className="inline-flex h-4 shrink-0 items-center justify-center rounded-full bg-brand-50 px-1.5 text-[0.65rem] font-semibold text-brand-700"
    >
      {member.designation ?? member.description.charAt(0).toUpperCase()}
    </span>
  );
}

/** A "Depends on" multi-select: a scrollable checkbox list of eligible predecessors (same-kind
 *  nodes from `dependencyTargets`). Selection order follows the option (document) order so it stays
 *  stable across toggles. Renders a muted note when there's nothing eligible to depend on. */
function DependsOnField({
  value,
  options,
  emptyNote,
  onChange,
}: {
  value: string[];
  options: { id: string; name: string }[];
  emptyNote: string;
  onChange: (next: string[]) => void;
}) {
  const selected = new Set(value);
  const toggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange(options.filter((o) => next.has(o.id)).map((o) => o.id));
  };
  return (
    <div className="block">
      <span className="label">Depends on</span>
      {options.length === 0 ? (
        <p className="text-xs muted">{emptyNote}</p>
      ) : (
        <>
          <div className="mt-1 max-h-32 space-y-1 overflow-y-auto rounded-md border border-slate-200 p-2">
            {options.map((o) => (
              <label key={o.id} className="flex cursor-pointer items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 shrink-0"
                  checked={selected.has(o.id)}
                  onChange={() => toggle(o.id)}
                />
                <span className="truncate text-slate-700">{o.name}</span>
              </label>
            ))}
          </div>
          <p className="help">Predecessors that must finish before this can start.</p>
        </>
      )}
    </div>
  );
}

/** Edit-modal body for a leaf task: phase, assignee, 3-point hours, PERT readout, description. */
function LeafFields({ leaf, members, defaultRole, dependsOnOptions, patch }: LeafFieldsProps) {
  // Keep a transient string draft for the ONE hour field currently being edited so the user can
  // clear it / type a partial value ("", "12.") without the controlled value snapping back to a
  // number mid-typing. The committed tree value is always the clamped non-negative number.
  const [draft, setDraft] = useState<{ field: HourField; value: string } | null>(null);
  return (
    <>
      <div className="grid grid-cols-2 gap-2">
        <label className="block">
          <span className="label">Phase</span>
          <select
            className="input"
            value={leaf.phase ?? "development"}
            onChange={(e) => patch(leaf.id, { phase: e.target.value as Phase })}
          >
            {PHASE_ORDER.map((p) => (
              <option key={p} value={p}>
                {PHASE_LABELS[p]}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="label">Assignee</span>
          <select
            className="input"
            value={leaf.role_id ?? defaultRole}
            onChange={(e) => patch(leaf.id, { role_id: e.target.value })}
          >
            {members.map((m) => (
              <option key={m.role_id} value={m.role_id}>
                {m.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="grid grid-cols-3 gap-2">
        {(
          [
            ["Optimistic", "optimistic"],
            ["Most likely", "most_likely"],
            ["Pessimistic", "pessimistic"],
          ] as const
        ).map(([label, field]) => {
          const isEditing = draft?.field === field;
          const displayValue = isEditing ? draft.value : String(leaf[field] ?? 0);
          return (
            <label key={field} className="block">
              <span className="label">{label}</span>
              <input
                type="number"
                min={0}
                step="any"
                className="input"
                value={displayValue}
                onChange={(e) => {
                  const raw = e.target.value;
                  setDraft({ field, value: raw });
                  // Commit the clamped number whenever the draft parses to a finite value; leave
                  // transient states ("", "12.", "-") uncommitted so typing isn't interrupted.
                  if (raw.trim() !== "" && Number.isFinite(Number(raw))) {
                    patch(leaf.id, { [field]: clampHours(raw) });
                  }
                }}
                onBlur={() => {
                  // On blur, commit the clamped value (""/NaN → 0) and drop the draft so the field
                  // shows the persisted number again.
                  if (draft?.field === field) {
                    patch(leaf.id, { [field]: clampHours(draft.value) });
                  }
                  setDraft(null);
                }}
              />
            </label>
          );
        })}
      </div>
      <p className="text-xs muted">
        PERT mean ≈{" "}
        <span className="font-semibold text-slate-700">
          {Math.round(
            pertMean(
              leaf.optimistic ?? 0,
              leaf.most_likely ?? 0,
              leaf.pessimistic ?? 0,
            ),
          )}{" "}
          h
        </span>
      </p>

      <label className="block">
        <span className="label">Description (optional)</span>
        <textarea
          className="input min-h-[3rem]"
          value={leaf.description ?? ""}
          onChange={(e) => patch(leaf.id, { description: e.target.value })}
        />
      </label>

      <DependsOnField
        value={leaf.depends_on ?? []}
        options={dependsOnOptions}
        emptyNote="No other tasks to depend on yet."
        onChange={(next) => patch(leaf.id, { depends_on: next })}
      />
    </>
  );
}

interface BranchFieldsProps {
  branch: WbsTaskInput;
  hours: Map<string, number>;
  memberById: Map<string, TeamMember>;
  dependsOnOptions: { id: string; name: string }[];
  onSelect: (id: string) => void;
  onAddTask: (parentId: string) => void;
  patch: (id: string, p: Partial<WbsTaskInput>) => void;
}

/** Edit-modal body for a branch (work package): child summary + drill-in list + add-task +
 *  a "depends on" selector limited to other work packages. */
function BranchFields({
  branch,
  hours,
  memberById,
  dependsOnOptions,
  onSelect,
  onAddTask,
  patch,
}: BranchFieldsProps) {
  return (
    <div className="space-y-2">
      <p className="text-sm text-slate-700">
        {countLeaves(branch.children)} task
        {countLeaves(branch.children) === 1 ? "" : "s"} ·{" "}
        <span className="font-semibold">{Math.round(hours.get(branch.id) ?? 0)} h</span>
      </p>
      <ul className="divide-y divide-slate-100">
        {branch.children.map((c) => (
          <li key={c.id}>
            <button
              type="button"
              onClick={() => onSelect(c.id)}
              className="flex w-full items-center justify-between gap-2 rounded px-2 -mx-2 py-2 text-left hover:bg-slate-50"
            >
              <span className="flex min-w-0 items-center gap-2">
                {isLeaf(c) && c.phase && (
                  <span
                    className="h-2.5 w-2.5 shrink-0 rounded-sm"
                    style={{ backgroundColor: PHASE_COLORS[c.phase] ?? PHASE_FALLBACK_COLOR }}
                  />
                )}
                <span className="truncate text-sm">{c.name}</span>
                {isLeaf(c) && c.role_id && <AssigneeBadge member={memberById.get(c.role_id)} />}
              </span>
              <span className="shrink-0 text-xs muted">
                {Math.round(hours.get(c.id) ?? 0)} h ›
              </span>
            </button>
          </li>
        ))}
      </ul>
      <button
        type="button"
        onClick={() => onAddTask(branch.id)}
        className="btn-secondary text-xs"
      >
        + Add task
      </button>

      <div className="border-t border-slate-100 pt-2">
        <DependsOnField
          value={branch.depends_on ?? []}
          options={dependsOnOptions}
          emptyNote="No other work packages to depend on yet."
          onChange={(next) => patch(branch.id, { depends_on: next })}
        />
      </div>
    </div>
  );
}

/** Edit WBS via a MUI X Tree View. Each row has an edit (pencil) icon that opens a modal to edit
 *  the node (name / phase / role / 3-point hours), add a child task, move it, or delete it. */
export function WbsTreeViewEditor({ tree, roster, onChange }: Props) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string[]>(() => branchIds(tree));

  // One bottom-up pass for every node's rolled-up most-likely hours (O(n)); replaces the per-node
  // subtreeMostLikely re-walks that were O(n²) on every keystroke.
  const hours = useMemo(() => rolledHoursMap(tree), [tree]);

  // Resolve the roster into individual team members (duplicate roles get A/B/C designations) so
  // tasks can be assigned to — and labeled by — a specific person, not just a role.
  const members = useMemo(() => designateTeamMembers(roster), [roster]);
  const memberById = useMemo(
    () => new Map(members.map((m) => [m.role_id, m])),
    [members],
  );

  const defaultRole = roster[0]?.role_id ?? "sr_engineer";
  const defaultPhase: Phase = "development";

  const selected = selectedId ? findNode(tree, selectedId) : null;

  // --- edits -------------------------------------------------------------------------------
  const patch = (id: string, p: Partial<WbsTaskInput>) => onChange(updateNode(tree, id, p));
  const addPackage = () => {
    const pkg = newPackage(defaultPhase, defaultRole);
    onChange([...tree, pkg]);
    setExpanded((e) => [...e, pkg.id]);
  };
  const addTopTask = () => onChange([...tree, newLeaf(defaultPhase, defaultRole)]);
  const addTaskTo = (parentId: string) => {
    // Adding a child flips a leaf parent into a branch; prune so a sibling's now-cross-kind
    // depends_on edge (leaf→package) and the parent's own now-invalid leaf deps are scrubbed.
    onChange(pruneDanglingDependencies(addChild(tree, parentId, newLeaf(defaultPhase, defaultRole))));
    setExpanded((e) => (e.includes(parentId) ? e : [...e, parentId]));
  };
  const remove = (id: string) => {
    // Prune the node, then scrub any now-dangling depends_on edges that pointed at it (or at a
    // package the removal emptied + pruned) so the tree stays referentially valid.
    onChange(pruneDanglingDependencies(removeNode(tree, id)));
    setSelectedId(null);
  };
  // moveNode can empty+prune the source package and flip the target leaf into a branch; prune after
  // so no depends_on edge is left dangling or cross-kind (mirrors `remove`).
  const move = (id: string, target: string | null) =>
    onChange(pruneDanglingDependencies(moveNode(tree, id, target)));

  // --- recursive tree rows -----------------------------------------------------------------
  const renderItem = (node: WbsTaskInput) => (
    <TreeItem
      key={node.id}
      itemId={node.id}
      label={
        <div className="flex items-center justify-between gap-2 py-0.5">
          <span className="flex min-w-0 items-center gap-2">
            {isLeaf(node) && node.phase && (
              <span
                className="h-2.5 w-2.5 shrink-0 rounded-sm"
                style={{ backgroundColor: PHASE_COLORS[node.phase] ?? PHASE_FALLBACK_COLOR }}
              />
            )}
            <span className="truncate text-sm">{node.name}</span>
            {isLeaf(node) && node.role_id && (
              <AssigneeBadge member={memberById.get(node.role_id)} />
            )}
            <span className="shrink-0 text-xs text-slate-400">
              {Math.round(hours.get(node.id) ?? 0)} h
            </span>
          </span>
          <IconButton
            size="small"
            aria-label={`Edit ${node.name}`}
            onClick={(e) => {
              e.stopPropagation(); // don't toggle the row's expansion
              setSelectedId(node.id);
            }}
          >
            <EditIcon fontSize="small" />
          </IconButton>
        </div>
      }
    >
      {node.children.map(renderItem)}
    </TreeItem>
  );

  const targets = selected ? moveTargets(tree, selected.id) : [];
  // Eligible predecessors for the selected node — same kind, no self, no cycle (see dependencyTargets).
  // Memoized: dependencyTargets walks the whole tree + runs a BFS, so recomputing it on every render
  // (each keystroke in an open task modal produces a new `tree`) caused visible typing lag at scale.
  const dependsOnOptions = useMemo(
    () => (selectedId ? dependencyTargets(tree, selectedId) : []),
    [tree, selectedId],
  );

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" onClick={addPackage} className="btn-secondary text-sm">
          + Add work package
        </button>
        <button type="button" onClick={addTopTask} className="btn-secondary text-sm">
          + Add task
        </button>
        <span className="text-xs muted">Click the ✎ on a row to edit it.</span>
      </div>

      {tree.length === 0 ? (
        <p className="rounded-lg border border-dashed border-slate-300 p-6 text-center text-sm muted">
          Add a work package or task to begin.
        </p>
      ) : (
        <div className="rounded-lg border border-slate-200 p-2">
          <SimpleTreeView
            expandedItems={expanded}
            onExpandedItemsChange={(_e, ids) => setExpanded(ids)}
          >
            {tree.map(renderItem)}
          </SimpleTreeView>
        </div>
      )}

      <Modal open={selected !== null} onClose={() => setSelectedId(null)} title={selected?.name ?? ""}>
        {selected && (
          <div className="space-y-4">
            <label className="block">
              <span className="label">Name</span>
              <input
                className="input"
                value={selected.name}
                onChange={(e) => patch(selected.id, { name: e.target.value })}
              />
            </label>

            {isLeaf(selected) ? (
              <LeafFields
                leaf={selected}
                members={members}
                defaultRole={defaultRole}
                dependsOnOptions={dependsOnOptions}
                patch={patch}
              />
            ) : (
              <BranchFields
                branch={selected}
                hours={hours}
                memberById={memberById}
                dependsOnOptions={dependsOnOptions}
                onSelect={setSelectedId}
                onAddTask={addTaskTo}
                patch={patch}
              />
            )}

            <div className="flex flex-wrap items-end justify-between gap-2 border-t border-slate-100 pt-3">
              <label className="block text-xs">
                <span className="muted">Move to</span>
                <select
                  className="input mt-0.5 max-w-[14rem] py-1 text-sm"
                  value=""
                  // "Top level" needs a sentinel distinct from the placeholder's value=""; sharing
                  // "" with the controlled value means selecting it never fires onChange.
                  onChange={(e) =>
                    e.target.value && move(selected.id, e.target.value === "__top__" ? null : e.target.value)
                  }
                >
                  <option value="" disabled>
                    Move to…
                  </option>
                  <option value="__top__">Top level</option>
                  {targets.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                onClick={() => {
                  if (
                    isLeaf(selected) ||
                    window.confirm(
                      `Delete "${selected.name}" and its ${countLeaves(selected.children)} task(s)?`,
                    )
                  ) {
                    remove(selected.id);
                  }
                }}
                className="rounded-md px-3 py-1.5 text-sm text-rose-600 hover:bg-rose-50"
              >
                Delete
              </button>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}
