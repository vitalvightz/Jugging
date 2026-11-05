import type { ReactNode } from "react";
import type { Metadata } from "next";
import type { Viewport } from "next";
import { headers } from "next/headers";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getTranslations } from "next-intl/server";

import { AppNav } from "@/components/app-nav";
import { AuthProvider } from "@/components/auth-provider";
import { ConnectionStatusBanner } from "@/components/connection-status-banner";
import { GenerationStatusShell } from "@/components/generation-status-shell";
import { LanguageSwitcher } from "@/components/language-switcher";
import { PasswordRecoveryRedirect } from "@/components/password-recovery-redirect";
import { PwaRegister } from "@/components/pwa-register";
import { PushNotificationPrompt } from "@/components/push-notification-prompt";
import { InstallPrompt } from "@/components/install-unlxck";
import { RoundTimerProvider } from "@/components/session-timer/round-timer-provider";
import { FadeThroughProvider } from "@/components/fade-through";
import { PageTransition } from "@/components/page-transition";
import { PerfDiagnostics } from "@/components/perf-diagnostics";
import { ToastProvider } from "@/components/toast-provider";
import { XpAwardFeedback } from "@/components/xp-award-feedback";
import { XpProvider } from "@/components/xp-provider";
import { getServerShellSurface } from "@/lib/app-surface";
import { APPEARANCE_STORAGE_KEY } from "@/lib/types";
import "./globals.css";
import "./brand-surface.css";
import "./xp-interface.css";
import "./xp-overview-card.css";
import "./xp-progress-page.css";
import "./plan-display-polish.css";
import "../components/language-switcher.css";
import "../components/session-timer/session-timer.css";
import "./ux-polish.css";
import "./today-session.css";

const THEME_INIT_SCRIPT = `(function(){try{var m=localStorage.getItem(${JSON.stringify(
  APPEARANCE_STORAGE_KEY,
)});if(m==="light"||m==="dark"){var d=document.documentElement;d.dataset.theme=m;d.style.colorScheme=m;}}catch(e){}})();`;

export const dynamic = "force-dynamic";

const metadataBase: Omit<Metadata, "title" | "description"> = {
  applicationName: "UNLXCK",
  manifest: "/manifest.webmanifest",
  icons: {
    icon: [
      { url: "/favicon.ico", type: "image/x-icon" },
      { url: "/icons/favicon-16x16.png", sizes: "16x16", type: "image/png" },
      { url: "/icons/favicon-32x32.png", sizes: "32x32", type: "image/png" },
    ],
    apple: [
      { url: "/icons/apple-touch-icon.png", sizes: "180x180", type: "image/png" },
    ],
  },
  appleWebApp: {
    capable: true,
    title: "UNLXCK",
    statusBarStyle: "black-translucent",
  },
  formatDetection: {
    address: false,
    email: false,
    telephone: false,
  },
};

export async function generateMetadata(): Promise<Metadata> {
  const translate = await getTranslations("Metadata");
  return {
    ...metadataBase,
    title: translate("title"),
    description: translate("description"),
  };
}

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: [
    { media: "(prefers-color-scheme: dark)", color: "#0a0a0b" },
    { media: "(prefers-color-scheme: light)", color: "#faf7f2" },
  ],
};

export default async function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  const requestHeaders = await headers();
  const locale = await getLocale();
  const safety = await getTranslations("Safety");
  const rawNonce = requestHeaders.get("x-nonce");
  const nonce = rawNonce && /^[A-Za-z0-9+/]{48}$/.test(rawNonce) ? rawNonce : undefined;
  const serverSurface = getServerShellSurface(requestHeaders.get("x-pathname"));
  const pwaBuildVersion =
    process.env.VERCEL_GIT_COMMIT_SHA ??
    process.env.VERCEL_DEPLOYMENT_ID ??
    process.env.VERCEL_URL ??
    "local";

  return (
    <html
      lang={locale}
      data-theme="dark"
      data-app-surface={serverSurface ?? undefined}
      style={{ colorScheme: "dark" }}
      suppressHydrationWarning
    >
      <head>
        <script nonce={nonce} suppressHydrationWarning dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Space+Grotesk:wght@300;400;500;600;700&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>
        <NextIntlClientProvider>
          <AuthProvider>
            <XpProvider>
              <PasswordRecoveryRedirect />
              <ToastProvider>
                <XpAwardFeedback />
                {/* Opt-in speed diagnostics (?perf=1 or Settings); renders nothing otherwise. */}
                <PerfDiagnostics />
                <LanguageSwitcher />
                <PwaRegister buildVersion={pwaBuildVersion}>
                  <InstallPrompt />
                  <PushNotificationPrompt />
                  <GenerationStatusShell>
                    <FadeThroughProvider>
                    <RoundTimerProvider>
                    <div className="app-shell">
                      <AppNav />
                      <div className="app-content">
                        <main className="app-main">
                          <ConnectionStatusBanner />
                          <PageTransition>{children}</PageTransition>
                        </main>
                        <footer className="app-safety-footer" role="contentinfo">
                          <span className="app-safety-footer-wide">{safety("footerWide")}</span>
                          <span className="app-safety-footer-tight">{safety("footerTight")}</span>
                        </footer>
                      </div>
                    </div>
                    </RoundTimerProvider>
                    </FadeThroughProvider>
                  </GenerationStatusShell>
                </PwaRegister>
              </ToastProvider>
            </XpProvider>
          </AuthProvider>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
