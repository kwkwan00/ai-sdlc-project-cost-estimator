/** Shared example projects used to prefill the project-description field on the
 *  new-estimate (Stage 1) and new-WBS pages. Kept here so both entry points share
 *  one list instead of duplicating ~120 lines. */

export const HEALTHCARE_EXAMPLE = `We need to build a HIPAA-compliant patient portal for a regional clinic. Patients should be able to view their lab results, schedule appointments, message their provider, request prescription refills, and view billing. Clinic staff need an admin view to manage appointment availability and review messages.

Estimated 25 screens covering 4 user roles (patient, provider, billing admin, scheduler). Integrations: Epic EHR (FHIR), Stripe billing, Twilio SMS for reminders. The clinic already uses Okta for SSO. They want responsive web (no mobile app initially).`;

export type ProjectSize = "Tiny" | "Small" | "Medium" | "Large";

export interface ExampleProject {
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
export const SIZE_ORDER: ProjectSize[] = ["Tiny", "Small", "Medium", "Large"];

// A spread of industries and scopes so users can compare estimate sizes.
export const EXAMPLES: ExampleProject[] = [
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
