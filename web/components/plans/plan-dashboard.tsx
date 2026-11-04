"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { type FormEvent, useState } from "react";

import { InjuryDetailSheet } from "@/components/today/injury-detail-sheet";
import { useToast } from "@/components/toast-provider";
import { archivePlan, renamePlan } from "@/lib/api";
import { isSafeAvatarImageUrl } from "@/lib/avatar-image-url";
import { toISODate } from "@/lib/camp-map";
import { getFightCountdown } from "@/lib/fight-countdown";
import { markGenerationIntent } from "@/lib/generation-intent";
import {
  EQUIPMENT_ACCESS_OPTIONS,
  getOptionLabels,
  TACTICAL_STYLE_OPTIONS,
  TECHNICAL_STYLE_OPTIONS,
} from "@/lib/intake-options";
import { canSetActivePlan, isArchivedPlan, isCompletedFightCamp } from "@/lib/plan-active";
import {
  formatAthletePlanStatus,
  formatPlanFightDate,
  formatPlanTimestamp,
  getPlanDisplayName,
  getPlanStyleSummary,
} from "@/lib/plan-format";
import { getPlanReviewReason } from "@/lib/plan-review";
import { useTrainingDay } from "@/lib/use-training-day";
import type { CustomPlan } from "@/lib/api-schema.generated";
import type { MeResponse, PlanRequest, PlanSummary, ProfileRecord } from "@/lib/types";

import styles from "./plans.module.css";

// The Plans tab: the active plan and compact saved rows.
// Plan management (make active, rename, archive) lives in its sheet.

type SummaryLine = {
  label: string;
  value: string;
};

function getRenameDraftValue(plan: PlanSummary): string {
  return plan.plan_name?.trim() || plan.fight_date || "";
}

export function canSetActive(plan: PlanSummary): boolean {
  return canSetActivePlan(plan.activation_state);
}

function isActivePlan(plan: PlanSummary, activePlanId: string | null): boolean {
  return Boolean(activePlanId && plan.plan_id === activePlanId);
}

function formatCompactList(values: string[], fallback: string): string {
  return values.length ? values.join(", ") : fallback;
}

function formatWeeklyTrainingDays(value: number | null | undefined): string | null {
  if (!Number.isFinite(value) || value === null || value === undefined || value <= 0) {
    return null;
  }
  return value > 7 ? `${value} sessions per week (legacy)` : `${value} per week (recorded)`;
}

function countTrainingDays(availability: string[] | undefined): number | null {
  if (!availability?.length) {
    return null;
  }
  return availability.length;
}

function summarizeEquipment(equipmentAccess: string[] | undefined): string | null {
  if (!equipmentAccess?.length) {
    return null;
  }
  const labels = getOptionLabels(EQUIPMENT_ACCESS_OPTIONS, equipmentAccess);
  const visible = labels.slice(0, 3);
  const remaining = labels.length - visible.length;
  return remaining > 0 ? `${visible.join(", ")} +${remaining} more` : visible.join(", ");
}

function getProfileSource(me: MeResponse | null): ProfileRecord | null {
  return me?.profile ?? null;
}

type DraftWithSource = PlanRequest & { plan_source?: string };

function getSavedDetailedDraft(me: MeResponse | null): PlanRequest | null {
  const draft = me?.profile?.onboarding_draft as DraftWithSource | null | undefined;
  if (!draft) {
    return null;
  }
  return draft.plan_source === "quick_build" ? null : draft;
}

export function getIntakeSource(me: MeResponse | null): PlanRequest | null {
  return me?.latest_intake ?? getSavedDetailedDraft(me) ?? null;
}

function summarizeProfile(me: MeResponse | null): SummaryLine[] {
  const profile = getProfileSource(me);
  const intake = getIntakeSource(me);
  const athleteName = profile?.full_name?.trim() || intake?.athlete.full_name?.trim() || "Athlete profile";
  const technicalStyle = getOptionLabels(
    TECHNICAL_STYLE_OPTIONS,
    profile?.technical_style?.length ? profile.technical_style : intake?.athlete.technical_style ?? [],
  );
  const tacticalStyle = getOptionLabels(
    TACTICAL_STYLE_OPTIONS,
    profile?.tactical_style?.length ? profile.tactical_style : intake?.athlete.tactical_style ?? [],
  );

  const lines: SummaryLine[] = [
    { label: "Athlete", value: athleteName },
    { label: "Combat sport", value: formatCompactList(technicalStyle, "Not set yet") },
  ];

  if (tacticalStyle.length) {
    lines.push({ label: "Tactical style", value: tacticalStyle.join(", ") });
  }

  return lines;
}

