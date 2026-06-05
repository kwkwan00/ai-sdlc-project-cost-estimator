import Link from "next/link";

export default function Dashboard() {
  return (
    <div className="space-y-8">
      <div className="space-y-2">
        <h1 className="text-3xl font-bold text-slate-900">Cost estimates</h1>
        <p className="text-slate-600 max-w-2xl">
          Start a new estimate to size a software project across the six SDLC
          phases — Discovery, UX/Design, Development, Code Review, Deployment, and
          QA/Testing — using six collaborative AI twins.
        </p>
      </div>

      <div className="flex items-center gap-3">
        <Link href="/estimate/new" className="btn-primary">
          New estimate
        </Link>
        <Link href="/estimate/new?quick=1" className="btn-secondary">
          Quick estimate (skip Stages 2 + 3)
        </Link>
      </div>

      <section className="card">
        <h2 className="section-title">Recent estimates</h2>
        <p className="muted mt-1">
          Recent estimate history (post-MVP). Once persistence is wired up, your
          past estimates will appear here.
        </p>
      </section>
    </div>
  );
}
