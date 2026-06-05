/**
 * Next.js instrumentation hook — runs once on server startup (both `next dev`
 * and the standalone `server.js` produced by `output: "standalone"`).
 *
 * Docs: https://nextjs.org/docs/app/guides/instrumentation
 */
export async function register() {
  // Only the Node.js server runtime should log this; edge runtime also calls
  // register() but won't have a stable "listening on" semantics.
  if (process.env.NEXT_RUNTIME !== "nodejs") return;

  const host = process.env.HOSTNAME || "localhost";
  const port = process.env.PORT || "3000";
  const api = process.env.NEXT_PUBLIC_API_URL || "(unset)";

  // eslint-disable-next-line no-console
  console.log(
    `✓ Frontend ready at http://${host}:${port}  (backend API: ${api})`
  );
}
