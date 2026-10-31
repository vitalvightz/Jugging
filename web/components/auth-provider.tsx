"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState, type Dispatch, type ReactNode, type SetStateAction } from "react";

import { usePathname } from "next/navigation";
import { ApiError, trackProductEvent, getMe } from "@/lib/api";
import { clearSharedReads } from "@/lib/shared-reads";
import { getSupabaseBrowserClient } from "@/lib/supabase";
import { hasSignupAdmission } from "@/lib/signup-invite-guard";
import { APPEARANCE_STORAGE_KEY, type AppearanceMode, type MeResponse } from "@/lib/types";

const ME_RETRY_ATTEMPTS = 3;
const ME_RETRY_DELAY_MS = 1_200;

async function sleep(ms: number): Promise<void> {
  await new Promise((resolve) => globalThis.setTimeout(resolve, ms));
}

type AppSession = {
  access_token: string;
  email?: string | null;
  user_id?: string | null;
};

type LoadMe = (
  nextSession: AppSession | null,
  options?: { allowRefresh?: boolean; force?: boolean },
) => Promise<void>;


type AppSessionValue = {
  isReady: boolean;
  isMeHydrated: boolean;
  hasTransientMeError: boolean;
  session: AppSession | null;
  me: MeResponse | null;
  previewAppearanceMode: Dispatch<SetStateAction<AppearanceMode | null>>;
  refreshMe: () => Promise<void>;
  replaceMe: (nextMe: MeResponse | null) => void;
  signOut: () => Promise<void>;
};

// Exported so component tests can mount a subtree that consumes the session
// without standing up the whole auth provider (and its network calls).
export const AppSessionContext = createContext<AppSessionValue | undefined>(undefined);

function applyAppearanceMode(mode: AppearanceMode) {
  if (typeof document === "undefined") {
    return;
  }
  document.documentElement.dataset.theme = mode;
  document.documentElement.style.colorScheme = mode;
  // Persist so the pre-paint script in the document head can restore this theme
  // on the next load before React hydrates — otherwise a light-theme user sees a
  // black flash while the dark SSR default is swapped out.
  try {
    window.localStorage.setItem(APPEARANCE_STORAGE_KEY, mode);
  } catch {
    // Ignore storage failures (private mode, disabled storage) — the theme still
    // applies for this session; only the next-load flash prevention is lost.
  }
}

