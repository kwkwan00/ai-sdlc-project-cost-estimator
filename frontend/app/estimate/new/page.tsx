"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { useForm } from "react-hook-form";

import { prefillFromDescription } from "@/lib/api-client";
import { stage1Schema, type Stage1Input } from "@/lib/schemas";
import { loadDraft, saveDraft } from "@/lib/wizard-store";

const HEALTHCARE_EXAMPLE = `We need to build a HIPAA-compliant patient portal for a regional clinic. Patients should be able to view their lab results, schedule appointments, message their provider, request prescription refills, and view billing. Clinic staff need an admin view to manage appointment availability and review messages.

Estimated 25 screens covering 4 user roles (patient, provider, billing admin, scheduler). Integrations: Epic EHR (FHIR), Stripe billing, Twilio SMS for reminders. The clinic already uses Okta for SSO. They want responsive web (no mobile app initially).`;

type ProjectSize = "Tiny" | "Small" | "Medium" | "Large";

interface ExampleProject {
  /** Project name pre-filled on the form. */
  name: string;
  /** Dropdown option label (industry — short name). */
  label: string;
  /** Size tier — groups the dropdown options. */
  size: ProjectSize;
  /** Full plain-English description pasted into the textarea. */
  description: string;
}

// Order of the dropdown's option groups (smallest scope first).
const SIZE_ORDER: ProjectSize[] = ["Tiny", "Small", "Medium", "Large"];