function summarizeIntake(me: MeResponse | null): SummaryLine[] {
  const intake = getIntakeSource(me);
  if (!intake) {
    return [];
  }

  const lines: SummaryLine[] = [];
  const weeklyTrainingDays = formatWeeklyTrainingDays(intake.weekly_training_frequency);
  const trainingDayCount = countTrainingDays(intake.training_availability);
  const equipmentSummary = summarizeEquipment(intake.equipment_access);

  if (weeklyTrainingDays) {
    lines.push({ label: "Training frequency", value: weeklyTrainingDays });
  }
  if (trainingDayCount !== null) {
    lines.push({ label: "Training days", value: `${trainingDayCount} day${trainingDayCount === 1 ? "" : "s"} available` });
  }
  if (equipmentSummary) {
    lines.push({ label: "Equipment", value: equipmentSummary });
  }

  return lines;
}

function connectionAwareMessage(error: unknown, fallback: string): string {
  const message = error instanceof Error ? error.message : fallback;
  return /Unable to reach the server|502|503|504/.test(message) ? "Connection issue. Try again in a minute." : message;
}

/**
 * Rename and archive for one plan: the state and API calls the Manage sheet
 * drives. One copy for the current camp and every saved plan.
 */
export function usePlanManagement({
  plan,
  accessToken,
  onPlanRenamed,
  onPlanDeleted,
}: {
  plan: PlanSummary;
  accessToken: string | null;
  onPlanRenamed: (updatedPlan: PlanSummary) => void;
  onPlanDeleted: (planId: string) => void;
}) {
  const { showToast } = useToast();
  const [pendingAction, setPendingAction] = useState<"rename" | "delete" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isRenaming, setIsRenaming] = useState(false);
  const [renameDraft, setRenameDraft] = useState(() => getRenameDraftValue(plan));
  const [isArchiveConfirmOpen, setIsArchiveConfirmOpen] = useState(false);

  function startRename() {
    setError(null);
    setRenameDraft(getRenameDraftValue(plan));
    setIsArchiveConfirmOpen(false);
    setIsRenaming(true);
  }

  function cancelRename() {
    if (pendingAction) return;
    setError(null);
    setIsRenaming(false);
  }

  async function submitRename(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!accessToken) {
      setError("Session expired. Sign in again.");
      return;
    }
    const normalizedName = renameDraft.trim();
    if (!normalizedName) {
      setError("Plan name cannot be empty.");
      return;
    }
    if (normalizedName === (plan.plan_name?.trim() || "")) {
      setError(null);
      setIsRenaming(false);
      return;
    }
    setPendingAction("rename");
    setError(null);
    try {
      const updatedPlan = await renamePlan(accessToken, plan.plan_id, normalizedName);
      onPlanRenamed(updatedPlan);
      showToast("Plan renamed.", { tone: "success" });
      setIsRenaming(false);
    } catch (renameError) {
      setError(connectionAwareMessage(renameError, "Unable to rename this plan."));
    } finally {
      setPendingAction(null);
    }
  }

  function requestArchive() {
    setError(null);
    setIsRenaming(false);
    setIsArchiveConfirmOpen(true);
  }

  function dismissArchive() {
    if (pendingAction === "delete") return;
    setIsArchiveConfirmOpen(false);
  }

  async function confirmArchive(): Promise<boolean> {
    if (!accessToken) {
      setError("Session expired. Sign in again.");
      return false;
    }
    setPendingAction("delete");
    setError(null);
    try {
      await archivePlan(accessToken, plan.plan_id);
      setIsArchiveConfirmOpen(false);
      onPlanDeleted(plan.plan_id);
      showToast(`Archived ${getPlanDisplayName(plan)}.`, { tone: "success" });
      return true;
    } catch (deleteError) {
      setError(connectionAwareMessage(deleteError, "Unable to archive this plan."));
      return false;
    } finally {
      setPendingAction(null);
    }
  }

  return {
    pendingAction,
    error,
    isRenaming,
    renameDraft,
    setRenameDraft,
    startRename,
    cancelRename,
    submitRename,
    isArchiveConfirmOpen,
    requestArchive,
    dismissArchive,
    confirmArchive,
  };
}

/** "7 Oct · 19:21": a saved plan's build time, short enough for one line. */
function formatBuiltShort(value: string): string {
  const full = formatPlanTimestamp(value);
  const match = /^\S+ (\d{1,2}) (\S+) \d{4}, (\d{2}:\d{2})$/.exec(full);
  return match ? `${Number(match[1])} ${match[2]} · ${match[3]}` : full;
}

