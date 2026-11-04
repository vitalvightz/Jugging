"use client";
import { AnalyticsPreference } from "@/components/analytics-preference";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef, useState, useTransition } from "react";

import { RequireAuth } from "@/components/auth-guard";
import { useAppSession } from "@/components/auth-provider";
import { PasswordStrengthMeter } from "@/components/password-strength-meter";
import { InstallUnlxck } from "@/components/install-unlxck";
import { LanguageSwitcher } from "@/components/language-switcher";
import { PushNotificationSettings } from "@/components/push-notification-settings";
import { PrivateTrialGuide } from "@/components/private-trial-guide";
import { GlobalFeedback } from "@/components/feedback/global-feedback";
import { ApiError, changeUsername, recordCompliance, updateMe } from "@/lib/api";
import {
  consentCopyForBand,
  deriveAgeFromDateOfBirth,
  hasHealthDataConsent,
  healthConsentSummary,
  termsSummary,
  validateDateOfBirthChange,
} from "@/lib/compliance";
import { PRIVACY_HREF, TERMS_HREF, buildDataRequestMailto } from "@/lib/legal-documents";
import { shouldShowAdminPanelLink } from "@/lib/admin-nav-visibility";
import { isSafeAvatarImageUrl } from "@/lib/avatar-image-url";
import { formatAppDate, formatAppDateTime } from "@/lib/date-format";
import {
  EQUIPMENT_ACCESS_OPTIONS,
  KEY_GOAL_OPTIONS,
  TRAINING_AVAILABILITY_OPTIONS,
  detectDeviceTimeZone,
  getOptionLabel,
  getOptionLabels,
  PROFESSIONAL_STATUS_OPTIONS,
  STANCE_OPTIONS,
  TACTICAL_STYLE_OPTIONS,
  TECHNICAL_STYLE_OPTIONS,
} from "@/lib/intake-options";
import { hydratePlanRequest } from "@/lib/onboarding";
import { evaluatePasswordStrength } from "@/lib/password-strength";
import { getSupabaseBrowserClient } from "@/lib/supabase";
import { ATHLETE_FULL_NAME_MAX, AVATAR_URL_MAX } from "@/lib/input-limits";
import type { AppearanceMode, GuidedInjuryInput, PlanRequest } from "@/lib/types";

type SettingsSection = {
  id: string;
  label: string;
};

type ProgrammeControls = {
  injuryFiltering: "light" | "strict";
  fatigueAdjustment: "light" | "strict";
  requireCoachReview: boolean;
  autoGeneratePlans: boolean;
};

type AdminTemplateDraft = {
  welcomeMessage: string;
  planEmail: string;
  coachNotes: string;
};

// Only guards against decoding absurd files on the phone: the original never
// leaves the device, it is shrunk to ~100 KB first. 25 MB covers 48 MP shots.
const MAX_AVATAR_FILE_BYTES = 25 * 1024 * 1024;
// Largest edge (px) we keep for a profile photo — the avatar only ever renders
// in a small circle, so anything bigger is wasted bytes.
const AVATAR_MAX_DIMENSION = 512;
// Byte budget for the resulting base64 data URL. Real phone photos are several
// MB raw, which blows past the API's request-body ceiling and the avatar_url
// field limit ("request body too large"), so we downscale + JPEG-compress the
// upload to fit well under 100 KB before sending it.
const AVATAR_TARGET_DATA_URL_CHARS = 96 * 1024;

// Downscale + JPEG-compress a chosen image into a small base64 data URL.
// Loads the file into an offscreen canvas, shrinks the longest edge to
// AVATAR_MAX_DIMENSION, then steps quality (and, if still too big, dimensions)
// down until the encoded data URL fits AVATAR_TARGET_DATA_URL_CHARS.
async function compressAvatarImage(file: File): Promise<string> {
  const objectUrl = URL.createObjectURL(file);
  try {
    const image = await new Promise<HTMLImageElement>((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error("decode-failed"));
      img.src = objectUrl;
    });

    const { naturalWidth: width, naturalHeight: height } = image;
    if (!width || !height) {
      throw new Error("decode-failed");
    }

    let dimension = Math.min(AVATAR_MAX_DIMENSION, Math.max(width, height));
    while (dimension >= 96) {
      const scale = dimension / Math.max(width, height);
      const targetW = Math.max(1, Math.round(width * scale));
      const targetH = Math.max(1, Math.round(height * scale));

      const canvas = document.createElement("canvas");
      canvas.width = targetW;
      canvas.height = targetH;
      const ctx = canvas.getContext("2d");
      if (!ctx) {
        throw new Error("canvas-unsupported");
      }
      ctx.drawImage(image, 0, 0, targetW, targetH);

      for (const quality of [0.82, 0.7, 0.6, 0.5, 0.4]) {
        const dataUrl = canvas.toDataURL("image/jpeg", quality);
        if (dataUrl.length <= AVATAR_TARGET_DATA_URL_CHARS) {
          return dataUrl;
        }
      }
      // Even the lowest quality is over budget at this size: shrink and retry.
      dimension = Math.round(dimension * 0.75);
    }
    throw new Error("too-large");
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

const USERNAME_PATTERN = /^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$/;
const USERNAME_MIN = 3;
const USERNAME_MAX = 24;
const PROGRAMME_CONTROLS_STORAGE_KEY = "unlxck.adminProgrammeControls";
const ADMIN_TEMPLATES_STORAGE_KEY = "unlxck.adminTemplateDrafts";

const ATHLETE_SETTINGS_SECTIONS: SettingsSection[] = [
  { id: "account", label: "Account" },
  { id: "training-profile", label: "Training Profile" },
  { id: "notifications", label: "Notifications" },
  { id: "subscription", label: "Subscription" },
  { id: "privacy", label: "Privacy" },
  { id: "private-trial", label: "Private Trial Guide" },
  { id: "send-feedback", label: "Send feedback" },
];

const ADMIN_SETTINGS_SECTIONS: SettingsSection[] = [
  { id: "admin-account", label: "Admin Account" },
  { id: "organisation", label: "Organisation" },
  { id: "coaches-roles", label: "Coaches & Roles" },
  { id: "programme-controls", label: "Programme Controls" },
  { id: "templates-billing", label: "Templates & Billing" },
  { id: "send-feedback", label: "Send feedback" },
];

const APPEARANCE_OPTIONS: Array<{
  value: AppearanceMode;
  label: string;
  description: string;
}> = [
  {
    value: "dark",
    label: "Dark",
    description: "Higher contrast workspace.",
  },
  {
    value: "light",
    label: "Light",
    description: "Brighter workspace.",
  },
];

const DEFAULT_PROGRAMME_CONTROLS: ProgrammeControls = {
  injuryFiltering: "strict",
  fatigueAdjustment: "light",
  requireCoachReview: true,
  autoGeneratePlans: true,
};

const DEFAULT_ADMIN_TEMPLATES: AdminTemplateDraft = {
  welcomeMessage: "Welcome to camp. Complete Camp Setup so your plan starts right.",
  planEmail: "Your updated training plan is ready.",
  coachNotes: "Keep review notes short, specific and tied to risk.",
};

function getInitials(name: string): string {
  const result = name
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((word) => word[0]?.toUpperCase() ?? "")
    .join("");
  return result || "A";
}

function isDataAvatarImageUrl(url: string): boolean {
  return url.startsWith("data:image/");
}

function validateUsernameClient(value: string): string | null {
  const trimmed = value.trim().toLowerCase();
  if (!trimmed) {
    return "Enter a username.";
  }
  if (trimmed.length < USERNAME_MIN || trimmed.length > USERNAME_MAX) {
    return `Username must be ${USERNAME_MIN}-${USERNAME_MAX} characters.`;
  }
  if (!USERNAME_PATTERN.test(trimmed)) {
    return "Use lowercase letters, digits, dots, dashes, or underscores. Must start and end with a letter or number.";
  }
  return null;
}

function formatNextAvailable(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  return formatAppDate(iso);
}

function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "Not saved yet";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "Not saved yet";
  return formatAppDateTime(iso);
}

