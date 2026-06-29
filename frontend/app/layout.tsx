import "./globals.css";
import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "Software Project Estimator",
  description: "Multi-agent cost estimation for AI-heavy software projects.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Providers>
          <div className="min-h-screen flex flex-col">
            <header className="border-b border-slate-200 bg-white">
              <div className="max-w-5xl mx-auto px-6 h-14 flex items-center justify-between">
                <Link href="/" className="font-semibold text-slate-900">
                  Software Project Estimator
                </Link>
                <nav className="flex items-center gap-4 text-sm">
                  <Link
                    href="/"
                    aria-label="Home"
                    title="Home"
                    className="text-slate-500 hover:text-slate-900"
                  >
                    <svg
                      viewBox="0 0 24 24"
                      className="h-5 w-5"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.8"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <path d="M3 10.5 12 3l9 7.5" />
                      <path d="M5 9.75V21h14V9.75" />
                      <path d="M9.5 21v-6h5v6" />
                    </svg>
                  </Link>
                  <Link
                    href="/observability"
                    aria-label="Observability"
                    title="Observability — LLM cost & usage"
                    className="text-slate-500 hover:text-slate-900"
                  >
                    <svg
                      viewBox="0 0 24 24"
                      className="h-5 w-5"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.8"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <path d="M3 3v18h18" />
                      <path d="M8 17v-4" />
                      <path d="M13 17V8" />
                      <path d="M18 17v-7" />
                    </svg>
                  </Link>
                  <Link
                    href="/settings"
                    aria-label="Settings"
                    title="Settings"
                    className="text-slate-500 hover:text-slate-900"
                  >
                    <svg
                      viewBox="0 0 24 24"
                      className="h-5 w-5"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.8"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <circle cx="12" cy="12" r="3" />
                      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
                    </svg>
                  </Link>
                </nav>
              </div>
            </header>
            <main className="flex-1 max-w-5xl mx-auto w-full px-6 py-8">
              {children}
            </main>
            <footer className="border-t border-slate-200 py-4 text-xs text-slate-400">
              <div className="max-w-5xl mx-auto px-6">
                MVP build. See planning outline for full scope.
              </div>
            </footer>
          </div>
        </Providers>
      </body>
    </html>
  );
}