/** What is notable about a saved plan beyond its sport and build time. */
function getPlanRowNote(plan: PlanSummary, activePlanId: string | null): string | null {
  if (isArchivedPlan(plan.status)) return "Archived";
  if (isActivePlan(plan, activePlanId)) return "Active";
  if (isCompletedFightCamp(plan.activation_state)) return "Camp complete";
  if (!canSetActive(plan)) return getPlanReviewReason(plan) || "Can't be active";
  return null;
}

/**
 * Management for one plan, off the dashboard: its facts, then open, activate,
 * rename and archive. Archive confirms in place rather than in a second dialog.
 */
export function PlanManageSheet({
  plan,
  activePlanId,
  accessToken,
  isSettingActive,
  onSetActive,
  onPlanRenamed,
  onPlanDeleted,
  onClose,
}: {
  plan: PlanSummary;
  activePlanId: string | null;
  accessToken: string | null;
  isSettingActive: boolean;
  onSetActive: (plan: PlanSummary) => Promise<void>;
  onPlanRenamed: (updatedPlan: PlanSummary) => void;
  onPlanDeleted: (planId: string) => void;
  onClose: () => void;
}) {
  const manage = usePlanManagement({
    plan,
    accessToken,
    onPlanRenamed,
    onPlanDeleted,
  });
  const active = isActivePlan(plan, activePlanId);
  const archived = isArchivedPlan(plan.status);
  const canActivate = !archived && !active && canSetActive(plan);
  const busy = manage.pendingAction !== null || isSettingActive;
  const facts: SummaryLine[] = [
    ...(plan.fight_date ? [{ label: "Fight date", value: formatPlanFightDate(plan.fight_date) }] : []),
    { label: "Built", value: formatPlanTimestamp(plan.created_at) },
    {
      label: "Status",
      value: active ? "Active" : archived ? "Archived" : formatAthletePlanStatus(plan.status),
    },
  ];
  const note = getPlanReviewReason(plan);

  return (
    <InjuryDetailSheet title={getPlanDisplayName(plan)} busy={busy} onClose={onClose}>
      <div className="injury-sheet-body">
        <dl className={styles.facts}>
          {facts.map((fact) => (
            <div key={fact.label}>
              <dt>{fact.label}</dt>
              <dd>{fact.value}</dd>
            </div>
          ))}
        </dl>
        {note ? <p className="muted">{note}</p> : null}

        {manage.isRenaming ? (
          <form className={styles.inlineForm} onSubmit={manage.submitRename}>
            <label htmlFor={`rename-plan-${plan.plan_id}`}>Plan name</label>
            <input
              id={`rename-plan-${plan.plan_id}`}
              value={manage.renameDraft}
              onChange={(event) => manage.setRenameDraft(event.target.value)}
              disabled={busy}
              maxLength={120}
              autoFocus
            />
            <div className={styles.sheetActions}>
              <button type="submit" className="secondary-button" disabled={busy}>
                {manage.pendingAction === "rename" ? "Saving..." : "Save"}
              </button>
              <button type="button" className="ghost-button" onClick={manage.cancelRename} disabled={busy}>
                Cancel
              </button>
            </div>
          </form>
        ) : manage.isArchiveConfirmOpen ? (
          <div className={styles.inlineForm} role="group" aria-label="Archive plan">
            <p>Archive this plan? You can still view it in your archived list.</p>
            <div className={styles.sheetActions}>
              <button
                type="button"
                className="secondary-button danger-button"
                onClick={() => void manage.confirmArchive().then((done) => { if (done) onClose(); })}
                disabled={busy}
              >
                {manage.pendingAction === "delete" ? "Archiving..." : "Archive"}
              </button>
              <button type="button" className="ghost-button" onClick={manage.dismissArchive} disabled={busy}>
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <div className={styles.sheetActions} data-stack="true">
            <Link href={`/plans/${plan.plan_id}`} className="cta">
              {archived ? "Preview plan" : "Open plan"}
            </Link>
            {canActivate ? (
              <button
                type="button"
                className="secondary-button"
                disabled={busy}
                onClick={() => {
                  // The overlap dialog may follow; it should stand alone.
                  onClose();
                  void onSetActive(plan);
                }}
              >
                {isSettingActive ? "Setting..." : "Make active"}
              </button>
            ) : null}
            {archived ? (
              <Link href="/create" className="ghost-button">
                Create a plan
              </Link>
            ) : (
              <>
                <button type="button" className="ghost-button" onClick={manage.startRename} disabled={busy}>
                  Rename
                </button>
                <button type="button" className="ghost-button danger-button" onClick={manage.requestArchive} disabled={busy}>
                  Archive
                </button>
              </>
            )}
          </div>
        )}
        {manage.error ? <div className="error-banner" role="alert">{manage.error}</div> : null}
      </div>
    </InjuryDetailSheet>
  );
}