function formatList(values: string[], fallback = "Not set", maxItems = 3): string {
  if (!values.length) {
    return fallback;
  }
  const visible = values.slice(0, maxItems);
  const extraCount = values.length - visible.length;
  return extraCount > 0 ? `${visible.join(", ")} +${extraCount}` : visible.join(", ");
}

function summarizeStyle(request: PlanRequest): string {
  const technical = getOptionLabels(TECHNICAL_STYLE_OPTIONS, request.athlete.technical_style);
  const tactical = getOptionLabels(TACTICAL_STYLE_OPTIONS, request.athlete.tactical_style);
  const stance = request.athlete.stance ? getOptionLabel(STANCE_OPTIONS, request.athlete.stance) : "";
  const parts = [...technical, ...tactical, stance].filter(Boolean);
  return formatList(parts, "Not set", 3);
}

function summarizeGoal(request: PlanRequest): string {
  const primaryValue = request.primary_goal || request.key_goals[0] || "";
  const primary = primaryValue ? getOptionLabel(KEY_GOAL_OPTIONS, primaryValue) : "";
  if (!primary) {
    return "Not set";
  }
  const secondaryCount = request.key_goals.filter((goal) => goal !== primaryValue).length;
  return secondaryCount > 0 ? `${primary} +${secondaryCount}` : primary;
}

function summarizeTrainingDays(request: PlanRequest): string {
  const labels = getOptionLabels(TRAINING_AVAILABILITY_OPTIONS, request.training_availability);
  const frequency = request.weekly_training_frequency;
  const sessions = !frequency ? "" : frequency > 7
    ? `${frequency} sessions/week (legacy)`
    : `${frequency}/week (recorded)`;
  const days = formatList(labels, "No days set", 4);
  return sessions ? `${sessions} - ${days}` : days;
}

function summarizeGuidedInjury(injury: GuidedInjuryInput | null | undefined): string | null {
  if (!injury) {
    return null;
  }
  const parts = [injury.area, injury.severity, injury.trend]
    .map((part) => String(part || "").trim())
    .filter(Boolean);
  return parts.length ? parts.join(" / ") : null;
}

function summarizeInjuries(request: PlanRequest): string {
  const guided =
    (request.guided_injuries ?? [])
      .map((injury) => summarizeGuidedInjury(injury))
      .find(Boolean) ?? summarizeGuidedInjury(request.guided_injury);
  if (guided) {
    const additionalCount = Math.max(0, (request.guided_injuries?.length ?? 0) - 1);
    return additionalCount > 0 ? `${guided} +${additionalCount}` : guided;
  }
  return request.injuries?.trim() || "None recorded";
}

function readLocalJson<T>(key: string, fallback: T): T {
  if (typeof window === "undefined") {
    return fallback;
  }
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? { ...fallback, ...JSON.parse(raw) } : fallback;
  } catch {
    return fallback;
  }
}

function writeLocalJson(key: string, value: unknown) {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Non-critical preference persistence.
  }
}

function SettingsNav({
  activeSection,
  sections,
  onSelect,
}: Readonly<{
  activeSection: string;
  sections: SettingsSection[];
  onSelect: (id: string) => void;
}>) {
  return (
    <nav className="settings-section-nav" aria-label="Settings sections">
      <label className="settings-section-picker">
        <span>Section</span>
        <select value={activeSection} onChange={(event) => onSelect(event.target.value)}>
          {sections.map((section) => <option key={section.id} value={section.id}>{section.label}</option>)}
        </select>
      </label>
      <p className="settings-section-nav-label">Sections</p>
      <div className="settings-section-nav-list">
        {sections.map((section) => (
          <a
            key={section.id}
            href={`#${section.id}`}
            className="settings-section-nav-link"
            aria-current={activeSection === section.id ? "true" : undefined}
            onClick={(event) => {
              event.preventDefault();
              onSelect(section.id);
            }}
          >
            {section.label}
          </a>
        ))}
      </div>
    </nav>
  );
}

function SettingsSummaryItem({ label, value }: Readonly<{ label: string; value: string }>) {
  return (
    <article className="plan-meta-item">
      <p className="plan-meta-label">{label}</p>
      <p className="plan-meta-value">{value}</p>
    </article>
  );
}

