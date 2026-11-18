import { NextRequest, NextResponse } from "next/server";

function buildContentSecurityPolicy(nonce: string): string {
  const isDev = process.env.NODE_ENV === "development";
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL ?? "";

  return [
    "default-src 'self'",
    "base-uri 'self'",
    "frame-ancestors 'none'",
    "object-src 'none'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic' https://challenges.cloudflare.com https://www.youtube.com${isDev ? " 'unsafe-eval'" : ""}`,
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "img-src 'self' data: blob: https:",
    // Locally generated round-timer bell tracks, including background playback;
    // and the project's own storage, for 60-second links to rebuilt voice notes
    // and form-check videos (only after the person presses play).
    `media-src 'self' blob:${supabaseUrl ? ` ${supabaseUrl}` : ""}`,
    "font-src 'self' data: https://fonts.gstatic.com",
    // Next's route transitions also fetch the existing Google Fonts stylesheet
    // through connect-src, rather than only through the initial <link>.
    `connect-src 'self' https://fonts.googleapis.com https://challenges.cloudflare.com https://*.supabase.co https://*.sentry.io https://*.ingest.sentry.io${supabaseUrl ? ` ${supabaseUrl}` : ""}`,
    // Exercise demo videos: the IFrame Player API script (above) and its
    // privacy-enhanced player frame, mounted only after the athlete taps play.
    "frame-src https://challenges.cloudflare.com https://www.youtube-nocookie.com https://www.youtube.com",
    "worker-src 'self' blob:",
    "manifest-src 'self'",
    "form-action 'self'",
    "upgrade-insecure-requests",
  ].join("; ");
}

export function proxy(request: NextRequest): NextResponse {
  const nonce = btoa(crypto.randomUUID());
  const csp = buildContentSecurityPolicy(nonce);

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  // Expose the resolved pathname to the server layout so it can commit the
  // brand/auth shell surface on the SSR <html> (no workspace-chrome flash).
  requestHeaders.set("x-pathname", request.nextUrl.pathname);
  requestHeaders.set("Content-Security-Policy", csp);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  matcher: [
    {
      source: "/((?!api|_next/static|_next/image|favicon.ico|icon.svg|manifest.webmanifest|.*\\..*).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
