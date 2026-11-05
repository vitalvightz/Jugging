"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { PlanSwitchDialog } from "@/components/plan-switch-dialog";
import { RequireAuth } from "@/components/auth-guard";
import { LogTraining } from "@/components/log-training";
import { useAppSession } from "@/components/auth-provider";
import { useOnReconnect } from "@/components/connection-status-banner";
import { TrainingLibraryLink } from "@/components/training-library-link";
import actionStyles from "@/components/training-action-tile.module.css";
import {
  canSetActive,
  CurrentCampCard,
  getIntakeSource,
  PlanManageSheet,
  PlanRow,
  CustomDraftRow,
} from "@/components/plans/plan-dashboard";
import styles from "@/components/plans/plans.module.css";
import { Skeleton } from "@/components/skeleton";
import { useToast } from "@/components/toast-provider";
import { ApiError, getActivePlan, getToday, listCustomPlans, listPlans, setActivePlan } from "@/lib/api";
import { describeLoadFailure } from "@/lib/connectivity";
import { readEpoch, recallView, rememberView, SHARED_READ_MAX_AGE_MS } from "@/lib/shared-reads";
import { getOptionLabels, TACTICAL_STYLE_OPTIONS } from "@/lib/intake-options";
import { humanizeIfRawEnum } from "@/lib/plan-labels";
import { requestXpRefresh } from "@/lib/xp-events";
import { formatPlanStatus, getPlanDisplayName } from "@/lib/plan-format";
import {
  type ActivePlanOverlapAction,
  isActivePlanOverlapError,
  isArchivedPlan,
} from "@/lib/plan-active";
import { getPlanReviewReason, isHeldForAdminReviewPlan } from "@/lib/plan-review";
import type { CustomPlan } from "@/lib/api-schema.generated";
import type { PlanSummary } from "@/lib/types";

function getArchivedPlans(plans: PlanSummary[]): PlanSummary[] {
  return plans.filter((plan) => isArchivedPlan(plan.status));
}

function HeldPlansReviewNotice({ plans }: { plans: PlanSummary[] }) {
  const visibleHeldPlans = plans.slice(0, 3);
  const remainingCount = Math.max(0, plans.length - visibleHeldPlans.length);

  return (
    <article className="list-card plans-dashboard-card athlete-motion-slot athlete-motion-status">
      <div className="plans-dashboard-card-header">
        <div className="plans-dashboard-card-copy">
          <p className="kicker">Admin review hold</p>
          <h2>{plans.length === 1 ? "A plan is held for review" : `${plans.length} plans are held for review`}</h2>
          <p className="muted">
            Saved, but held from Overview and Today until an admin approves.
          </p>
        </div>
        <span className="badge">HELD</span>
      </div>

      <div className="plan-history-list plans-history-list">
        {visibleHeldPlans.map((plan) => (
          <div key={plan.plan_id} className="plan-history-row">
            <div className="plan-history-copy">
              <p className="label">{formatPlanStatus(plan.status)}</p>
              <Link href={`/plans/${plan.plan_id}`}>
                <h3 className="plan-card-title">{getPlanDisplayName(plan)}</h3>
              </Link>
              <p className="muted">{getPlanReviewReason(plan)}</p>
            </div>
            <div className="plan-history-meta">
              <Link href={`/plans/${plan.plan_id}?review_required=1`} className="ghost-button">
                Review hold
              </Link>
            </div>
          </div>
        ))}
      </div>

      {remainingCount > 0 ? (
        <p className="muted">
          {remainingCount} more held plan{remainingCount === 1 ? "" : "s"} shown in saved plan history.
        </p>
      ) : null}
    </article>
  );
}

// Loading stand-ins keep the real card and list shapes, so nothing jumps when
// the plans arrive. Each waits 250ms before it shows (loading-reveal): most
// loads finish first, and a card that appears for a split second is a flicker.
const PLANS_SCREEN_KEY = "screen:plans";
type PlansScreenView = { plans: PlanSummary[]; activePlanId: string | null; customDrafts: CustomPlan[]; customError: string | null };

