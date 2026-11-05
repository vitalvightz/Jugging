import path from "node:path";
import type { NextConfig } from "next";
import { withSentryConfig } from "@sentry/nextjs";
import createNextIntlPlugin from "next-intl/plugin";

const withNextIntl = createNextIntlPlugin("./i18n/request.ts");

// The Next app lives in `web/` but imports the shared Stage 2 policy from
// `../shared/stage2-policy.json` (the single source of truth shared with the
// Python backend). Turbopack otherwise infers the workspace root as `web/` and
// refuses to resolve modules above it; pinning the root to the repo root lets
// that cross-package import resolve. `next build` runs with the cwd set to
// `web/` (CI working-directory and the Vercel project root), so the parent dir
// is the repo root.
const WORKSPACE_ROOT = path.resolve(process.cwd(), "..");

const LOCAL_API_BASE_URL = "http://127.0.0.1:8000";
const MISSING_PRODUCTION_REWRITE_ERROR =
  "NEXT_PUBLIC_API_BASE_URL must be set for production builds so /api rewrites are always configured.";

function resolveBackendUrl(): string | null {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL?.trim().replace(/\/$/, "");
  if (configured) {
    return configured;
  }

  if (process.env.NODE_ENV !== "production") {
    return LOCAL_API_BASE_URL;
  }

  return null;
}

// Content-Security-Policy is set per request in proxy.ts so each response can
// carry a fresh nonce for Next's inline bootstrap and hydration scripts.
const SECURITY_HEADERS = [
  // Force HTTPS for a year, including subdomains. `preload` is intentionally
  // omitted until every subdomain is confirmed HTTPS-only — add it only when
  // ready to submit to the HSTS preload list (the decision is hard to reverse).
  {
    key: "Strict-Transport-Security",
    value: "max-age=31536000; includeSubDomains",
  },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  {
    key: "Permissions-Policy",
    value: "camera=(), microphone=(), geolocation=()",
  },
  // Sever the opener relationship with any cross-origin window. Safe here: the
  // app opens no auth popups — Supabase email/password, magic links and recovery
  // links are all top-level navigations — and COOP does not apply to iframes, so
  // the Cloudflare Turnstile widget is unaffected.
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  // Stop other origins loading our responses as subresources. Nothing external
  // embeds them (no Open Graph images, no third-party embeds) and framing is
  // already denied by X-Frame-Options + frame-ancestors.
  { key: "Cross-Origin-Resource-Policy", value: "same-origin" },
  // Cross-Origin-Embedder-Policy is deliberately NOT set: `require-corp` would
  // demand CORP/CORS opt-in from challenges.cloudflare.com and fonts.gstatic.com,
  // and nothing here needs cross-origin isolation.
];

const SERVICE_WORKER_HEADERS = [
  {
    key: "Content-Type",
    value: "application/javascript; charset=utf-8",
  },
  {
    key: "Cache-Control",
    value: "no-cache, no-store, must-revalidate",
  },
  {
    key: "Service-Worker-Allowed",
    value: "/",
  },
  {
    key: "Content-Security-Policy",
    value: "default-src 'self'; base-uri 'none'; connect-src 'self'; object-src 'none'; script-src 'self'",
  },
];

const nextConfig: NextConfig = {
  // Suppress `X-Powered-By: Next.js`. It advertises the framework to anyone
  // scanning for version-specific exploits and buys nothing in return.
  poweredByHeader: false,
  turbopack: {
    root: WORKSPACE_ROOT,
  },
  experimental: {
    // The root layout is dynamic (per-request CSP nonce), so by default every
    // client navigation waited for a fresh RSC payload from the server before
    // the next screen could even mount: one phone → Vercel (iad1) round trip
    // per tab tap. Workspace pages are client components that read their own
    // data from /api on every visit, so a visited route's payload carries no
    // user data and can be reused. Data freshness is unchanged; server actions
    // and router.refresh() still clear this cache.
    staleTimes: { dynamic: 300, static: 300 },
  },
  async rewrites() {
    const backendUrl = resolveBackendUrl();
    if (!backendUrl) {
      throw new Error(MISSING_PRODUCTION_REWRITE_ERROR);
    }

    return [
      {
        source: "/api/health",
        destination: `${backendUrl}/health`,
      },
      {
        source: "/api/:path*",
        destination: `${backendUrl}/api/:path*`,
      },
    ];
  },
  async headers() {
    return [
      {
        source: "/sw.js",
        headers: SERVICE_WORKER_HEADERS,
      },
      {
        source: "/:path*",
        headers: SECURITY_HEADERS,
      },
    ];
  },
};

export default withSentryConfig(withNextIntl(nextConfig), {
  org: process.env.SENTRY_ORG ?? "unlxck",
  project: process.env.SENTRY_PROJECT ?? "javascript-nextjs",
  authToken: process.env.SENTRY_AUTH_TOKEN,
  widenClientFileUpload: true,
  silent: !process.env.CI,
});