export function AuthProvider({ children }: Readonly<{ children: ReactNode }>) {
  const pathname = usePathname();
  const [isReady, setIsReady] = useState(false);
  const [isMeHydrated, setIsMeHydrated] = useState(false);
  const [session, setSession] = useState<AppSession | null>(null);
  const [me, setMe] = useState<MeResponse | null>(null);
  useEffect(() => {
    if (!session?.access_token || me?.profile.role !== "athlete") return;
    const token = session.access_token;
    const recordVisit = () => {
      if (document.visibilityState === "visible") void trackProductEvent(token, "app_opened", "onboarding");
    };
    recordVisit();
    document.addEventListener("visibilitychange", recordVisit);
    return () => document.removeEventListener("visibilitychange", recordVisit);
  }, [session?.access_token, me?.profile.role]);
  useEffect(() => {
    if (!session?.access_token || me?.profile.role !== "athlete") return;
    // Only categorical names leave the browser. Never send URLs, queries or IDs.
    const section = pathname?.split("/")[1] ?? "";
    const features: Record<string, string> = { plan: "plans", plans: "plans", today: "recovery", timer: "timer", history: "history", nutrition: "nutrition", strength: "strength", sparring: "sparring" };
    void trackProductEvent(session.access_token, "page_viewed", features[section] ?? "onboarding");
  }, [pathname, session?.access_token, me?.profile.role]);
  const [hasTransientMeError, setHasTransientMeError] = useState(false);
  const [appearancePreview, setAppearancePreview] = useState<AppearanceMode | null>(null);
  const handledAccessTokenRef = useRef<string | null>(null);
  const hydratedAccessTokenRef = useRef<string | null>(null);
  const loadGenerationRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const latestMeRef = useRef<MeResponse | null>(me);
  const loadMeRef = useRef<LoadMe | null>(null);

  useEffect(() => {
    latestMeRef.current = me;
  }, [me]);

  const clearRetryTimer = useCallback(() => {
    if (retryTimerRef.current !== null) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
  }, []);

  useEffect(() => {
    return () => {
      clearRetryTimer();
    };
  }, [clearRetryTimer]);

  const loadMe = useCallback<LoadMe>(async (nextSession, options = {}) => {
    const allowRefresh = options.allowRefresh ?? true;
    const force = options.force ?? false;
    const currentLoadId = loadGenerationRef.current + 1;
    loadGenerationRef.current = currentLoadId;
    let shouldHoldHydration = false;
    let admissionChecked = false;

    if (!nextSession?.access_token) {
      if (loadGenerationRef.current === currentLoadId) {
        setSession(null);
        clearRetryTimer();
        setHasTransientMeError(false);
        setAppearancePreview(null);
        setMe(null);
        hydratedAccessTokenRef.current = null;
        setIsMeHydrated(true);
        setIsReady(true);
      }
      return;
    }

    // Only pause the workspace when there is no profile for this user yet.
    const hasCurrentProfile = latestMeRef.current?.profile.athlete_id === nextSession.user_id;
    if (!hasCurrentProfile) setIsReady(false);
    try {
      const admitted = await hasSignupAdmission(nextSession.user_id);
      admissionChecked = true;
      if (!admitted) {
        if (loadGenerationRef.current !== currentLoadId) return;
        clearRetryTimer();
        setSession(null);
        setMe(null);
        hydratedAccessTokenRef.current = null;
        setIsMeHydrated(true);
        try { await getSupabaseBrowserClient().auth.signOut(); } catch { /* Redirect regardless. */ }
        window.location.assign("/signup?invite=required");
        return;
      }
      if (loadGenerationRef.current !== currentLoadId) return;
      setSession(nextSession);
      setIsReady(true);

      if (!force && hydratedAccessTokenRef.current === nextSession.access_token && latestMeRef.current) {
        setIsMeHydrated(true);
        return;
      }
      if (hydratedAccessTokenRef.current !== nextSession.access_token && !hasCurrentProfile) setMe(null);
      setIsMeHydrated(false);

      let nextMe: MeResponse | null = null;
      let lastError: unknown = null;

      for (let attempt = 1; attempt <= ME_RETRY_ATTEMPTS; attempt += 1) {
        try {
          nextMe = await getMe(nextSession.access_token);
          break;
        } catch (err) {
          lastError = err;
          if (
            err instanceof ApiError &&
            err.status === 401
          ) {
            throw err;
          }
          if (attempt < ME_RETRY_ATTEMPTS) {
            await sleep(ME_RETRY_DELAY_MS * attempt);
          }
        }
      }

      if (nextMe === null && lastError) {
        throw lastError;
      }
      if (loadGenerationRef.current !== currentLoadId) {
        return;
      }
      clearRetryTimer();
      setHasTransientMeError(false);
      setMe(nextMe);
      // Set now, not after the render commits: the auth-change and getSession
      // paths both load at startup, and the later one must see this result.
      latestMeRef.current = nextMe;
      hydratedAccessTokenRef.current = nextSession.access_token;
      setSession(nextSession);
    } catch (err) {
      if (loadGenerationRef.current !== currentLoadId) {
        return;
      }

      if (err instanceof ApiError && err.status === 401 && allowRefresh) {
        try {
          const client = getSupabaseBrowserClient();
          const refreshResult = await client.auth.refreshSession();
          const refreshedAccessToken = refreshResult.data.session?.access_token ?? null;
          if (refreshedAccessToken) {
            const refreshedSession = {
              access_token: refreshedAccessToken,
              email: refreshResult.data.session?.user.email ?? nextSession.email ?? null,
              user_id: refreshResult.data.session?.user.id ?? nextSession.user_id ?? null,
            };
            handledAccessTokenRef.current = refreshedAccessToken;
            await loadMeRef.current?.(refreshedSession, { allowRefresh: false, force });
            return;
          }
        } catch {
          // Treat refresh failures as a genuine session expiry below only when no session remains.
        }

        try {
          const client = getSupabaseBrowserClient();
          const currentSession = await client.auth.getSession();
          const liveAccessToken = currentSession.data.session?.access_token ?? null;
          if (liveAccessToken) {
            const liveSession = {
              access_token: liveAccessToken,
              email: currentSession.data.session?.user.email ?? nextSession.email ?? null,
              user_id: currentSession.data.session?.user.id ?? nextSession.user_id ?? null,
            };
            setHasTransientMeError(true);
            setIsMeHydrated(false);
            shouldHoldHydration = true;
            setSession(liveSession);
            clearRetryTimer();
            retryTimerRef.current = setTimeout(() => {
              retryTimerRef.current = null;
              void loadMeRef.current?.(liveSession, { allowRefresh: false, force });
            }, ME_RETRY_DELAY_MS);
            return;
          }
        } catch {
          // If we cannot confirm auth state, keep the current session and retry later.
          setHasTransientMeError(true);
          setIsMeHydrated(false);
          shouldHoldHydration = true;
          clearRetryTimer();
          retryTimerRef.current = setTimeout(() => {
            retryTimerRef.current = null;
            void loadMeRef.current?.(nextSession, { allowRefresh: false, force });
          }, ME_RETRY_DELAY_MS);
          return;
        }
      }

      if (err instanceof ApiError && err.status === 401) {
        clearRetryTimer();
        setHasTransientMeError(false);
        setAppearancePreview(null);
        setSession(null);
        setMe(null);
        hydratedAccessTokenRef.current = null;
        return;
      }

      setHasTransientMeError(true);
      setIsMeHydrated(false);
      shouldHoldHydration = true;
      clearRetryTimer();
      retryTimerRef.current = setTimeout(() => {
        retryTimerRef.current = null;
        void loadMeRef.current?.(nextSession, { allowRefresh: false, force });
      }, ME_RETRY_DELAY_MS);
    } finally {
      if (loadGenerationRef.current === currentLoadId) {
        setIsReady(admissionChecked || hasCurrentProfile);
        if (!shouldHoldHydration) {
          setIsMeHydrated(true);
        }
      }
    }
  }, [clearRetryTimer]);
  useEffect(() => {
    loadMeRef.current = loadMe;
  }, [loadMe]);

  useEffect(() => {
    let active = true;
    let subscription: { unsubscribe: () => void } | null = null;

    let client;
    try {
      client = getSupabaseBrowserClient();
    } catch {
      setIsReady(true);
      setIsMeHydrated(true);
      return () => {
        active = false;
      };
    }

    client.auth
      .getSession()
      .then(({ data }) => {
        if (!active) {
          return;
        }
        const nextSession = data.session
          ? {
              access_token: data.session.access_token,
              email: data.session.user.email ?? null,
              user_id: data.session.user.id ?? null,
            }
          : null;
        handledAccessTokenRef.current = nextSession?.access_token ?? null;
        void loadMe(nextSession);
      })
      .catch(async () => {
        if (!active) {
          return;
        }
        try {
          await client.auth.signOut();
        } catch {
          // Ignore cleanup failures after a stale browser auth session.
        }
        handledAccessTokenRef.current = null;
        clearRetryTimer();
        setHasTransientMeError(false);
        setAppearancePreview(null);
        setSession(null);
        setMe(null);
        hydratedAccessTokenRef.current = null;
        setIsMeHydrated(true);
        setIsReady(true);
      });

    const authState = client.auth.onAuthStateChange((_event, nextSession) => {
      if (!active) {
        return;
      }
      const mappedSession = nextSession
        ? {
            access_token: nextSession.access_token,
            email: nextSession.user.email ?? null,
            user_id: nextSession.user.id ?? null,
          }
        : null;
      const nextToken = mappedSession?.access_token ?? null;
      if (handledAccessTokenRef.current === nextToken) {
        return;
      }
      handledAccessTokenRef.current = nextToken;
      // Reads are keyed by token already; this just drops the old session's.
      clearSharedReads();
      void loadMe(mappedSession);
    });
    subscription = authState.data.subscription;

    return () => {
      active = false;
      subscription?.unsubscribe();
    };
  }, [clearRetryTimer, loadMe]);

  useEffect(() => {
    if (!isReady) {
      return;
    }
    if (appearancePreview) {
      applyAppearanceMode(appearancePreview);
      return;
    }
    // An authenticated session can be ready before /me (and its appearance_mode)
    // has loaded. Forcing "dark" here would overwrite the theme the pre-paint
    // script already restored from localStorage and cause a flash, so hold the
    // current theme until the profile arrives.
    if (session && !me) {
      return;
    }
    applyAppearanceMode(session && me?.profile.appearance_mode === "light" ? "light" : "dark");
  }, [appearancePreview, isReady, session, me]);

  async function refreshMe() {
    await loadMe(session, { force: true });
  }

  function replaceMe(nextMe: MeResponse | null) {
    setMe(nextMe);
    hydratedAccessTokenRef.current = nextMe && session?.access_token ? session.access_token : null;
    setIsMeHydrated(true);
  }

  async function signOut() {
    try {
      await getSupabaseBrowserClient().auth.signOut();
    } catch {
      // Ignore missing client during sign-out cleanup.
    }
    handledAccessTokenRef.current = null;
    hydratedAccessTokenRef.current = null;
    clearSharedReads();
    clearRetryTimer();
    setHasTransientMeError(false);
    setAppearancePreview(null);
    setSession(null);
    setMe(null);
    setIsReady(true);
    setIsMeHydrated(true);
    applyAppearanceMode("dark");
  }

  return (
    <AppSessionContext.Provider
      value={{
        isReady,
        isMeHydrated,
        hasTransientMeError,
        session,
        me,
        previewAppearanceMode: setAppearancePreview,
        refreshMe,
        replaceMe,
        signOut,
      }}
    >
      {children}
    </AppSessionContext.Provider>
  );
}

export function useAppSession() {
  const context = useContext(AppSessionContext);
  if (!context) {
    throw new Error("useAppSession must be used within AuthProvider.");
  }
  return context;
}