/** A saved plan in the list: what it is and when it was built. Opens its sheet. */
export function PlanRow({
  plan,
  activePlanId,
  onOpen,
}: {
  plan: PlanSummary;
  activePlanId: string | null;
  onOpen: () => void;
}) {
  const note = getPlanRowNote(plan, activePlanId);
  const meta = [getPlanStyleSummary(plan), `Built ${formatBuiltShort(plan.created_at)}`, note].filter(Boolean);
  return (
    <li>
      <button type="button" className={styles.row} aria-haspopup="dialog" onClick={onOpen}>
        <span className={styles.rowText}>
          <span className={styles.rowHeading}><span className={styles.rowTitle}>{getPlanDisplayName(plan)}</span>{plan.custom_plan_id ? <span className={styles.customPill}>Custom</span> : null}</span>
          <span className={styles.rowMeta}>{meta.join(" · ")}</span>
        </span>
        <span className={styles.chevron} aria-hidden="true" />
      </button>
    </li>
  );
}

/** Unpublished prescriptions use the same saved-list layout and open the editor. */
export function CustomDraftRow({ draft }: { draft: CustomPlan }) {
  const days = new Set(draft.sessions.map((session) => session.weekday).filter(Boolean));
  return (
    <li>
      <Link href={`/plans/custom?plan=${draft.id}`} className={styles.row}>
        <span className={styles.rowText}>
          <span className={styles.rowHeading}>
            <span className={styles.rowTitle}>{draft.name}</span>
            <span className={styles.customPill}>Custom</span>
          </span>
          <span className={styles.rowMeta}>
            {draft.incomplete ? "Draft" : "Saved draft"} · {draft.sessions.length} session{draft.sessions.length === 1 ? "" : "s"}
            {days.size ? ` · ${days.size} day${days.size === 1 ? "" : "s"}` : " · No days assigned"}
          </span>
        </span>
        <span className={styles.chevron} aria-hidden="true" />
      </Link>
    </li>
  );
}

/**
 * The current camp at a glance: what it is, how far out the fight is, and one
 * way in. Everything administrative sits behind Manage.
 */
export function CurrentCampCard({
  plan,
  intake,
  tacticalStyle,
  phase,
  onManage,
}: {
  plan: PlanSummary | null;
  intake: PlanRequest | null;
  tacticalStyle: string | null;
  phase: string | null;
  onManage: () => void;
}) {
  const t = useTranslations("Workspace");
  const trainingDay = useTrainingDay();
  const hasSavedIntake = Boolean(intake);

  if (!plan) {
    return (
      <article className={`${styles.campCard} ${styles.emptyCamp}`}>
        <p className={styles.eyebrow}>{t("currentCamp")}</p>
        <h2 className={styles.campTitle}>No active plan</h2>
        <p className="muted">
          {hasSavedIntake
            ? "No active plan is selected. Make one of your saved plans active."
            : "Set your goals and get a plan in about two minutes."}
        </p>
        {hasSavedIntake ? <Link href="/onboarding" className="cta">Resume Camp Setup</Link> :
          <Link href="/create" className="cta">Create a plan</Link>}
      </article>
    );
  }

  const countdown = trainingDay
    ? getFightCountdown({ fightDate: plan.fight_date, trainingDay: toISODate(trainingDay) })
    : null;
  const title = plan.plan_name?.trim() || (plan.fight_date ? "Fight camp" : "Open training plan");
  const facts: SummaryLine[] = [
    { label: "Sport", value: getPlanStyleSummary(plan) },
    ...(tacticalStyle ? [{ label: "Style", value: tacticalStyle }] : []),
    ...(phase ? [{ label: "Phase", value: phase }] : []),
  ];

  return (
    <article className={styles.campCard}>
      <div className={styles.campHead}>
        <p className={styles.eyebrow}>{plan.custom_plan_id ? t("activePlan") : plan.fight_date ? t("currentCamp") : t("currentBlock")}</p>
        <span className={styles.activePill}>Active</span>
      </div>
      <h2 className={styles.campTitle}>{title}</h2>
      {plan.fight_date ? (
        <div className={styles.fightRow}>
          <div>
            <p className={styles.factLabel}>Fight date</p>
            <p className={styles.fightDate}>{formatPlanFightDate(plan.fight_date)}</p>
          </div>
          {countdown ? (
            <div className={styles.countdown}>
              <p className={styles.countdownValue}>D-{countdown.daysOut}</p>
              <p className={styles.factLabel}>{t("untilFight")}</p>
            </div>
          ) : null}
        </div>
      ) : null}
      <dl className={styles.campSummary}>
        {facts.map((fact) => (
          <div key={fact.label}>
            <dt>{fact.label}</dt>
            <dd>{fact.value}</dd>
          </div>
        ))}
      </dl>
      <Link href={`/plans/${plan.plan_id}`} className={`cta ${styles.primaryAction}`}>
        {t("openTrainingPlan")} <span aria-hidden="true">→</span>
      </Link>
      <div className={styles.quietLinks}>
        <Link
          href={plan.custom_plan_id ? `/plans/custom?plan=${plan.custom_plan_id}` : intake ? "/generate" : "/onboarding"}
          onClick={() => {
            if (intake && !plan.custom_plan_id) markGenerationIntent();
          }}
        >
          {plan.custom_plan_id ? "Edit Plan" : t("newVersion")}
        </Link>
        <span aria-hidden="true">·</span>
        <button type="button" aria-haspopup="dialog" onClick={onManage}>
          {t("manage")}
        </button>
      </div>
    </article>
  );
}

