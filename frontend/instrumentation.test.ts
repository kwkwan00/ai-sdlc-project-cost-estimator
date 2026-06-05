import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { register } from "./instrumentation";

describe("instrumentation.register", () => {
  let logSpy: ReturnType<typeof vi.spyOn>;
  const origEnv = { ...process.env };

  beforeEach(() => {
    logSpy = vi.spyOn(console, "log").mockImplementation(() => {});
  });

  afterEach(() => {
    logSpy.mockRestore();
    process.env = { ...origEnv };
  });

  it("logs a ready message including host:port and API URL when running on the Node runtime", async () => {
    process.env.NEXT_RUNTIME = "nodejs";
    process.env.HOSTNAME = "0.0.0.0";
    process.env.PORT = "3000";
    process.env.NEXT_PUBLIC_API_URL = "http://localhost:8000";

    await register();

    expect(logSpy).toHaveBeenCalledOnce();
    const msg = logSpy.mock.calls[0][0] as string;
    expect(msg).toContain("Frontend ready");
    expect(msg).toContain("http://0.0.0.0:3000");
    expect(msg).toContain("http://localhost:8000");
  });

  it("logs nothing when running on a non-Node runtime (e.g. edge)", async () => {
    process.env.NEXT_RUNTIME = "edge";
    await register();
    expect(logSpy).not.toHaveBeenCalled();
  });

  it("falls back to sensible defaults when env vars are missing", async () => {
    process.env.NEXT_RUNTIME = "nodejs";
    delete process.env.HOSTNAME;
    delete process.env.PORT;
    delete process.env.NEXT_PUBLIC_API_URL;

    await register();

    const msg = logSpy.mock.calls[0][0] as string;
    expect(msg).toContain("http://localhost:3000");
    expect(msg).toContain("(unset)");
  });
});