export default function SettingsPage() {
  const t = useTranslations("Workspace");
  const tNav = useTranslations("Navigation");
  const { isMeHydrated, me, previewAppearanceMode, replaceMe, session, signOut } = useAppSession();

  const [fullName, setFullName] = useState("");
  const [appearanceMode, setAppearanceMode] = useState<AppearanceMode>("dark");
  const [avatarUrl, setAvatarUrl] = useState("");
  const [urlInputValue, setUrlInputValue] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();
  const [isAvatarProcessing, setIsAvatarProcessing] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // Monotonic id for the in-flight avatar compression. Bumped whenever a new
  // photo is selected (or the avatar is otherwise changed) so a slower earlier
  // compression can't clobber a newer selection when it finishes last.
  const avatarRequestRef = useRef(0);

  const [usernameDraft, setUsernameDraft] = useState("");
  const [usernameError, setUsernameError] = useState<string | null>(null);
  const [usernameMessage, setUsernameMessage] = useState<string | null>(null);
  const [isUsernamePending, startUsernameTransition] = useTransition();

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showPasswords, setShowPasswords] = useState(false);
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [passwordMessage, setPasswordMessage] = useState<string | null>(null);
  const [isPasswordPending, startPasswordTransition] = useTransition();

  const [programmeControls, setProgrammeControls] = useState<ProgrammeControls>(DEFAULT_PROGRAMME_CONTROLS);
  const [adminTemplates, setAdminTemplates] = useState<AdminTemplateDraft>(DEFAULT_ADMIN_TEMPLATES);

  const [privacyMessage, setPrivacyMessage] = useState<string | null>(null);
  const [privacyError, setPrivacyError] = useState<string | null>(null);
  const [isConsentSaving, setIsConsentSaving] = useState(false);

  // Date of birth. `isEditingDateOfBirth` gates the input behind a deliberate
  // action, and `isConfirmingDateOfBirth` gates the save behind a second one:
  // this one value decides the age band, the under-18 safety rules and the age
  // every future generation runs against, so it is not something to change by
  // brushing a date picker on a phone.
  const [dateOfBirthDraft, setDateOfBirthDraft] = useState("");
  const [isEditingDateOfBirth, setIsEditingDateOfBirth] = useState(false);
  const [isConfirmingDateOfBirth, setIsConfirmingDateOfBirth] = useState(false);
  const [dateOfBirthError, setDateOfBirthError] = useState<string | null>(null);
  const [dateOfBirthMessage, setDateOfBirthMessage] = useState<string | null>(null);
  const [isDateOfBirthSaving, setIsDateOfBirthSaving] = useState(false);

  const isAdmin = me?.profile.role === "admin";
  const sections = isAdmin ? ADMIN_SETTINGS_SECTIONS : ATHLETE_SETTINGS_SECTIONS;
  const isReady = isMeHydrated && Boolean(me);

  const [activeSection, setActiveSection] = useState(sections[0]?.id ?? "");
  // The account card owns the save controls. When they scroll out of view with
  // edits pending, a floating bar takes over so the save is never far away.
  const accountActionsRef = useRef<HTMLDivElement | null>(null);
  const [isAccountActionsVisible, setIsAccountActionsVisible] = useState(true);
  // Device time zone we have already pushed, so a drift is synced once.
  const syncedTimeZoneRef = useRef<string | null>(null);
  const hydratedProfile = useMemo(() => hydratePlanRequest(me), [me]);
  const currentUsername = (me?.profile.username ?? "").trim();
  const detectedTimeZone = detectDeviceTimeZone() || me?.profile.athlete_timezone || "Automatic";
  const lastUpdatedLabel = formatDateTime(me?.profile.updated_at);
  const healthConsentGranted = hasHealthDataConsent(me);
  // Age-appropriate wording for the privacy card, from the server's band.
  const consentCopy = consentCopyForBand(me?.profile.age_band);
  // A mailto route is only offered once a real privacy address is configured.
  // Linking to the notice's "[ADD PRIVACY EMAIL...]" placeholder would give the
  // athlete a deletion route that silently goes nowhere.
  const deletionRequestHref = buildDataRequestMailto(
    "UNLXCK account and data deletion request",
    `Please delete my UNLXCK account and associated personal data.\n\nAccount email: ${me?.profile.email ?? ""}\n`,
  );
  const initials = getInitials(fullName || "Athlete");
  const professionalStatusLabel =
    getOptionLabel(PROFESSIONAL_STATUS_OPTIONS, hydratedProfile.athlete.professional_status ?? "") || "Not set";

  const rateLimit = me?.username_rate_limit;
  const usernameRemaining = rateLimit?.remaining ?? 4;
  const usernameMax = rateLimit?.max_changes_per_window ?? 4;
  const usernameWindowDays = rateLimit?.window_days ?? 30;
  const nextAvailableLabel = formatNextAvailable(rateLimit?.next_available_at);

  // The stored date is the only source of age truth; the age beside it is a
  // display value recomputed from it, never a second stored field.
  const savedDateOfBirth = (me?.profile.date_of_birth ?? "").slice(0, 10);
  const derivedAge = deriveAgeFromDateOfBirth(savedDateOfBirth || null);

  function beginDateOfBirthEdit() {
    setDateOfBirthDraft(savedDateOfBirth);
    setIsEditingDateOfBirth(true);
    setIsConfirmingDateOfBirth(false);
    setDateOfBirthError(null);
    setDateOfBirthMessage(null);
  }

  function cancelDateOfBirthEdit() {
    setIsEditingDateOfBirth(false);
    setIsConfirmingDateOfBirth(false);
    setDateOfBirthDraft("");
    setDateOfBirthError(null);
  }

  async function submitDateOfBirth() {
    const token = session?.access_token;
    const next = dateOfBirthDraft.trim();
    const validationError = validateDateOfBirthChange(next);
    if (validationError) {
      setDateOfBirthError(validationError);
      setIsConfirmingDateOfBirth(false);
      return;
    }
    if (next !== savedDateOfBirth && !isConfirmingDateOfBirth) {
      // First submit of a genuinely different date only asks for confirmation.
      // An unchanged date needs none — there is nothing to confirm.
      setIsConfirmingDateOfBirth(true);
      setDateOfBirthError(null);
      return;
    }
    if (!token || isDateOfBirthSaving) {
      return;
    }
    setIsDateOfBirthSaving(true);
    setDateOfBirthError(null);
    setDateOfBirthMessage(null);
    try {
      // Goes through the compliance endpoint, never straight to Supabase: the
      // browser has no write access to this column, and the server re-checks the
      // 13+ floor and re-derives `is_minor`/`age_band` from what it stores.
      const updated = await recordCompliance(token, { date_of_birth: next });
      // Replacing the cached profile is what makes the change take effect
      // everywhere at once — Camp Setup's displayed age, the weight-cut surface
      // and the safety copy all read this one response.
      replaceMe(updated);
      setIsEditingDateOfBirth(false);
      setIsConfirmingDateOfBirth(false);
      setDateOfBirthMessage("Date of birth updated. Age-based guidance now uses the new date.");
    } catch (dobError) {
      setDateOfBirthError(
        dobError instanceof Error
          ? dobError.message
          : "Your date of birth could not be saved. Try again.",
      );
      setIsConfirmingDateOfBirth(false);
    } finally {
      setIsDateOfBirthSaving(false);
    }
  }

  async function toggleHealthConsent(grant: boolean) {
    const token = session?.access_token;
    if (!token || isConsentSaving) {
      return;
    }
    setIsConsentSaving(true);
    setPrivacyMessage(null);
    setPrivacyError(null);
    try {
      // The server records the withdrawal timestamp and returns the updated
      // profile, so the UI reflects the stored evidence rather than an optimistic
      // guess about what was written.
      const next = await recordCompliance(token, { health_data_consent: grant });
      replaceMe(next);
      setPrivacyMessage(
        grant
          ? "Health-data consent recorded. Personalised health features are available again."
          : "Health-data consent withdrawn. UNLXCK will not use your health information for new personalised guidance.",
      );
    } catch (consentError) {
      setPrivacyError(
        consentError instanceof Error
          ? consentError.message
          : "Your consent change could not be saved. Try again.",
      );
    } finally {
      setIsConsentSaving(false);
    }
  }

  const passwordStrength = useMemo(
    () => evaluatePasswordStrength(newPassword, { fullName, email: me?.profile.email ?? "" }),
    [newPassword, fullName, me?.profile.email],
  );

  const trainingProfileSummary = useMemo(
    () => [
      { label: "Current sport/style", value: summarizeStyle(hydratedProfile) },
      { label: "Current focus", value: summarizeGoal(hydratedProfile) },
      {
        label: "Equipment access",
        value: formatList(getOptionLabels(EQUIPMENT_ACCESS_OPTIONS, hydratedProfile.equipment_access), "Not set", 4),
      },
      { label: "Training days", value: summarizeTrainingDays(hydratedProfile) },
      { label: "Injuries/limitations", value: summarizeInjuries(hydratedProfile) },
    ],
    [hydratedProfile],
  );

  const savedFullName = (me?.profile.full_name ?? "").trim();
  const savedAvatarUrl = (me?.profile.avatar_url ?? "").trim();
  const hasUnsavedAccountChanges =
    isReady && (fullName.trim() !== savedFullName || avatarUrl.trim() !== savedAvatarUrl);
  const showFloatingSave = hasUnsavedAccountChanges && !isAccountActionsVisible;

  useEffect(() => {
    if (!me) {
      return;
    }
    setFullName(me.profile.full_name);
    setAppearanceMode(me.profile.appearance_mode ?? "dark");
    const storedAvatar = me.profile.avatar_url ?? "";
    setAvatarUrl(storedAvatar);
    if (!isDataAvatarImageUrl(storedAvatar)) {
      setUrlInputValue(storedAvatar);
    }
    setUsernameDraft(currentUsername);
  }, [currentUsername, me]);

  useEffect(() => {
    setProgrammeControls(readLocalJson(PROGRAMME_CONTROLS_STORAGE_KEY, DEFAULT_PROGRAMME_CONTROLS));
    setAdminTemplates(readLocalJson(ADMIN_TEMPLATES_STORAGE_KEY, DEFAULT_ADMIN_TEMPLATES));
  }, []);

  useEffect(() => {
    return () => {
      previewAppearanceMode(null);
    };
  }, [previewAppearanceMode]);

  // Keep direct links to a settings section working while showing one section
  // at a time. This avoids a long stack of unrelated forms on phones.
  useEffect(() => {
    if (!isReady) return;
    const syncHash = () => {
      const requested = decodeURIComponent(window.location.hash.slice(1));
      setActiveSection(sections.some((section) => section.id === requested) ? requested : sections[0].id);
    };
    syncHash();
    window.addEventListener("hashchange", syncHash);
    return () => window.removeEventListener("hashchange", syncHash);
  }, [isReady, sections]);

  function selectSection(id: string) {
    setActiveSection(id);
    window.history.replaceState(null, "", `#${id}`);
    document.querySelector(".settings-layout")?.scrollIntoView({ block: "start", behavior: "smooth" });
  }

  useEffect(() => {
    const node = accountActionsRef.current;
    if (!node || typeof IntersectionObserver === "undefined") {
      return;
    }
    const observer = new IntersectionObserver(
      ([entry]) => setIsAccountActionsVisible(entry.isIntersecting),
      { rootMargin: "0px 0px -96px 0px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [isReady]);

  // The device time zone is not something the athlete edits, so it cannot ride
  // along with Save: that button is disabled until the name or photo changes,
  // which would strand a travelled athlete on a stale zone — the one Today and
  // XP date the training day from. Persist a drift on its own instead, once per
  // detected value so a normalising server can't start a loop.
  useEffect(() => {
    const token = session?.access_token;
    const deviceTimeZone = detectDeviceTimeZone();
    if (!token || !me || !deviceTimeZone) {
      return;
    }
    if (deviceTimeZone === me.profile.athlete_timezone || syncedTimeZoneRef.current === deviceTimeZone) {
      return;
    }
    syncedTimeZoneRef.current = deviceTimeZone;
    updateMe(token, { athlete_timezone: deviceTimeZone })
      .then(replaceMe)
      .catch(() => {
        // Stay silent: the stored zone is untouched and the next visit retries.
        syncedTimeZoneRef.current = null;
      });
  }, [me, replaceMe, session?.access_token]);

  async function saveAppearanceMode(nextMode: AppearanceMode) {
    if (!session?.access_token) {
      return;
    }

    startTransition(async () => {
      try {
        const updatedMe = await updateMe(session.access_token, {
          appearance_mode: nextMode,
        });
        replaceMe(updatedMe);
        setMessage("Theme updated.");
      } catch (saveError) {
        setAppearanceMode(me?.profile.appearance_mode ?? "dark");
        setError(saveError instanceof Error ? saveError.message : "Unable to update settings.");
      }
    });
  }

  function handleSaveAccount() {
    if (!session?.access_token) {
      return;
    }
    if (isAvatarProcessing) {
      // Photo is still compressing; block the save so we don't persist the old
      // avatar under a preview that hasn't been committed yet.
      setError("Your photo is still processing. Please wait a moment.");
      return;
    }
    setMessage(null);
    setError(null);

    startTransition(async () => {
      try {
        const updatedMe = await updateMe(session.access_token, {
          full_name: fullName,
          athlete_timezone: detectDeviceTimeZone() || me?.profile.athlete_timezone || "",
          appearance_mode: appearanceMode,
          avatar_url: isSafeAvatarImageUrl(avatarUrl) ? avatarUrl.trim() : null,
        });
        replaceMe(updatedMe);
        setMessage("Account settings updated.");
      } catch (saveError) {
        setError(saveError instanceof Error ? saveError.message : "Unable to update settings.");
      }
    });
  }

  function handleUsernameSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session?.access_token) return;

    setUsernameError(null);
    setUsernameMessage(null);

    const next = usernameDraft.trim().toLowerCase();
    if (next === currentUsername.toLowerCase()) {
      setUsernameError("That is already your username.");
      return;
    }
    const validationError = validateUsernameClient(next);
    if (validationError) {
      setUsernameError(validationError);
      return;
    }
    if (usernameRemaining <= 0) {
      setUsernameError(
        nextAvailableLabel
          ? `Limit reached. You can change again on ${nextAvailableLabel}.`
          : `Limit reached. You can change your username up to ${usernameMax} times every ${usernameWindowDays} days.`,
      );
      return;
    }

    startUsernameTransition(async () => {
      try {
        const updated = await changeUsername(session.access_token, { username: next });
        replaceMe(updated);
        setUsernameMessage(currentUsername ? "Username updated." : "Username set.");
      } catch (err) {
        if (err instanceof ApiError) {
          setUsernameError(err.message);
        } else {
          setUsernameError(err instanceof Error ? err.message : "Unable to update username.");
        }
      }
    });
  }

  function handlePasswordSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPasswordError(null);
    setPasswordMessage(null);

    if (!currentPassword) {
      setPasswordError("Enter your current password.");
      return;
    }
    if (!newPassword || newPassword.length < 8) {
      setPasswordError("New password must be at least 8 characters.");
      return;
    }
    if (newPassword !== confirmPassword) {
      setPasswordError("New password and confirmation do not match.");
      return;
    }
    if (!passwordStrength.isAcceptable) {
      setPasswordError(passwordStrength.feedback || "Pick a stronger password.");
      return;
    }
    if (newPassword === currentPassword) {
      setPasswordError("New password must be different from your current password.");
      return;
    }
    const email = me?.profile.email;
    if (!email) {
      setPasswordError("Your account email is unavailable. Reload and try again.");
      return;
    }

    startPasswordTransition(async () => {
      let client;
      try {
        client = getSupabaseBrowserClient();
      } catch (clientError) {
        setPasswordError(clientError instanceof Error ? clientError.message : "Auth client unavailable.");
        return;
      }

      const { error: reauthError } = await client.auth.signInWithPassword({
        email,
        password: currentPassword,
      });
      if (reauthError) {
        setPasswordError("Current password is incorrect.");
        return;
      }

      const { error: updateError } = await client.auth.updateUser({ password: newPassword });
      if (updateError) {
        setPasswordError(updateError.message);
        return;
      }

      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setPasswordMessage("Password updated.");
    });
  }

  function handleFileChange(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    if (!file.type.startsWith("image/") || file.type === "image/svg+xml") {
      setError("Please choose a photo (JPEG, PNG, WebP, HEIC or GIF).");
      if (fileInputRef.current) fileInputRef.current.value = "";
      return;
    }
    if (file.size > MAX_AVATAR_FILE_BYTES) {
      setError("Image must be smaller than 25 MB. Please choose a smaller file.");
      if (fileInputRef.current) fileInputRef.current.value = "";
      return;
    }
    const requestId = ++avatarRequestRef.current;
    setError(null);
    setIsAvatarProcessing(true);
    compressAvatarImage(file)
      .then((dataUrl) => {
        // A newer selection has superseded this one — drop the stale result.
        if (requestId !== avatarRequestRef.current) return;
        setAvatarUrl(dataUrl);
        setUrlInputValue("");
        setError(null);
      })
      .catch(() => {
        if (requestId !== avatarRequestRef.current) return;
        setError("Couldn't process that image. Please try a different photo.");
      })
      .finally(() => {
        // Only the latest request clears the processing flag.
        if (requestId === avatarRequestRef.current) {
          setIsAvatarProcessing(false);
        }
        // Reset so re-selecting the same file fires another change event.
        if (fileInputRef.current) fileInputRef.current.value = "";
      });
  }

  function handleRemoveAvatar() {
    // Invalidate any in-flight compression so it can't repopulate the avatar.
    avatarRequestRef.current += 1;
    setAvatarUrl("");
    setUrlInputValue("");
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  }

  function handleDiscardAccountChanges() {
    // Invalidate any in-flight compression so it can't repopulate the avatar.
    avatarRequestRef.current += 1;
    const storedAvatar = me?.profile.avatar_url ?? "";
    setFullName(me?.profile.full_name ?? "");
    setAvatarUrl(storedAvatar);
    setUrlInputValue(isDataAvatarImageUrl(storedAvatar) ? "" : storedAvatar);
    setIsAvatarProcessing(false);
    setMessage(null);
    setError(null);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  }

  function updateProgrammeControls(next: ProgrammeControls) {
    setProgrammeControls(next);
    writeLocalJson(PROGRAMME_CONTROLS_STORAGE_KEY, next);
  }

  function updateAdminTemplate(key: keyof AdminTemplateDraft, value: string) {
    setAdminTemplates((current) => {
      const next = { ...current, [key]: value };
      writeLocalJson(ADMIN_TEMPLATES_STORAGE_KEY, next);
      return next;
    });
  }

  const usernameChangedFromCurrent =
    usernameDraft.trim().toLowerCase() !== currentUsername.toLowerCase() && usernameDraft.trim().length > 0;
  const usernameSubmitDisabled = isUsernamePending || !usernameChangedFromCurrent || usernameRemaining <= 0;

  function renderAvatarEditor() {
    const hasAvatar = avatarUrl.trim() !== "" && isSafeAvatarImageUrl(avatarUrl.trim());
    return (
      <>
        <input
          ref={fileInputRef}
          type="file"
          accept="image/*"
          className="avatar-file-input"
          aria-label="Upload profile photo"
          onChange={handleFileChange}
          disabled={isAvatarProcessing}
        />

        <div className="avatar-editor-row">
          <button
            type="button"
            className="avatar-upload-trigger"
            aria-label="Choose profile photo"
            onClick={() => fileInputRef.current?.click()}
            disabled={isAvatarProcessing}
          >
            <div className="avatar-upload-circle">
              {isSafeAvatarImageUrl(avatarUrl) ? (
                <img src={avatarUrl.trim()} alt="Profile" className="avatar-preview-img" />
              ) : (
                <span className="avatar-preview-initials">{initials}</span>
              )}
              <div className="avatar-upload-overlay" aria-hidden="true">
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                  <path
                    d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                  <circle
                    cx="12"
                    cy="13"
                    r="4"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </div>
            </div>
          </button>

          <div className="avatar-editor-actions">
            <button
              type="button"
              className="secondary-button avatar-upload-btn"
              onClick={() => fileInputRef.current?.click()}
              disabled={isAvatarProcessing}
            >
              {isAvatarProcessing ? "Processing photo…" : hasAvatar ? "Change photo" : "Upload photo"}
            </button>

            {hasAvatar ? (
              <button
                type="button"
                className="ghost-button danger-button"
                onClick={handleRemoveAvatar}
                disabled={isAvatarProcessing}
              >
                Remove
              </button>
            ) : null}

            <div className="field avatar-url-field">
              <label htmlFor="settingsAvatarUrl">Or paste image URL</label>
              <input
                id="settingsAvatarUrl"
                type="url"
                value={urlInputValue}
                onChange={(event) => {
                  setUrlInputValue(event.target.value);
                  setAvatarUrl(event.target.value);
                }}
                maxLength={AVATAR_URL_MAX}
                placeholder="https://example.com/photo.jpg"
                disabled={isAvatarProcessing}
              />
            </div>
          </div>
        </div>
      </>
    );
  }

  function renderAccountControls() {
    return (
      <>
        <div className="form-grid settings-identity-grid">
          <div className="field">
            <label htmlFor="settingsFullName">Name</label>
            <input
              id="settingsFullName"
              name="name"
              autoComplete="name"
              value={fullName}
              onChange={(event) => setFullName(event.target.value)}
              maxLength={ATHLETE_FULL_NAME_MAX}
            />
          </div>
          <div className="field">
            <label>Email</label>
            <div className="readonly-field">{me?.profile.email || "Unavailable"}</div>
          </div>
        </div>

        <details className="settings-disclosure">
          <summary>Security and sign-in <span>Date of birth, username, password</span></summary>
          <div className="settings-disclosure-body">
        <div className="settings-subsection">
          <div className="settings-subsection-header">
            <h3 className="settings-subsection-title">Date of birth</h3>
          </div>

          {isEditingDateOfBirth ? (
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void submitDateOfBirth();
              }}
            >
              <div className="field">
                <label htmlFor="settingsDateOfBirth">Date of birth</label>
                <input
                  id="settingsDateOfBirth"
                  type="date"
                  autoComplete="bday"
                  value={dateOfBirthDraft}
                  onChange={(event) => {
                    setDateOfBirthDraft(event.target.value);
                    setDateOfBirthError(null);
                    // Any edit invalidates a pending confirmation: the athlete
                    // must confirm the date they are actually saving.
                    setIsConfirmingDateOfBirth(false);
                  }}
                />
                <p className="muted">
                  Affects age-based training and safety recommendations.
                </p>
              </div>

              {isConfirmingDateOfBirth ? (
                <p className="warning-text" role="alert">
                  Confirm {formatAppDate(dateOfBirthDraft)} is your date of birth. Submit again to save it.
                </p>
              ) : null}
              {dateOfBirthError ? <p className="error-text">{dateOfBirthError}</p> : null}

              <div className="form-actions settings-subsection-actions">
                <button type="submit" className="cta" disabled={isDateOfBirthSaving}>
                  {isDateOfBirthSaving
                    ? "Saving..."
                    : isConfirmingDateOfBirth
                      ? "Confirm date of birth"
                      : "Update date of birth"}
                </button>
                <button type="button" className="ghost-button" onClick={cancelDateOfBirthEdit}>
                  Cancel
                </button>
              </div>
            </form>
          ) : (
            <>
              <div className="form-grid settings-identity-grid">
                <div className="field">
                  <label>Date of birth</label>
                  <div className="readonly-field" data-testid="settings-date-of-birth">
                    {savedDateOfBirth ? formatAppDate(savedDateOfBirth) : "Not set"}
                  </div>
                </div>
                <div className="field">
                  <label>Age</label>
                  <div className="readonly-field" data-testid="settings-derived-age">
                    {derivedAge ?? "Unavailable"}
                  </div>
                </div>
              </div>
              <p className="muted">
                Sets your age across UNLXCK, including safety rules. Change it only here.
              </p>
              {dateOfBirthMessage ? <p className="success-text">{dateOfBirthMessage}</p> : null}
              {dateOfBirthError ? <p className="error-text">{dateOfBirthError}</p> : null}
              <div className="form-actions settings-subsection-actions">
                <button type="button" className="ghost-button" onClick={beginDateOfBirthEdit}>
                  Change date of birth
                </button>
              </div>
            </>
          )}
        </div>

        <form className="settings-subsection" onSubmit={handleUsernameSubmit}>
          <div className="settings-subsection-header">
            <h3 className="settings-subsection-title">Username</h3>
            <span
              className={`badge ${usernameRemaining > 0 ? "status-badge-neutral" : "status-badge-danger"}`}
              aria-live="polite"
            >
              {usernameRemaining} of {usernameMax} changes left
            </span>
          </div>

          <div className="field settings-username-field">
            <label htmlFor="settingsUsername">Handle</label>
            <div className="settings-username-input">
              <span className="settings-username-prefix" aria-hidden="true">@</span>
              <input
                id="settingsUsername"
                name="username"
                autoComplete="username"
                value={usernameDraft}
                onChange={(event) => {
                  setUsernameDraft(event.target.value);
                  setUsernameError(null);
                  setUsernameMessage(null);
                }}
                placeholder="your-fight-handle"
                minLength={USERNAME_MIN}
                maxLength={USERNAME_MAX}
                spellCheck={false}
                autoCapitalize="off"
              />
            </div>
            {usernameRemaining === 0 && nextAvailableLabel ? (
              <p className="warning-text">Next change available on {nextAvailableLabel}.</p>
            ) : null}
            {usernameError ? <p className="error-text">{usernameError}</p> : null}
            {usernameMessage ? <p className="success-text">{usernameMessage}</p> : null}
          </div>

          <div className="form-actions settings-subsection-actions">
            <button type="submit" className="cta" disabled={usernameSubmitDisabled}>
              {isUsernamePending ? "Saving..." : currentUsername ? "Update username" : "Set username"}
            </button>
            {currentUsername && usernameDraft.trim().toLowerCase() !== currentUsername.toLowerCase() ? (
              <button
                type="button"
                className="ghost-button"
                onClick={() => {
                  setUsernameDraft(currentUsername);
                  setUsernameError(null);
                  setUsernameMessage(null);
                }}
              >
                Cancel
              </button>
            ) : null}
          </div>
        </form>

        <form className="settings-subsection" onSubmit={handlePasswordSubmit}>
          <div className="settings-subsection-header">
            <h3 className="settings-subsection-title">Password</h3>
            <button
              type="button"
              className="password-toggle settings-password-toggle"
              onClick={() => setShowPasswords((prev) => !prev)}
              aria-pressed={showPasswords}
            >
              {showPasswords ? "Hide" : "Show"}
            </button>
          </div>

          <div className="form-grid settings-password-grid">
            <div className="field">
              <label htmlFor="settingsCurrentPassword">Current password</label>
              <input
                id="settingsCurrentPassword"
                type={showPasswords ? "text" : "password"}
                autoComplete="current-password"
                value={currentPassword}
                onChange={(event) => setCurrentPassword(event.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="settingsNewPassword">New password</label>
              <input
                id="settingsNewPassword"
                type={showPasswords ? "text" : "password"}
                autoComplete="new-password"
                minLength={8}
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
              />
              {newPassword ? <PasswordStrengthMeter strength={passwordStrength} /> : null}
            </div>
            <div className="field">
              <label htmlFor="settingsConfirmPassword">Confirm new password</label>
              <input
                id="settingsConfirmPassword"
                type={showPasswords ? "text" : "password"}
                autoComplete="new-password"
                minLength={8}
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
              />
            </div>
          </div>

          {passwordError ? <p className="error-text">{passwordError}</p> : null}
          {passwordMessage ? <p className="success-text">{passwordMessage}</p> : null}

          <div className="form-actions settings-subsection-actions">
            <button
              type="submit"
              className="cta"
              disabled={isPasswordPending || !currentPassword || !newPassword || !confirmPassword}
            >
              {isPasswordPending ? "Updating..." : "Update password"}
            </button>
            <Link href="/forgot-password" className="ghost-button">
              Forgot password?
            </Link>
          </div>
        </form>

          </div>
        </details>

        <div className="settings-subsection">
          <div className="settings-subsection-header">
            <h3 className="settings-subsection-title">Appearance</h3>
          </div>
          <div className="appearance-mode-grid" role="radiogroup" aria-label="Workspace theme">
            {APPEARANCE_OPTIONS.map((option) => {
              const isSelected = appearanceMode === option.value;
              return (
                <button
                  key={option.value}
                  type="button"
                  role="radio"
                  aria-checked={isSelected}
                  className={`appearance-mode-card ${isSelected ? "appearance-mode-card-active" : ""}`.trim()}
                  onClick={async () => {
                    if (appearanceMode === option.value || isPending) {
                      return;
                    }
                    setAppearanceMode(option.value);
                    previewAppearanceMode(option.value);
                    await saveAppearanceMode(option.value);
                  }}
                >
                  <span className={`appearance-mode-preview appearance-mode-preview-${option.value}`} aria-hidden="true">
                    <span className="appearance-mode-preview-header" />
                    <span className="appearance-mode-preview-panel" />
                    <span className="appearance-mode-preview-line" />
                    <span className="appearance-mode-preview-line appearance-mode-preview-line-short" />
                  </span>
                  <span className="appearance-mode-copy">
                    <span className="appearance-mode-title-row">
                      <span className="appearance-mode-title">{option.label}</span>
                    </span>
                    <span className="appearance-mode-description">{option.description}</span>
                  </span>
                </button>
              );
            })}
          </div>
        </div>

        <InstallUnlxck />

        <div ref={accountActionsRef} className="settings-account-actions">
          <p className="settings-account-actions-status" aria-live="polite">
            {hasUnsavedAccountChanges ? "Unsaved changes to your name or photo." : `Last saved ${lastUpdatedLabel}`}
          </p>
          <div className="form-actions settings-account-actions-buttons">
            {hasUnsavedAccountChanges ? (
              <button type="button" className="ghost-button" onClick={handleDiscardAccountChanges} disabled={isPending}>
                Discard
              </button>
            ) : null}
            <button
              type="button"
              className="cta"
              onClick={handleSaveAccount}
              disabled={isPending || isAvatarProcessing || !hasUnsavedAccountChanges}
            >
              {isAvatarProcessing ? "Processing photo…" : isPending ? "Saving..." : "Save account"}
            </button>
          </div>
        </div>
      </>
    );
  }

  function renderAthleteSettings() {
    return (
      <div className="settings-stack athlete-motion-slot athlete-motion-main">
        <article id="account" className="step-card settings-card" hidden={activeSection !== "account"}>
          <div className="form-section-header">
            <p className="kicker">Account</p>
            <h2 className="form-section-title">Profile and sign-in</h2>
          </div>
          {renderAvatarEditor()}
          {renderAccountControls()}
        </article>

        <article id="training-profile" className="step-card settings-card" hidden={activeSection !== "training-profile"}>
          <div className="settings-card-header-row">
            <div className="form-section-header">
              <p className="kicker">Training Profile</p>
              <h2 className="form-section-title">Current athlete context</h2>
            </div>
            <Link href="/onboarding?mode=edit" className="cta">
              Update Training Profile
            </Link>
          </div>
          <div className="settings-profile-summary-grid">
            {trainingProfileSummary.map((item) => (
              <SettingsSummaryItem key={item.label} label={item.label} value={item.value} />
            ))}
          </div>
        </article>

        <article id="notifications" className="step-card settings-card" hidden={activeSection !== "notifications"}>
          <div className="form-section-header">
            <p className="kicker">Notifications</p>
            <h2 className="form-section-title">Reminders and messages</h2>
          </div>
          {session?.access_token ? (
            <PushNotificationSettings token={session.access_token} />
          ) : (
            <p className="muted">Sign in again to manage notification preferences.</p>
          )}
        </article>

        <article id="subscription" className="step-card settings-card" hidden={activeSection !== "subscription"}>
          <div className="form-section-header">
            <p className="kicker">Subscription</p>
            <h2 className="form-section-title">Plan and billing access</h2>
          </div>
          <div className="settings-profile-summary-grid">
            <SettingsSummaryItem label="Current plan" value="Beta athlete access" />
            <SettingsSummaryItem label="Payment method" value="Not connected" />
            <SettingsSummaryItem label="Invoices" value="No invoices yet" />
          </div>
          <p className="settings-coming-soon">Billing arrives with subscriptions.</p>
        </article>

        <article id="privacy" className="step-card settings-card" hidden={activeSection !== "privacy"}>
          <AnalyticsPreference />
          <div className="form-section-header">
            <p className="kicker">Privacy</p>
            <h2 className="form-section-title">Data and consent</h2>
          </div>
          {/* "Account required data only" was not true: UNLXCK processes
              injuries, readiness and bodyweight, which is special-category
              health data. The summary now states the actual consent record. */}
          <div className="settings-profile-summary-grid">
            <SettingsSummaryItem label="Terms of Use" value={termsSummary(me)} />
            <SettingsSummaryItem label="Health-data consent" value={healthConsentSummary(me)} />
            <SettingsSummaryItem label="Last profile update" value={lastUpdatedLabel} />
            <SettingsSummaryItem label="Detected time zone" value={detectedTimeZone} />
          </div>
          <p className="muted">{consentCopy.privacySummary}</p>
          <ul className="summary-list">
            <li>
              <Link href={PRIVACY_HREF} className="auth-text-link">
                Privacy Notice
              </Link>
            </li>
            <li>
              <Link href={TERMS_HREF} className="auth-text-link">
                Terms of Use
              </Link>
            </li>
          </ul>
          {privacyMessage ? (
            <div className="success-banner" role="status">
              {privacyMessage}
            </div>
          ) : null}
          {privacyError ? (
            <div className="error-banner" role="alert">
              {privacyError}
            </div>
          ) : null}
          {healthConsentGranted ? (
            <p className="muted">
              Withdrawing stops UNLXCK using your health information for new guidance. Existing plans stay readable. You can consent again any time.
            </p>
          ) : (
            <p className="muted">{consentCopy.declineNote}</p>
          )}
          <div className="plan-summary-actions settings-action-row">
            <button
              type="button"
              className="ghost-button"
              onClick={() => void toggleHealthConsent(!healthConsentGranted)}
              disabled={isConsentSaving}
            >
              {isConsentSaving
                ? "Saving…"
                : healthConsentGranted
                  ? "Withdraw health-data consent"
                  : "Give health-data consent"}
            </button>
            {deletionRequestHref ? (
              <a className="ghost-button" href={deletionRequestHref}>
                Request account & data deletion
              </a>
            ) : null}
            <button type="button" className="ghost-button" onClick={() => void signOut()}>
              Sign out
            </button>
          </div>
          {deletionRequestHref ? null : (
            <p className="muted">
              To access your data or delete your account, use Send feedback below. We’ll action it under our retention policy.
            </p>
          )}
        </article>

        {/* The same briefing shown at sign-up, kept readable for the whole
            trial: testers ask what they are supposed to be checking weeks after
            the one-time screen. */}
        <article id="private-trial" className="step-card settings-card" hidden={activeSection !== "private-trial"}>
          <div className="form-section-header">
            <p className="kicker">Private trial</p>
            <h2 className="form-section-title" id="settings-private-trial-heading">
              Private Trial Guide
            </h2>
          </div>
          <PrivateTrialGuide headingId="settings-private-trial-heading" showTitle={false} />
        </article>

        <article id="send-feedback" className="step-card settings-card" hidden={activeSection !== "send-feedback"}>
          <div className="form-section-header">
            <p className="kicker">Beta feedback</p>
            <h2 className="form-section-title">Send feedback</h2>
          </div>
          <p className="muted">Report a bug, request a feature, flag a safety issue or share feedback.</p>
          {session?.access_token ? <GlobalFeedback token={session.access_token} /> : null}
        </article>
      </div>
    );
  }

  function renderAdminSettings() {
    return (
      <div className="settings-stack athlete-motion-slot athlete-motion-main">
        <article id="admin-account" className="step-card settings-card" hidden={activeSection !== "admin-account"}>
          <div className="form-section-header">
            <p className="kicker">Admin Account</p>
            <h2 className="form-section-title">Profile and sign-in</h2>
          </div>
          {renderAvatarEditor()}
          {renderAccountControls()}
        </article>

        <article id="organisation" className="step-card settings-card" hidden={activeSection !== "organisation"}>
          <div className="form-section-header">
            <p className="kicker">Organisation</p>
            <h2 className="form-section-title">Branding and contact defaults</h2>
          </div>
          <div className="settings-profile-summary-grid">
            <SettingsSummaryItem label="Brand name" value="Not connected" />
            <SettingsSummaryItem label="Logo" value="Not connected" />
            <SettingsSummaryItem label="Contact email" value="Not connected" />
          </div>
          <p className="settings-coming-soon">Organisation settings appear once organisation records are connected.</p>
        </article>

        <article id="coaches-roles" className="step-card settings-card" hidden={activeSection !== "coaches-roles"}>
          <div className="form-section-header">
            <p className="kicker">Coaches & Roles</p>
            <h2 className="form-section-title">Access control</h2>
          </div>
          <div className="settings-profile-summary-grid">
            <SettingsSummaryItem label="Primary admin" value={me?.profile.email || "Unavailable"} />
            <SettingsSummaryItem label="Role" value="Admin" />
            <SettingsSummaryItem label="Coach seats" value="Not configured" />
          </div>
          <p className="settings-coming-soon">Coach invites and roles arrive with team management.</p>
        </article>

        <article id="programme-controls" className="step-card settings-card" hidden={activeSection !== "programme-controls"}>
          <div className="form-section-header">
            <p className="kicker">Programme Defaults</p>
            <h2 className="form-section-title">Coming soon</h2>
          </div>
          <p className="settings-coming-soon">Preview only. These don&apos;t change your plans yet.</p>
          <div className="settings-control-grid">
            <div className="settings-subsection">
              <div className="settings-subsection-header">
                <h3 className="settings-subsection-title">Injury filtering</h3>
                <span className="badge status-badge-neutral">{programmeControls.injuryFiltering}</span>
              </div>
              <div className="settings-segmented-control" role="radiogroup" aria-label="Injury filtering">
                {(["light", "strict"] as const).map((value) => (
                  <button
                    key={value}
                    type="button"
                    className={programmeControls.injuryFiltering === value ? "settings-segment-active" : ""}
                    onClick={() => updateProgrammeControls({ ...programmeControls, injuryFiltering: value })}
                  >
                    {value}
                  </button>
                ))}
              </div>
            </div>
            <div className="settings-subsection">
              <div className="settings-subsection-header">
                <h3 className="settings-subsection-title">Fatigue adjustment</h3>
                <span className="badge status-badge-neutral">{programmeControls.fatigueAdjustment}</span>
              </div>
              <div className="settings-segmented-control" role="radiogroup" aria-label="Fatigue adjustment">
                {(["light", "strict"] as const).map((value) => (
                  <button
                    key={value}
                    type="button"
                    className={programmeControls.fatigueAdjustment === value ? "settings-segment-active" : ""}
                    onClick={() => updateProgrammeControls({ ...programmeControls, fatigueAdjustment: value })}
                  >
                    {value}
                  </button>
                ))}
              </div>
            </div>
            <label className="settings-toggle-row">
              <span>
                <span className="settings-toggle-title">Require coach review for risky athletes</span>
                <span className="settings-toggle-detail">Manual review gate</span>
              </span>
              <input
                type="checkbox"
                checked={programmeControls.requireCoachReview}
                onChange={(event) =>
                  updateProgrammeControls({ ...programmeControls, requireCoachReview: event.target.checked })
                }
              />
            </label>
            <label className="settings-toggle-row">
              <span>
                <span className="settings-toggle-title">Auto-generate plans</span>
                <span className="settings-toggle-detail">Use latest completed intake</span>
              </span>
              <input
                type="checkbox"
                checked={programmeControls.autoGeneratePlans}
                onChange={(event) =>
                  updateProgrammeControls({ ...programmeControls, autoGeneratePlans: event.target.checked })
                }
              />
            </label>
          </div>
        </article>

        <article id="templates-billing" className="step-card settings-card" hidden={activeSection !== "templates-billing"}>
          <div className="form-section-header">
            <p className="kicker">Templates & Billing</p>
            <h2 className="form-section-title">Reusable copy, layouts, and payments</h2>
          </div>
          <div className="form-grid settings-template-grid">
            <div className="field">
              <label htmlFor="settingsWelcomeMessage">Welcome message</label>
              <textarea
                id="settingsWelcomeMessage"
                value={adminTemplates.welcomeMessage}
                onChange={(event) => updateAdminTemplate("welcomeMessage", event.target.value)}
                rows={3}
              />
            </div>
            <div className="field">
              <label htmlFor="settingsPlanEmail">Plan email</label>
              <textarea
                id="settingsPlanEmail"
                value={adminTemplates.planEmail}
                onChange={(event) => updateAdminTemplate("planEmail", event.target.value)}
                rows={3}
              />
            </div>
            <div className="field">
              <label htmlFor="settingsCoachNotes">Coach notes</label>
              <textarea
                id="settingsCoachNotes"
                value={adminTemplates.coachNotes}
                onChange={(event) => updateAdminTemplate("coachNotes", event.target.value)}
                rows={3}
              />
            </div>
          </div>
          <p className="settings-coming-soon">Template changes stay local until template storage is connected.</p>
          <div className="settings-profile-summary-grid">
            <SettingsSummaryItem label="Plan" value="Admin beta access" />
            <SettingsSummaryItem label="Active users" value="Not connected" />
            <SettingsSummaryItem label="Failed payments" value="None" />
          </div>
          <div className="plan-summary-actions settings-action-row">
            <button type="button" className="ghost-button" onClick={() => void signOut()}>
              Sign out
            </button>
          </div>
          <p className="settings-coming-soon">Billing arrives with subscriptions.</p>
        </article>

        <article id="send-feedback" className="step-card settings-card" hidden={activeSection !== "send-feedback"}>
          <div className="form-section-header">
            <p className="kicker">Beta feedback</p>
            <h2 className="form-section-title">Send feedback</h2>
          </div>
          <p className="muted">Report a bug, request a feature, flag a safety issue or share feedback.</p>
          {session?.access_token ? <GlobalFeedback token={session.access_token} /> : null}
        </article>
      </div>
    );
  }

  if (!isMeHydrated || !me) {
    return (
      <RequireAuth>
        <section className="panel loading-card" aria-busy="true">
          <p className="kicker">{t("settings")}</p>
          <h1>{t("loadingSettings")}</h1>
          <p className="muted">{t("restoringProfile")}</p>
        </section>
      </RequireAuth>
    );
  }

  return (
    <RequireAuth>
      <section className="panel settings-page">
        <div className="section-heading">
          <div className="athlete-motion-slot athlete-motion-header">
            <p className="kicker">{t("settings")}</p>
            <h1>{isAdmin ? t("adminSettings") : t("athleteSettings")}</h1>
            <p className="muted">
              {isAdmin ? t("adminSettingsSummary") : t("athleteSettingsSummary")}
            </p>
            {/* Account controls that the phone avatar button leads here for. */}
            <div className="sidebar-user-actions settings-quick-account">
              <LanguageSwitcher placement="inline" />
              {shouldShowAdminPanelLink(me?.profile.role) ? (
                <Link href="/admin" className="ghost-button">{tNav("adminPanel")}</Link>
              ) : null}
              <button type="button" className="ghost-button" onClick={() => void signOut()}>
                {tNav("signOut")}
              </button>
            </div>
          </div>
          <div
            className={`status-card athlete-motion-slot athlete-motion-status${isAdmin ? "" : " settings-sync-card"}`}
          >
            <p className="status-label">{isAdmin ? t("adminProfile") : t("profileSync")}</p>
            <h2 className="plan-summary-title">{isAdmin ? professionalStatusLabel : t("savedAccount")}</h2>
            <p className="muted">{isAdmin ? me?.profile.email || t("unavailable") : t("lastUpdated", { time: lastUpdatedLabel })}</p>
          </div>
        </div>

        <div className="settings-layout">
          <SettingsNav sections={sections} activeSection={activeSection} onSelect={selectSection} />

          <div className="settings-main">
            <div aria-live="polite">
              {message ? <div className="success-banner">{message}</div> : null}
              {error ? <div className="error-banner">{error}</div> : null}
            </div>

            {isAdmin ? renderAdminSettings() : renderAthleteSettings()}
          </div>
        </div>

        {showFloatingSave ? (
          <div className="settings-save-bar" role="group" aria-label={t("unsavedChanges")}>
            <p className="settings-save-bar-text">{t("unsavedChanges")}</p>
            <div className="settings-save-bar-actions">
              <button type="button" className="ghost-button" onClick={handleDiscardAccountChanges} disabled={isPending}>
                {t("discard")}
              </button>
              <button
                type="button"
                className="cta"
                onClick={handleSaveAccount}
                disabled={isPending || isAvatarProcessing}
              >
                {isAvatarProcessing ? t("processingPhoto") : isPending ? t("saving") : t("saveAccount")}
              </button>
            </div>
          </div>
        ) : null}
      </section>
    </RequireAuth>
  );
}