/** The athlete behind the next build, in one line; the details open on tap. */
export function AthleteProfileRow({ me }: { me: MeResponse | null }) {
  const t = useTranslations("Workspace");
  const [open, setOpen] = useState(false);
  const [failedAvatar, setFailedAvatar] = useState<string | null>(null);
  const profileLines = summarizeProfile(me);
  const intakeLines = summarizeIntake(me);
  const hasIntake = Boolean(getIntakeSource(me));
  const summary = profileLines
    .map((line) => line.value)
    .filter((value) => value && value !== "Not set yet" && value !== "Athlete profile")
    .join(" · ");
  const facts = [...profileLines.slice(1), ...intakeLines];
  const name = profileLines[0]?.value ?? "Athlete";
  const avatar = isSafeAvatarImageUrl(me?.profile.avatar_url) ? me?.profile.avatar_url : null;
  const initials = name.split(/\s+/).filter(Boolean).slice(0, 2).map(part => part[0]).join("").toUpperCase();
  const stance = me?.profile.stance?.replaceAll("_", " ");
  const sportSummary = [...profileLines.slice(1).map(line => line.value).filter(value => value !== "Not set yet"),
    ...(stance ? [stance.charAt(0).toUpperCase() + stance.slice(1)] : [])].join(" · ");

  return (
    <>
      <button type="button" className={styles.profileRow} aria-label={`Athlete profile: ${summary || name}`} aria-haspopup="dialog" onClick={() => setOpen(true)}>
        <span className={styles.profileAvatar} aria-hidden="true">
          {avatar && avatar !== failedAvatar ?
            // eslint-disable-next-line @next/next/no-img-element -- saved HTTPS/data avatars use the same safe URL policy as AppNav
            <img src={avatar} alt="" onError={() => setFailedAvatar(avatar)} /> : <span>{initials}</span>}
        </span>
        <span className={styles.rowText}>
          <span className={styles.sectionTitle}>{t("athleteProfile")}</span>
          <span className={styles.profileSummary}><span className={styles.profileName}>{name}</span>
            <span className={styles.rowMeta}> · {sportSummary || "Not set yet"}</span></span>
        </span>
        <span className={styles.chevron} aria-hidden="true" />
      </button>
      {open ? (
        <InjuryDetailSheet title={t("athleteProfile")} onClose={() => setOpen(false)}>
          <div className="injury-sheet-body">
            <p className="muted">Profile and intake used for your next build.</p>
            {facts.length ? (
              <dl className={styles.facts}>
                {facts.map((line) => (
                  <div key={line.label}>
                    <dt>{line.label}</dt>
                    <dd>{line.value}</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="muted">No plan source details saved yet.</p>
            )}
            <Link href="/settings#account" className="secondary-button">Edit profile & photo</Link>
            <Link href="/onboarding" className="secondary-button">
              {hasIntake ? "Review & edit intake" : "Complete Camp Setup"}
            </Link>
          </div>
        </InjuryDetailSheet>
      ) : null}
    </>
  );
}
