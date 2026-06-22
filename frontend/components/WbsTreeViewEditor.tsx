"use client";

import EditIcon from "@mui/icons-material/Edit";
import IconButton from "@mui/material/IconButton";
import { SimpleTreeView } from "@mui/x-tree-view/SimpleTreeView";
import { TreeItem } from "@mui/x-tree-view/TreeItem";
import { useMemo, useState } from "react";

import { Modal } from "@/components/Modal";
import { ROLE_CATEGORY_LABELS, type CustomRoleInput } from "@/lib/schemas";
import { PHASE_LABELS, type Phase } from "@/lib/types";
import {
  addChild,
  branchIds,
  clampHours,
  countLeaves,
  findNode,
  isLeaf,
  moveNode,
  moveTargets,
  newLeaf,
  newPackage,
  PHASE_ORDER,
  pertMean,
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
  roster: CustomRoleInput[];
  defaultRole: string;
  patch: (id: string, p: Partial<WbsTaskInput>) => void;
}

type HourField = "optimistic" | "most_likely" | "pessimistic";

/** Concise role label for the leaf dropdowns: the role's category (e.g. "Engineering"); falls back
 *  to the free-form description when the category is "other". */
function roleLabel(r: CustomRoleInput): string {
  return r.category !== "other" ? ROLE_CATEGORY_LABELS[r.category] : r.description;
}

/** Edit-modal body for a leaf task: phase, role, 3-point hours, PERT readout, description. */
function LeafFields({ leaf, roster, defaultRole, patch }: LeafFieldsProps) {
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
          <span className="label">Role</span>
          <select
            className="input"
            value={leaf.role_id ?? defaultRole}
            onChange={(e) => patch(leaf.id, { role_id: e.target.value })}
          >
            {roster.map((r) => (
              <option key={r.role_id} value={r.role_id}>
                {roleLabel(r)}
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
    </>
  );
}

interface BranchFieldsProps {
  branch: WbsTaskInput;
  hours: Map<string, number>;
  onSelect: (id: string) => void;
  onAddTask: (parentId: string) => void;
}

/** Edit-modal body for a branch (work package): child summary + drill-in list + add-task. */
function BranchFields({ branch, hours, onSelect, onAddTask }: BranchFieldsProps) {
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
    onChange(addChild(tree, parentId, newLeaf(defaultPhase, defaultRole)));
    setExpanded((e) => (e.includes(parentId) ? e : [...e, parentId]));
  };
  const remove = (id: string) => {
    onChange(removeNode(tree, id));
    setSelectedId(null);
  };
  const move = (id: string, target: string | null) => onChange(moveNode(tree, id, target));

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
                roster={roster}
                defaultRole={defaultRole}
                patch={patch}
              />
            ) : (
              <BranchFields
                branch={selected}
                hours={hours}
                onSelect={setSelectedId}
                onAddTask={addTaskTo}
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
