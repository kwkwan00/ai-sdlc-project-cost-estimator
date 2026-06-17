"use client";

import { useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

export interface TabItem {
  /** Stable key + URL-safe id fragment for the tab/panel pair. */
  id: string;
  /** Tab button text. */
  label: string;
  /** Optional muted count/badge rendered after the label. */
  badge?: ReactNode;
  /** Panel body shown when this tab is active. */
  content: ReactNode;
}

interface Props {
  tabs: TabItem[];
  /** id of the initially-active tab; defaults to the first. */
  initialId?: string;
}

/** Accessible tab switcher following the WAI-ARIA tabs pattern: a roving-tabindex
 *  `tablist` with Left/Right/Home/End keyboard navigation, and a single mounted
 *  `tabpanel`. Only the active panel renders, so heavy children (recharts fan
 *  charts) aren't all mounted at once. Styling matches the review surface — a
 *  brand-underlined active tab over a slate divider. */
export function Tabs({ tabs, initialId }: Props) {
  const [active, setActive] = useState(initialId ?? tabs[0]?.id);
  const base = useId();
  const btnRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const activeTab = tabs.find((t) => t.id === active) ?? tabs[0];

  const focusTab = (id: string) => {
    setActive(id);
    btnRefs.current[id]?.focus();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const idx = tabs.findIndex((t) => t.id === activeTab?.id);
    if (idx < 0) return;
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      const delta = e.key === "ArrowRight" ? 1 : -1;
      focusTab(tabs[(idx + delta + tabs.length) % tabs.length].id);
    } else if (e.key === "Home") {
      e.preventDefault();
      focusTab(tabs[0].id);
    } else if (e.key === "End") {
      e.preventDefault();
      focusTab(tabs[tabs.length - 1].id);
    }
  };

  return (
    <div>
      <div
        role="tablist"
        aria-label="Estimate sections"
        onKeyDown={onKeyDown}
        className="flex flex-wrap gap-1 border-b border-slate-200"
      >
        {tabs.map((t) => {
          const selected = t.id === activeTab?.id;
          return (
            <button
              key={t.id}
              type="button"
              role="tab"
              ref={(el) => {
                btnRefs.current[t.id] = el;
              }}
              id={`${base}-tab-${t.id}`}
              aria-selected={selected}
              aria-controls={`${base}-panel-${t.id}`}
              tabIndex={selected ? 0 : -1}
              onClick={() => setActive(t.id)}
              className={`-mb-px rounded-t-md border-b-2 px-4 py-2 text-sm font-medium transition focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-400 ${
                selected
                  ? "border-brand-500 text-brand-700"
                  : "border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-700"
              }`}
            >
              {t.label}
              {t.badge != null && (
                <span className="ml-1.5 text-xs text-slate-400">{t.badge}</span>
              )}
            </button>
          );
        })}
      </div>
      {activeTab && (
        <div
          role="tabpanel"
          id={`${base}-panel-${activeTab.id}`}
          aria-labelledby={`${base}-tab-${activeTab.id}`}
          tabIndex={0}
          className="space-y-6 pt-6 focus:outline-none"
        >
          {activeTab.content}
        </div>
      )}
    </div>
  );
}
