import assert from "node:assert/strict";

Deno.test("failed Storage removal stays pending and succeeds on the next cleanup", async () => {
  const originalServe = Deno.serve;
  const originalFetch = globalThis.fetch;
  const originalUrl = Deno.env.get("SUPABASE_URL");
  let handler!: (request: Request) => Promise<Response>;
  let pending!: Promise<unknown>;
  const runtime = globalThis as typeof globalThis & { EdgeRuntime?: { waitUntil: (p: Promise<unknown>) => void } };
  const originalRuntime = runtime.EdgeRuntime;
  let failingBucket = "attachment-intake";
  const deleted: string[] = [];
  const acknowledgements: unknown[] = [];
  try {
    Deno.env.set("SUPABASE_URL", "https://storage.example");
    Deno.serve = ((fn: typeof handler) => { handler = fn; }) as unknown as typeof Deno.serve;
    runtime.EdgeRuntime = { waitUntil: p => { pending = p; } };
    globalThis.fetch = ((url: string | URL | Request, init?: RequestInit) => {
      const path = String(url);
      if (path.endsWith("claim_attachment_processing")) return Promise.resolve(Response.json([]));
      if (path.endsWith("attachment_cleanup_candidates")) return Promise.resolve(Response.json([
        { id: "asset", status: "rejected", intake_path: "source.jpg", ready_path: "clean.jpg" },
      ]));
      if (path.includes("/storage/v1/object/")) {
        deleted.push(path);
        return Promise.resolve(new Response("{}", { status: failingBucket && path.endsWith(failingBucket) ? 503 : 200 }));
      }
      if (path.endsWith("ack_attachment_cleanup")) {
        acknowledgements.push(JSON.parse(String(init?.body)));
        return Promise.resolve(Response.json(null));
      }
      throw new Error(`Unexpected request ${path}`);
    }) as typeof fetch;
    await import("./index.ts");
    const request = () => new Request("https://edge.example", { method: "POST",
      headers: { apikey: "test-key", "Content-Type": "application/json" },
      body: JSON.stringify({ asset_id: "00000000-0000-4000-8000-000000000001" }) });
    await handler(request());
    await pending;
    assert.equal(acknowledgements.length, 0);
    failingBucket = "attachment-ready";
    await handler(request());
    await pending;
    assert.equal(acknowledgements.length, 0);
    failingBucket = "";
    await handler(request());
    await pending;
    assert.equal(deleted.length, 5);
    assert.deepEqual(acknowledgements, [{ p_asset_id: "asset", p_all_removed: true }]);
  } finally {
    Deno.serve = originalServe;
    globalThis.fetch = originalFetch;
    runtime.EdgeRuntime = originalRuntime;
    if (originalUrl === undefined) Deno.env.delete("SUPABASE_URL");
    else Deno.env.set("SUPABASE_URL", originalUrl);
  }
});