// A spread of industries and scopes so users can compare estimate sizes.
const EXAMPLES: ExampleProject[] = [
  {
    size: "Tiny",
    label: "Marketing — landing + waitlist",
    name: "Marketing site & waitlist",
    description: `A marketing website for an early-stage startup: a landing page, a features page, a pricing page, an about page, and a simple blog. Visitors can join a waitlist by submitting their email.

About 6 mostly-static pages plus one waitlist form and a basic admin list to view and export signups. No visitor login. Integrations: Mailchimp for the email list and Plausible for analytics. Responsive web, greenfield, no regulatory requirements.`,
  },
  {
    size: "Tiny",
    label: "Nonprofit — donation page",
    name: "Nonprofit donation page",
    description: `A donation website for a local animal-rescue nonprofit: a homepage telling their story, a programs/impact page, a one-time and recurring donation form, and a thank-you flow. A basic admin lets staff see recent donations and export them.

About 5 pages plus the donation form and a minimal admin. No donor login. Integrations: Stripe for payments and Mailchimp for the newsletter. Responsive web, greenfield.`,
  },
  {
    size: "Small",
    label: "Internal — expense approvals",
    name: "Expense approval tool",
    description: `A lightweight internal web tool for a 40-person company to submit and approve expense reports. Employees enter expense lines and upload receipt photos; managers get an approval queue and approve or reject with comments; finance exports approved expenses to CSV.

About 6 screens across 3 roles (employee, manager, finance). Integrations: Google Workspace SSO and SendGrid for email notifications. No payments — reimbursement happens outside the tool. Internal desktop web, greenfield.`,
  },
  {
    size: "Small",
    label: "Retail — coffee storefront",
    name: "Coffee storefront",
    description: `An online storefront for a small specialty coffee roaster. Shoppers browse products, read product detail pages, add items to a cart, and check out as a guest or with an account; they can view order history and track shipping. An admin manages the catalog, inventory, and orders.

About 12 screens across 2 roles (shopper, store admin). Integrations: Stripe for checkout, Shippo for shipping rates/labels, and Klaviyo for marketing emails. Responsive web, greenfield.`,
  },
  {
    size: "Small",
    label: "Community — discussion forum",
    name: "Community forum",
    description: `A discussion forum for a hobbyist community. Members sign up, create threads in categories, post replies, upvote, and follow topics; moderators can pin, lock, and remove posts and handle reports. Search spans all threads.

About 12 screens across 3 roles (member, moderator, admin). Integrations: email/password plus Google login, SendGrid for notifications, and S3-compatible storage for image uploads. Responsive web, greenfield.`,
  },
  {
    size: "Medium",
    label: "Beauty — salon booking SaaS",
    name: "Salon booking MVP",
    description: `An MVP for a small appointment-booking SaaS aimed at independent salons. Owners configure their services, hours, and staff; clients browse availability and book or cancel appointments; the system sends reminders. Owners get a calendar view and a simple dashboard of upcoming bookings.

About 10 screens across 2 roles (salon owner, client). Integrations: Stripe for booking deposits, Twilio for SMS reminders, and Google Calendar sync. Responsive web, greenfield, no existing system to integrate with.`,
  },
  {
    size: "Medium",
    label: "EdTech — learning platform",
    name: "Learning platform",
    description: `A learning platform for a vocational training company. Instructors build courses with modules, lessons (video + text), and auto-graded quizzes; students enroll, work through lessons, track progress, and earn completion certificates. Admins manage users, cohorts, and reporting.

About 20 screens across 3 roles (student, instructor, admin). Integrations: Stripe for course purchases, Mux for video hosting, Google and Microsoft SSO, and SCORM export. Responsive web; a brownfield rebuild replacing an aging internal tool.`,
  },
  {
    size: "Medium",
    label: "Logistics — delivery dispatch",
    name: "Delivery dispatch dashboard",
    description: `A web dashboard for a regional last-mile delivery company to manage drivers and routes. Dispatchers assign orders to drivers and optimize routes; drivers get a mobile-friendly view of their stops with proof-of-delivery capture; managers see live tracking and daily performance reports.

About 18 screens across 3 roles (dispatcher, driver, manager). Integrations: Google Maps/Routes, Twilio for customer SMS, and a webhook feed from the existing order system. Responsive web plus a driver PWA; brownfield (integrates with an existing orders database).`,
  },
  {
    size: "Medium",
    label: "PropTech — listings marketplace",
    name: "Real-estate marketplace",
    description: `A property-listings marketplace for a mid-size brokerage. Buyers search and filter listings, save favorites, and request viewings; agents create and manage listings with photos and schedule viewings; an admin manages agents and featured listings.

About 18 screens across 3 roles (buyer, agent, admin). Integrations: an MLS data feed, Mapbox for map search, Stripe for featured-listing fees, and SendGrid for lead emails. Responsive web, greenfield.`,
  },
  {
    size: "Large",
    label: "Healthcare — patient portal",
    name: "Healthcare patient portal",
    description: HEALTHCARE_EXAMPLE,
  },
  {
    size: "Large",
    label: "FinTech — KYC + payments",
    name: "Fintech onboarding & payments",
    description: `A customer onboarding and payments platform for a fintech offering business accounts. New customers complete a KYC/KYB flow (identity verification, document upload, risk screening), then open an account, link external banks, and initiate ACH/wire transfers; a back-office team reviews flagged cases and manages compliance.

About 30 screens across 3 roles (customer, compliance reviewer, admin). Regulated: SOC 2 and PCI-DSS with a full audit trail. Integrations: Plaid for bank linking, Persona for identity verification, a payments rail (Stripe Treasury), and Okta SSO for staff. Responsive web, greenfield.`,
  },
];