function CampCardSkeleton() {
  return (
    <article className={`${styles.campCard} loading-reveal`} aria-busy="true" aria-label="Loading current camp">
      <div className={styles.campHead}>
        <Skeleton variant="text" width={110} height={12} />
        <Skeleton variant="block" width={72} height={26} style={{ borderRadius: 999 }} />
      </div>
      <Skeleton variant="text" width="48%" height={26} />
      <div className={styles.fightRow}>
        <div style={{ display: "grid", gap: 8 }}>
          <Skeleton variant="text" width={84} height={11} />
          <Skeleton variant="text" width={150} height={18} />
        </div>
        <div style={{ display: "grid", gap: 8, justifyItems: "end" }}>
          <Skeleton variant="text" width={64} height={30} />
          <Skeleton variant="text" width={84} height={11} />
        </div>
      </div>
      <Skeleton variant="text" width="70%" height={14} />
      <Skeleton variant="block" width="100%" height={52} style={{ borderRadius: 12 }} />
      <div className={styles.quietLinks}>
        <Skeleton variant="text" width={96} height={14} />
        <Skeleton variant="text" width={72} height={14} />
      </div>
    </article>
  );
}

function PlanRowSkeleton() {
  return (
    <li>
      <div className={styles.row}>
        <span className={styles.rowText} style={{ gap: 8 }}>
          <Skeleton variant="text" width="45%" height={18} />
          <Skeleton variant="text" width="70%" height={13} />
        </span>
      </div>
    </li>
  );
}

