import "./globals.css";
import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "AI SDLC Cost Estimator",
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
                  AI SDLC Cost Estimator
                </Link>
                <nav className="text-sm">
                  <Link className="text-slate-600 hover:text-slate-900" href="/">
                    Dashboard
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