function Stage1Inner() {
  const router = useRouter();
  const params = useSearchParams();
  const quick = params.get("quick") === "1";

  const { register, handleSubmit, setValue, formState } = useForm<Stage1Input>({
    resolver: zodResolver(stage1Schema),
    defaultValues: { raw_input: "", project_name: "" },
  });

  const [analyzing, setAnalyzing] = useState(false);
  const [prefillNote, setPrefillNote] = useState<string | null>(null);
  const [pick, setPick] = useState("");

  useEffect(() => {
    const draft = loadDraft();
    if (draft) {
      setValue("raw_input", draft.raw_input || "");
      setValue("project_name", draft.project_name || "");
    }
  }, [setValue]);

  const onSubmit = async (values: Stage1Input) => {
    if (quick) {
      // Quick mode bypasses Stage 2/3 entirely — skip the prefill call so we
      // don't burn an LLM round-trip the user will immediately discard.
      saveDraft({
        raw_input: values.raw_input,
        project_name: values.project_name,
      });
      router.push(`/estimate/draft/create?quick=1`);
      return;
    }

    setAnalyzing(true);
    setPrefillNote(null);
    try {
      const prefill = await prefillFromDescription(values.raw_input);
      saveDraft({
        raw_input: values.raw_input,
        project_name: values.project_name,
        // Prefill is roster-free, and we no longer seed placeholder roles — the
        // roster starts empty and is populated by the AG-UI proposal on Stage 2.
        stage2: { ...prefill.stage2, roster: { roles: [] } },
        stage2_prefilled: true,
        prefill_ambiguity: prefill.ambiguity_score,
        prefill_summary: prefill.summary,
        // Carry any AI tools named in the description forward to seed Stage 3.
        prefill_ai_tooling: prefill.ai_tooling_description,
      });
      router.push(`/estimate/draft/context`);
    } catch (e) {
      // Graceful degradation: if the LLM call fails (network, backend down)
      // we save the raw input and continue. Stage 2 will render its defaults.
      saveDraft({
        raw_input: values.raw_input,
        project_name: values.project_name,
      });
      setPrefillNote(
        `Couldn't auto-fill from description (${(e as Error).message}). Continuing with a blank form.`
      );
      setAnalyzing(false);
      // Brief pause so the user sees the message before the route change.
      setTimeout(() => router.push(`/estimate/draft/context`), 1200);
    }
  };

  const applyExample = (ex: ExampleProject) => {
    setValue("raw_input", ex.description);
    setValue("project_name", ex.name);
  };

  return (
    <div className="space-y-6 max-w-3xl">
      <header className="space-y-2">
        <p className="text-xs uppercase tracking-wide muted">Stage 1 of 5</p>
        <h1 className="text-2xl font-bold text-slate-900">
          Describe the project
        </h1>
        <p className="muted">
          Paste sales notes, RFP excerpts, meeting summaries — anything that
          captures the scope. The AI parser will extract structured signals; you
          can review and refine them on the next page.
        </p>
      </header>

      <form onSubmit={handleSubmit(onSubmit)} className="card space-y-5">
        <div>
          <label className="label" htmlFor="project_name">
            Project name <span className="muted">(optional)</span>
          </label>
          <input
            id="project_name"
            type="text"
            className="input mt-1"
            placeholder="e.g. Healthcare patient portal"
            {...register("project_name")}
          />
        </div>

        <div>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <label className="label" htmlFor="raw_input">
              Project description
            </label>
            <select
              aria-label="Prefill an example project"
              value={pick}
              onChange={(e) => {
                const ex = EXAMPLES.find((x) => x.name === e.target.value);
                if (ex) applyExample(ex);
                setPick(""); // action menu — reset so any example can be re-picked
              }}
              className="input max-w-[15rem] py-1 text-sm"
            >
              <option value="">Prefill an example…</option>
              {SIZE_ORDER.map((size) => (
                <optgroup key={size} label={size}>
                  {EXAMPLES.filter((ex) => ex.size === size).map((ex) => (
                    <option key={ex.name} value={ex.name}>
                      {ex.label}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
          </div>
          <textarea
            id="raw_input"
            className="textarea mt-1"
            placeholder="Describe the project in plain English..."
            {...register("raw_input")}
          />
          {formState.errors.raw_input && (
            <p className="help text-rose-600">
              {formState.errors.raw_input.message}
            </p>
          )}
          <p className="help">
            Tip: include user roles, screen estimates, integrations, and
            regulatory requirements if known.
          </p>
        </div>

        {prefillNote && (
          <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-xs text-amber-800">
            {prefillNote}
          </div>
        )}

        <div className="flex items-center justify-between pt-2">
          <p className="text-xs muted">
            {analyzing
              ? "Analyzing description with Claude…"
              : quick
              ? "Quick mode — Stages 2 + 3 will be skipped with defaults."
              : "Next: project context (Stage 2). The description is auto-analyzed to prefill the form."}
          </p>
          <button
            className="btn-primary disabled:opacity-60 disabled:cursor-progress"
            type="submit"
            disabled={analyzing}
          >
            {analyzing ? "Analyzing…" : quick ? "Generate estimate" : "Continue"}
          </button>
        </div>
      </form>
    </div>
  );
}

export default function Stage1Page() {
  return (
    <Suspense fallback={<div className="card max-w-xl">Loading...</div>}>
      <Stage1Inner />
    </Suspense>
  );
}