export default function PlansPage() {
  const t = useTranslations("Workspace");
  const router = useRouter();
  const { showToast } = useToast();
  const { me, session } = useAppSession();
  // A revisit shows the plan list this session last read at once, then reads
  // it again. Plan lists are presentation; any write (activate, rename,
  // delete, publish) drops the remembered list (lib/shared-reads.ts).
  const [recalled] = useState(() => session?.access_token
    ? recallView<PlansScreenView>(session.access_token, PLANS_SCREEN_KEY)
    : null);
  const shownRef = useRef(Boolean(recalled));
  const [customDrafts, setCustomDrafts] = useState<CustomPlan[]>(() => recalled?.customDrafts ?? []);
  const [customError, setCustomError] = useState<string | null>(() => recalled?.customError ?? null);
  const [plans, setPlans] = useState<PlanSummary[]>(() => recalled?.plans ?? []);
  const [isLoading, setIsLoading] = useState(!recalled);
  const [error, setError] = useState<string | null>(null);
  const [localPlans, setLocalPlans] = useState<PlanSummary[] | null>(null);
  const [activePlanId, setActivePlanId] = useState<string | null>(() => recalled?.activePlanId ?? null);
  const [isSettingActivePlanId, setIsSettingActivePlanId] = useState<string | null>(null);
  const [overlapConflictPlan, setOverlapConflictPlan] = useState<PlanSummary | null>(null);
  const [isArchiveOpen, setIsArchiveOpen] = useState(false);
  // The plan whose Manage sheet is open, if any.
  const [managedPlanId, setManagedPlanId] = useState<string | null>(null);
  // The active camp's phase, from the Today command; extra, so never awaited.
  const [activePhase, setActivePhase] = useState<{ planId: string; phase: string } | null>(null);
  const latestTokenRef = useRef(session?.access_token);

  useEffect(() => {
    latestTokenRef.current = session?.access_token;
  }, [session?.access_token]);

  const visiblePlans = useMemo(() => {
    const sourcePlans = localPlans ?? plans;
    return [...sourcePlans].sort((left, right) => new Date(right.created_at).getTime() - new Date(left.created_at).getTime());
  }, [localPlans, plans]);
  const explicitActivePlan = activePlanId ? visiblePlans.find((plan) => plan.plan_id === activePlanId) ?? null : null;
  const activePlan = explicitActivePlan && canSetActive(explicitActivePlan) ? explicitActivePlan : null;
  const intakeSource = getIntakeSource(me);
  const heldForReviewPlans = visiblePlans.filter(isHeldForAdminReviewPlan);
  const archivedPlans = getArchivedPlans(visiblePlans);
  const otherSavedPlans = visiblePlans.filter((plan) => plan.plan_id !== activePlan?.plan_id && !isArchivedPlan(plan.status));
  // A published custom plan already has its own shared row and Edit Plan action.
  // Keep unfinished edits visible alongside it; do not list its complete draft twice.
  const unpublishedDrafts = customDrafts.filter((draft) => draft.incomplete ||
    !visiblePlans.some((plan) => plan.custom_plan_id === draft.id));
  const savedRows = [
    ...otherSavedPlans.map((plan) => ({ kind: "published" as const, plan, date: plan.created_at })),
    ...unpublishedDrafts.map((draft) => ({ kind: "draft" as const, draft, date: draft.updated_at })),
  ].sort((left, right) => new Date(right.date).getTime() - new Date(left.date).getTime());
  const managedPlan = managedPlanId ? visiblePlans.find((plan) => plan.plan_id === managedPlanId) ?? null : null;
  const tacticalStyle = getOptionLabels(TACTICAL_STYLE_OPTIONS, me?.profile?.tactical_style ?? [])[0] ?? null;
  const phase = activePlan && activePhase?.planId === activePlan.plan_id ? activePhase.phase : null;

  const loadPlans = useCallback(async () => {
    const token = session?.access_token;
    if (!token) {
      return;
    }
    // A shown list refreshes in place; only an empty screen shows placeholders.
    if (!shownRef.current) setIsLoading(true);
    setError(null);
    const epoch = readEpoch();
    try {
      const [nextPlans, active, custom] = await Promise.all([
        listPlans(token),
        getActivePlan(token).catch((activeError) => {
          if (activeError instanceof ApiError && activeError.status === 404) {
            return null;
          }
          throw activeError;
        }),
        listCustomPlans(token).then((rows) => {
          if (!Array.isArray(rows)) throw new Error("Unable to load custom drafts.");
          return { rows, error: null };
        }).catch((reason: unknown) => ({
          rows: [] as CustomPlan[],
          error: reason instanceof Error ? reason.message : "Unable to load custom drafts.",
        })),
      ]);
      // Ignore results from a request that the current session has moved past.
      if (latestTokenRef.current !== token) {
        return;
      }
      setPlans(nextPlans);
      setCustomDrafts(custom.rows);
      setCustomError(custom.error);
      setActivePlanId(active?.plan_id ?? null);
      shownRef.current = true;
      rememberView(token, PLANS_SCREEN_KEY, {
        plans: nextPlans, activePlanId: active?.plan_id ?? null, customDrafts: custom.rows, customError: custom.error,
      } satisfies PlansScreenView, epoch);
    } catch (plansError) {
      if (latestTokenRef.current !== token) {
        return;
      }
      const message = plansError instanceof Error ? plansError.message : "";
      setError(message.includes("401") || message.toLowerCase().includes("session")
        ? "Session expired. Sign in again."
        : describeLoadFailure(plansError, "Connection issue. Try again in a minute."));
    } finally {
      if (latestTokenRef.current === token) {
        setIsLoading(false);
      }
    }
  }, [session?.access_token]);

  useEffect(() => {
    void loadPlans();
  }, [loadPlans]);

  useOnReconnect(() => void loadPlans(), Boolean(error));

  useEffect(() => {
    if (!archivedPlans.length) {
      setIsArchiveOpen(false);
    }
  }, [archivedPlans.length]);

  useEffect(() => {
    const token = session?.access_token;
    if (!token) return;
    let cancelled = false;
    // A label only, so a Today view read moments ago by Home is enough.
    getToday(token, { reuseForMs: SHARED_READ_MAX_AGE_MS })
      .then((command) => {
        const planId = command.active_plan?.id;
        const rawPhase = command.active_plan?.phase;
        if (!cancelled && planId && rawPhase) {
          setActivePhase({ planId, phase: humanizeIfRawEnum(rawPhase) });
        }
      })
      // The phase is a nicety: without it the card simply leaves it out.
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [session?.access_token]);

  function handlePlanDeleted(planId: string) {
    setLocalPlans((current) => {
      const source = current ?? plans;
      return source.filter((currentPlan) => currentPlan.plan_id !== planId);
    });
    router.refresh();
  }

  function handlePlanRenamed(updatedPlan: PlanSummary) {
    setLocalPlans((current) => {
      const source = current ?? plans;
      return source.map((currentPlan) => (currentPlan.plan_id === updatedPlan.plan_id ? { ...currentPlan, ...updatedPlan } : currentPlan));
    });
    router.refresh();
  }

  async function activatePlan(plan: PlanSummary, overlapAction?: ActivePlanOverlapAction): Promise<void> {
    const token = session?.access_token;
    if (!token || !canSetActive(plan)) {
      return;
    }
    setIsSettingActivePlanId(plan.plan_id);
    try {
      const active = await setActivePlan(token, plan.plan_id, { overlapAction });
      setActivePlanId(active.plan_id);
      setOverlapConflictPlan(null);
      showToast("Active plan updated.", { tone: "success" });
      await loadPlans();
      requestXpRefresh();
      router.refresh();
    } catch (activeError) {
      if (!overlapAction && isActivePlanOverlapError(activeError)) {
        setOverlapConflictPlan(plan);
        return;
      }
      const message = activeError instanceof Error ? activeError.message : "Unable to set active plan.";
      showToast(message, { tone: "error" });
    } finally {
      setIsSettingActivePlanId(null);
    }
  }

  async function handleSetActive(plan: PlanSummary): Promise<void> {
    await activatePlan(plan);
  }

  async function handleOverlapConfirm(action: ActivePlanOverlapAction): Promise<void> {
    if (!overlapConflictPlan) {
      return;
    }
    await activatePlan(overlapConflictPlan, action);
  }

  const isPlanListLoading = isLoading;

  return (
    <RequireAuth>
      <section className={styles.page} data-plan-workspace>
        <header className={styles.pageHeader}>
          <h1 className={`athlete-motion-slot athlete-motion-header ${styles.pageTitle}`}>
            {/* The heading depends on the active plan: hold its space, never guess. */}
            {isLoading ? <Skeleton variant="text" width="6em" height="0.9em" /> : activePlan?.fight_date ? t("fightCampHeading") : t("planWorkspace")}
          </h1>
          <p className={styles.pageSubtitle}>Follow and manage your training.</p>
        </header>

        {error ? (
          <div className="error-banner athlete-motion-slot athlete-motion-status" role="alert">
            <span>{error}</span>
            {error.includes("Session expired") ? null : (
              <button
                type="button"
                className="error-banner-retry"
                onClick={() => void loadPlans()}
                disabled={isLoading}
              >
                {isLoading ? t("retrying") : t("retry")}
              </button>
            )}
          </div>
        ) : null}

        <div className="athlete-motion-slot athlete-motion-main">
          {isPlanListLoading ? (
            <CampCardSkeleton />
          ) : (
            <CurrentCampCard
              plan={activePlan}
              intake={intakeSource}
              tacticalStyle={tacticalStyle}
              phase={phase}
              onManage={() => setManagedPlanId(activePlan?.plan_id ?? null)}
            />
          )}
        </div>

        {!isLoading && heldForReviewPlans.length > 0 ? (
          <HeldPlansReviewNotice plans={heldForReviewPlans} />
        ) : null}

        {isLoading ? (
          <div className={`athlete-motion-slot athlete-motion-main ${styles.section}`} aria-busy="true">
            <div className={`${styles.sectionHead} loading-reveal`}>
              <h2 className={styles.sectionTitle}>{t("previousPlans")}</h2>
              <span className={styles.sectionMeta}>{t("loading")}</span>
            </div>
            <ul className={`${styles.rows} loading-reveal`}>
              <PlanRowSkeleton />
              <PlanRowSkeleton />
              <PlanRowSkeleton />
            </ul>
          </div>
        ) : null}

        {!isLoading ? (
          <section id="custom-plans" aria-label="Saved plans" className={`athlete-motion-slot athlete-motion-main ${styles.section}`}>
            <div className={styles.sectionHead}>
              <h2 className={styles.sectionTitle}>{t("previousPlans")}</h2>
              {savedRows.length ? (
                <span className={styles.sectionMeta}>{t("savedCount", { count: savedRows.length })}</span>
              ) : null}
            </div>
            {savedRows.length > 0 ? (
              <ul className={styles.rows}>
                {savedRows.map((row) => row.kind === "draft" ? (
                  <CustomDraftRow key={`draft:${row.draft.id}`} draft={row.draft} />
                ) : (
                  <PlanRow
                    key={row.plan.plan_id}
                    plan={row.plan}
                    activePlanId={activePlanId}
                    onOpen={() => setManagedPlanId(row.plan.plan_id)}
                  />
                ))}
              </ul>
            ) : (
              <p className="muted">{visiblePlans.length ? "No other saved plans." : "No saved plans yet."}</p>
            )}

            {customError ? (
              <div className="error-banner" role="alert">
                <span>{customError}</span>
                <button className="error-banner-retry" onClick={() => void loadPlans()}>Retry custom plans</button>
              </div>
            ) : null}
            {archivedPlans.length ? (
              <>
                <button
                  type="button"
                  className={styles.archiveToggle}
                  onClick={() => setIsArchiveOpen((current) => !current)}
                  aria-expanded={isArchiveOpen}
                  aria-controls="plans-archive"
                >
                  {t("archivedCount", { count: archivedPlans.length })}
                  <span className={styles.toggleChevron} aria-hidden="true" />
                </button>
                {isArchiveOpen ? (
                  <ul id="plans-archive" className={styles.rows} aria-label={t("olderPlans")}>
                    {archivedPlans.map((plan) => (
                      <PlanRow
                        key={plan.plan_id}
                        plan={plan}
                        activePlanId={activePlanId}
                        onOpen={() => setManagedPlanId(plan.plan_id)}
                      />
                    ))}
                  </ul>
                ) : null}
              </>
            ) : null}
          </section>
        ) : null}

        <section className={styles.tools} aria-label="Training tools">
          <h2 className={styles.sectionTitle}>Training tools</h2>
          <div className={`${actionStyles.actions} ${styles.shortcuts}`}>
            <LogTraining accessToken={session?.access_token ?? null} variant="tile" />
            <TrainingLibraryLink variant="tile" />
          </div>
        </section>

        {managedPlan ? (
          <PlanManageSheet
            key={managedPlan.plan_id}
            plan={managedPlan}
            activePlanId={activePlanId}
            accessToken={session?.access_token ?? null}
            isSettingActive={isSettingActivePlanId === managedPlan.plan_id}
            onSetActive={handleSetActive}
            onPlanRenamed={handlePlanRenamed}
            onPlanDeleted={handlePlanDeleted}
            onClose={() => setManagedPlanId(null)}
          />
        ) : null}

        {overlapConflictPlan ? (
          <PlanSwitchDialog
            plan={overlapConflictPlan}
            isPending={isSettingActivePlanId === overlapConflictPlan.plan_id}
            onConfirm={handleOverlapConfirm}
            onCancel={() => setOverlapConflictPlan(null)}
          />
        ) : null}
      </section>
    </RequireAuth>
  );
}
