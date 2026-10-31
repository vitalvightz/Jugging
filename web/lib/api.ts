import { completeApiMeasurement, measuredApiFetch } from "./api-measurements";
import { beginSharedReadWrite, sharedRead } from "./shared-reads";
import {
  OFFLINE_NETWORK_MESSAGE,
  RETRYABLE_NETWORK_MESSAGE,
  isBrowserOffline,
  reportNetworkFailure,
} from "@/lib/connectivity";
import type { ExternalTrainingFields, TrainingHistoryPage, TrainingHistorySession } from "@/lib/training-history";
import type { SessionDetails, ExerciseLibraryResponse, ExercisePerformancesResponse } from "@/lib/api-schema.generated";
import type { ManualExerciseEntry, ManualExerciseListResponse, PerformedExerciseInput } from "@/lib/api-schema.generated";
import type { LibraryFeaturedResponse, LibraryPreview, LibrarySearchResponse } from "@/lib/api-schema.generated";
import type { CustomDoseSuggestion, CustomPlan, CustomPlanInput } from "@/lib/api-schema.generated";
import type { PrivateMediaPreview, PrivateMediaSettings, UploadPermission, UploadState, YouTubeClipInput } from "@/lib/api-schema.generated";
import type {
  ApproveAndResumeGenerationRequest,
  AdminAthleteRecord,
  AdminTimeToFirstPlanSummary,
  AdminLatestIntakeUpdateRequest,
  AdminGenerationJobDiagnostic,
  AdminFeedbackRecord,
  AdminFeedbackScreenshotAccess,
  AdminPlanSummary,
  AdminReviewRecord,
  AdminReviewResolveRequest,
  ComplianceAcceptanceRequest,
  ExerciseLogBatchRequest,
  ExerciseLogBatchResponse,
  ExerciseHistoryResponse,
  ExerciseLogListResponse,
  ExerciseLogRequest,
  ExerciseLogResponse,
  InjuryFlagCreateRequest,
  InjuryFlagRecord,
  InjuryFlagStatus,
  ManualStage2SubmissionRequest,
  GenerationJobResponse,
  ContextualFeedbackRequest,
  FeedbackRecord,
  GlobalFeedbackRequest,
  MeResponse,
  NutritionWorkspaceState,
  NutritionWorkspaceUpdateRequest,
  PlanCompletionsResponse,
  PlanDetail,
  PlanRequest,
  PlanSummary,
  ProfileUpdateRequest,
  RehabResponseRequest,
  RehabResponseResult,
  SessionFeedbackRequest,
  TodayCheckinHistoryRecord,
  TodayCheckinRequest,
  TodayCheckinResponse,
  TodayCommandView,
  ExerciseChangeRequest,
  ExerciseChangeResult,
  ExerciseOptionsResponse,
  TodayInjuryCheckinRequest,
  TodayInjuryCheckinResponse,
  TodaySessionCompletionRecord,
  PendingRehabResponsesResponse,
  SparringLogRequest,
  SparringLogHistoryResponse,
  SparringLogResponse,
  TodaySessionCompletionRequest,
  TodaySessionCompletionResponse,
  UsernameChangeRequest,
} from "@/lib/types";
import type { ActivePlanOverlapAction } from "@/lib/plan-active";
import { parseXpAwardResponse, type XpAwardResult } from "@/lib/xp";
import { requestXpRefresh } from "@/lib/xp-events";

const EXPLICIT_API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? null;
const LOCAL_API_BASE_URL = "http://127.0.0.1:8000";
const DEFAULT_API_REQUEST_TIMEOUT_MS = 60_000;

export function getPrivateMediaSettings(token: string, signal?: AbortSignal) {
  return readJson<PrivateMediaSettings>("/api/private-media/settings", { token, signal });
}
export function getPrivateMedia(token: string, id: string, signal?: AbortSignal) {
  return readJson<PrivateMediaPreview>(`/api/private-media/${encodeURIComponent(id)}`, { token, signal });
}
export function createPrivateYouTubeClip(token: string, body: YouTubeClipInput, signal?: AbortSignal) {
  return readJson<PrivateMediaPreview>("/api/private-media/youtube", { token, method: "POST", body: JSON.stringify(body), signal });
}
export function reservePrivateUpload(token: string, file: File, signal?: AbortSignal) {
  return readJson<UploadPermission>("/api/private-media/uploads", {
    token, method: "POST", body: JSON.stringify({ size_bytes: file.size, content_type: file.type }), signal,
  });
}
export function getPrivateUpload(token: string, id: string, signal?: AbortSignal) {
  return readJson<UploadState>(`/api/private-media/uploads/${encodeURIComponent(id)}`, { token, signal });
}

export function searchTrainingLibrary(token: string, params: URLSearchParams, signal?: AbortSignal) {
  return readJson<LibrarySearchResponse>(`/api/training/library?${params}`, { token, signal });
}

export function getTrainingLibraryEntry(token: string, catalogueId: string, signal?: AbortSignal) {
  const params = new URLSearchParams({ catalogue_id: catalogueId });
  return readJson<LibraryPreview>(`/api/training/library/entry?${params}`, { token, signal });
}

export function getFeaturedDrill(token: string, signal?: AbortSignal) {
  return readJson<LibraryFeaturedResponse>("/api/training/library/featured", { token, signal });
}

export function listCustomPlans(token: string, signal?: AbortSignal) {
  return readJson<CustomPlan[]>("/api/custom-plans", { token, signal });
}

export function getCustomPlan(token: string, id: string) {
  return readJson<CustomPlan>(`/api/custom-plans/${encodeURIComponent(id)}`, { token });
}

export function publishCustomPlan(token: string, id: string) {
  return readJson<PlanSummary>(`/api/custom-plans/${encodeURIComponent(id)}/open-plan`, {
    token, method: "POST",
  }).then(row => { requestXpRefresh(); return row; });
}

export function saveCustomPlan(token: string, body: CustomPlanInput, id?: string) {
  return readJson<CustomPlan>(id ? `/api/custom-plans/${encodeURIComponent(id)}` : "/api/custom-plans", {
    token, method: id ? "PUT" : "POST", body: JSON.stringify(body),
  });
}

export function deleteCustomPlan(token: string, id: string) {
  return requestVoid(`/api/custom-plans/${encodeURIComponent(id)}`, { token, method: "DELETE" });
}

function getApiBaseUrl(): string {
  if (typeof window !== "undefined") {
    // Keep browser requests same-origin so Next.js rewrites can proxy /api calls.
    // This avoids direct cross-origin calls that can fail due to CORS/SSL mismatches.
    return "";
  }

  if (EXPLICIT_API_BASE_URL) {
    return EXPLICIT_API_BASE_URL;
  }

  if (process.env.NODE_ENV !== "production") {
    return LOCAL_API_BASE_URL;
  }

  throw new Error("NEXT_PUBLIC_API_BASE_URL must be set for server-side API calls in production.");
}

type ApiRequestInit = RequestInit & {
  token?: string | null;
  clientRequestId?: string | null;
  planSource?: string | null;
};

/**
 * Error thrown for non-2xx HTTP responses. Includes the HTTP `status` code and,
 * when the backend supplies one, a stable machine-readable `code` (e.g.
 * `generation_already_in_flight`). Prefer branching on `code` over matching the
 * human-readable `message`, which is free to change.
 */
export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly code?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const RETRYABLE_GATEWAY_STATUSES = new Set([502, 503, 504]);
const RETRYABLE_INTERNAL_ERROR_SNIPPETS = [
  "failed to ensure profile",
  "profile service temporarily unavailable",
  "store service temporarily unavailable",
];
export { OFFLINE_NETWORK_MESSAGE, RETRYABLE_NETWORK_MESSAGE } from "@/lib/connectivity";
const meRequestsByToken = new Map<string, Promise<MeResponse>>();
const meUpdatesByToken = new Map<string, Promise<MeResponse>>();

function getApiRequestTimeoutMs(): number {
  const rawValue = process.env.NEXT_PUBLIC_API_REQUEST_TIMEOUT_MS?.trim();
  if (!rawValue) {
    return DEFAULT_API_REQUEST_TIMEOUT_MS;
  }
  const parsed = Number.parseInt(rawValue, 10);
  if (!Number.isFinite(parsed)) {
    return DEFAULT_API_REQUEST_TIMEOUT_MS;
  }
  return Math.max(0, parsed);
}

function looksLikeHtmlErrorPage(contentType: string, body: string): boolean {
  return contentType.includes("text/html") || /^<!doctype html/i.test(body);
}

function extractHtmlRequestId(body: string): string | null {
  const match = body.match(/Request ID:\s*([A-Za-z0-9-]+)/i);
  return match?.[1] ?? null;
}

function formatGatewayErrorMessage(status: number, requestId: string | null): string {
  const baseMessage =
    status === 502
      ? "The plan service is temporarily unavailable. Please try again in a minute."
      : "The plan service is taking longer than expected. Please try again in a minute.";
  return requestId ? `${baseMessage} (request id: ${requestId})` : baseMessage;
}

function buildPlainTextErrorMessage(params: {
  status: number;
  contentType: string;
  trimmedText: string;
  headerRequestId: string | null;
}): string {
  const { status, contentType, trimmedText, headerRequestId } = params;

  if (looksLikeHtmlErrorPage(contentType, trimmedText)) {
    const requestId = headerRequestId ?? extractHtmlRequestId(trimmedText);
    return formatGatewayErrorMessage(status, requestId);
  }

  return headerRequestId
    ? `${trimmedText || `Request failed: ${status}`} (request id: ${headerRequestId})`
    : trimmedText || `Request failed: ${status}`;
}

export function isRetryableApiFailure(error: unknown): boolean {
  if (error instanceof ApiError) {
    if (RETRYABLE_GATEWAY_STATUSES.has(error.status)) {
      return true;
    }
    return (
      error.status >= 500 &&
      RETRYABLE_INTERNAL_ERROR_SNIPPETS.some((snippet) =>
        error.message.toLowerCase().includes(snippet),
      )
    );
  }
  return (
    error instanceof Error &&
    (error.message === RETRYABLE_NETWORK_MESSAGE || error.message === OFFLINE_NETWORK_MESSAGE)
  );
}

async function sleep(ms: number): Promise<void> {
  await new Promise((resolve) => globalThis.setTimeout(resolve, ms));
}

function createClientRequestId(prefix: string): string {
  const randomId =
    globalThis.crypto?.randomUUID?.() ??
    `${Date.now()}_${Math.random().toString(16).slice(2)}`;

  return `${prefix}_${randomId}`;
}

function shouldLogApiDetails(): boolean {
  // Detailed API logging can include raw response bodies, which may contain
  // athlete or plan data. Never enable it in production browser logs,
  // regardless of NEXT_PUBLIC_API_DEBUG.
  if (process.env.NODE_ENV === "production") {
    return false;
  }
  // Outside production, keep the existing local/dev debugging behaviour:
  // detailed logging is on by default. NEXT_PUBLIC_API_DEBUG can be set to
  // "false" to silence it locally.
  return process.env.NEXT_PUBLIC_API_DEBUG !== "false";
}

async function withTransientRetries<T>(
  operation: () => Promise<T>,
  {
    attempts = 3,
    delayMs = 1500,
    signal,
  }: {
    attempts?: number;
    delayMs?: number;
    // A caller abort surfaces as the retryable network error; it must end the
    // request, not start another attempt.
    signal?: AbortSignal | null;
  } = {},
): Promise<T> {
  let lastError: unknown;

  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return await operation();
    } catch (error) {
      lastError = error;
      // Offline: retrying in-place only holds a spinner up for nothing. Fail
      // now; screens and the connection banner recover on reconnect.
      if (
        signal?.aborted ||
        !isRetryableApiFailure(error) ||
        attempt === attempts ||
        isBrowserOffline()
      ) {
        throw error;
      }
      await sleep(delayMs * attempt);
      if (signal?.aborted) {
        throw error;
      }
    }
  }

  throw lastError instanceof Error ? lastError : new Error("Request failed.");
}

function truncateForLog(value: string, max = 1200): string {
  return value.length > max ? `${value.slice(0, max)}…[truncated]` : value;
}

type ExecutedRequest = {
  response: Response;
  path: string;
  method: string;
  url: string;
  durationMs: number;
  contentType: string;
  requestId: string | null;
};

// Product analytics events change no training, plan or history data.
function isNeutralWrite(path: string): boolean {
  return path.startsWith("/api/analytics/");
}

async function executeRequest(path: string, init?: ApiRequestInit): Promise<ExecutedRequest> {
  const method = init?.method ?? "GET";
  // Any other write ends request sharing until it settles (see shared-reads.ts).
  const endWrite = method === "GET" || method === "HEAD" || isNeutralWrite(path) ? null : beginSharedReadWrite();
  try {
    return await sendRequest(path, method, init);
  } finally {
    endWrite?.();
  }
}

async function sendRequest(path: string, method: string, init?: ApiRequestInit): Promise<ExecutedRequest> {
  // Telemetry delivery is not evidence that training/API connectivity is degraded.
  const backgroundAnalytics = path === "/api/analytics/events";
  const headers = new Headers(init?.headers ?? {});
  if (init?.body && !(typeof FormData !== "undefined" && init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (init?.token) {
    headers.set("Authorization", `Bearer ${init.token}`);
  }
  if (init?.clientRequestId) {
    headers.set("X-Client-Request-Id", init.clientRequestId);
  }
  if (init?.planSource) {
    headers.set("X-Plan-Source", init.planSource);
  }

  const url = `${getApiBaseUrl()}${path}`;
  const startedAt = Date.now();
  const timeoutMs = getApiRequestTimeoutMs();
  const abortController = new AbortController();
  let timeoutId: ReturnType<typeof globalThis.setTimeout> | null = null;
  const abortFromCaller = () => abortController.abort();

  if (init?.signal?.aborted) {
    abortController.abort();
  } else {
    init?.signal?.addEventListener("abort", abortFromCaller, { once: true });
  }

  if (shouldLogApiDetails()) {
    console.info("[api] request:start", {
      path,
      method,
      url,
      hasBody: Boolean(init?.body),
      hasToken: Boolean(init?.token),
      startedAtIso: new Date(startedAt).toISOString(),
    });
  }

  let response: Response;
  try {
    if (timeoutMs > 0) {
      timeoutId = globalThis.setTimeout(() => abortController.abort(), timeoutMs);
    }
    response = await measuredApiFetch(path, url, {
      ...init,
      cache: "no-store",
      headers,
      signal: abortController.signal,
    });
  } catch (networkError) {
    // Search and sheet unmounts cancel superseded reads deliberately. Preserve
    // AbortError instead of reporting a network failure or opening a dev overlay.
    if (init?.signal?.aborted) throw networkError;
    const durationMs = Date.now() - startedAt;
    if (shouldLogApiDetails()) {
      console.error("[api] request:network_error", {
        path,
        method,
        url,
        durationMs,
        online: typeof navigator !== "undefined" ? navigator.onLine : "unknown",
        error:
          networkError instanceof Error
            ? { name: networkError.name, message: networkError.message, stack: networkError.stack }
            : networkError,
      });
    }
    if (!backgroundAnalytics) reportNetworkFailure();
    throw new Error(isBrowserOffline() ? OFFLINE_NETWORK_MESSAGE : RETRYABLE_NETWORK_MESSAGE, {
      cause: networkError,
    });
  } finally {
    if (timeoutId) {
      globalThis.clearTimeout(timeoutId);
    }
    // The caller's abort stays attached (once) after the headers arrive so it
    // still cancels the body download; aborting after that is a no-op.
  }

  const durationMs = Date.now() - startedAt;
  const contentType = response.headers.get("content-type") ?? "";
  const requestId = response.headers.get("x-request-id");
  // Foreground gateway failures activate the shared connection recovery flow.
  if (!backgroundAnalytics && RETRYABLE_GATEWAY_STATUSES.has(response.status)) {
    reportNetworkFailure();
  }

  if (!response.ok) {
    const rawText = await response.text();
    completeApiMeasurement(response);
    const trimmedText = rawText.trim();
    let parsedBody: unknown = null;

    if (trimmedText && contentType.includes("application/json")) {
      try {
        parsedBody = JSON.parse(trimmedText);
      } catch (parseError) {
        if (shouldLogApiDetails()) {
          console.warn("[api] request:error_body_json_parse_failed", {
            path,
            method,
            url,
            requestId,
            status: response.status,
            contentType,
            durationMs,
            parseError:
              parseError instanceof Error
                ? { name: parseError.name, message: parseError.message }
                : parseError,
          });
        }
      }
    }

    if (shouldLogApiDetails()) {
      console.error("[api] request:failed", {
        path,
        method,
        url,
        requestId,
        status: response.status,
        statusText: response.statusText,
        contentType,
        durationMs,
        rawText: truncateForLog(trimmedText),
        parsedBody,
      });
    }

    if (parsedBody && typeof parsedBody === "object" && parsedBody !== null) {
      const detail = "detail" in parsedBody ? (parsedBody as { detail?: unknown }).detail : null;
      const bodyRequestId =
        "request_id" in parsedBody ? (parsedBody as { request_id?: unknown }).request_id : null;
      const rawCode = "code" in parsedBody ? (parsedBody as { code?: unknown }).code : null;
      let errorCode = typeof rawCode === "string" && rawCode ? rawCode : undefined;

      if (detail && typeof detail === "object" && !Array.isArray(detail)) {
        const detailBody = detail as { code?: unknown; message?: unknown };
        const detailCode = typeof detailBody.code === "string" && detailBody.code ? detailBody.code : undefined;
        const detailMessage = typeof detailBody.message === "string" ? detailBody.message : null;
        errorCode = errorCode ?? detailCode;
        if (detailMessage) {
          throw new ApiError(
            detailCode === "health_data_consent_required" || detailCode === "confirm_remove"
              ? detailMessage
              : bodyRequestId ? `${detailMessage} (request id: ${String(bodyRequestId)})` : detailMessage,
            response.status,
            errorCode,
          );
        }
      }

      if (typeof detail === "string") {
        throw new ApiError(
          bodyRequestId ? `${detail} (request id: ${String(bodyRequestId)})` : detail,
          response.status,
          errorCode,
        );
      }

      if (detail != null) {
        throw new ApiError(
          bodyRequestId
            ? `${JSON.stringify(detail)} (request id: ${String(bodyRequestId)})`
            : JSON.stringify(detail),
          response.status,
          errorCode,
        );
      }
    }

    throw new ApiError(
      buildPlainTextErrorMessage({
        status: response.status,
        contentType,
        trimmedText,
        headerRequestId: requestId,
      }),
      response.status,
    );
  }

  return { response, path, method, url, durationMs, contentType, requestId };
}

async function readJson<T>(path: string, init?: ApiRequestInit): Promise<T> {
  const { response, method, url, durationMs, contentType, requestId } = await executeRequest(
    path,
    init,
  );

  try {
    const data = (await response.json()) as T;
    completeApiMeasurement(response);
    if (shouldLogApiDetails()) {
      console.info("[api] request:success", {
        path,
        method,
        url,
        requestId,
        status: response.status,
        durationMs,
      });
    }
    if (init?.token && !path.startsWith("/api/admin/") && !path.startsWith("/api/analytics/")) {
      const token = init.token;
      if (path === "/api/nutrition/current" && method === "PUT") void trackProductEvent(token, "nutrition_saved", "nutrition");
      if (/^\/api\/custom-plans\/[^/]+$/.test(path) && method === "PUT") void productEventIdentity(path).then(flow => trackProductEvent(token, "plan_edited", "plans", undefined, flow)).catch(() => {});
      if (path.endsWith("/set-active") && method === "POST") void trackProductEvent(token, "plan_replaced", "plans");
    }
    return data;
  } catch (parseError) {
    completeApiMeasurement(response);
    if (shouldLogApiDetails()) {
      console.error("[api] request:success_body_parse_failed", {
        path,
        method,
        url,
        requestId,
        status: response.status,
        contentType,
        durationMs,
        parseError:
          parseError instanceof Error
            ? { name: parseError.name, message: parseError.message, stack: parseError.stack }
            : parseError,
      });
    }
    throw new Error("Server returned an unreadable response.");
  }
}

export function getTrainingHistory(token: string, includeSkipped = false, beforeDay?: string): Promise<TrainingHistoryPage> {
  const before = beforeDay ? `&before_day=${encodeURIComponent(beforeDay)}` : "";
  const path = includeSkipped ? `/api/training/history?include_skipped=true&limit=200${before}` : "/api/training/history";
  // Shared while in flight only: History edits must show on the next read.
  return sharedRead(token, path, signal => readJson<TrainingHistoryPage>(path, { token, signal }));
}

export function getSessionDetails(token: string, source: TrainingHistorySession["source"], id: string): Promise<SessionDetails> {
  return readJson(`/api/training/history/${source}/${encodeURIComponent(id)}`, { token });
}

export function getExerciseLibrary(token: string): Promise<ExerciseLibraryResponse> {
  return readJson("/api/history/exercise-library", { token });
}

export function getExercisePerformances(token: string, exercise: string, asOf: string, cursor?: string): Promise<ExercisePerformancesResponse> {
  const query = new URLSearchParams({ exercise, as_of: asOf });
  if (cursor) query.set("cursor", cursor);
  return readJson(`/api/history/exercise-performances?${query}`, { token });
}

/** Manual-only evidence avoids truncating habits behind prescribed sessions. */
export function getManualTrainingHistory(token: string): Promise<{ sessions: TrainingHistorySession[] }> {
  return readJson("/api/training/history?manual_only=true&limit=200", { token });
}

export function logExternalTraining(token: string, body: ExternalTrainingFields & { client_request_id: string; training_day?: string }): Promise<TrainingHistorySession> {
  return readJson<TrainingHistorySession>("/api/training/sessions", { method: "POST", token, body: JSON.stringify(body) })
    .then(row => { requestXpRefresh(); return row; });
}

export function editExternalTraining(token: string, id: string, body: ExternalTrainingFields): Promise<TrainingHistorySession> {
  const payload = JSON.stringify(body);
  // Replaying the same field replacement is safe after a lost gateway response.
  return withTransientRetries(() => readJson(`/api/training/sessions/${encodeURIComponent(id)}`, { method: "PATCH", token, body: payload }));
}

export function saveTrainingTakeaway(token: string, id: string, takeaway: string): Promise<TrainingHistorySession> {
  const payload = JSON.stringify({ takeaway });
  // Setting the same text again is a no-op, so a lost response can be retried.
  return withTransientRetries(() => readJson(`/api/training/sessions/${encodeURIComponent(id)}/takeaway`, { method: "PUT", token, body: payload }));
}

export function listManualExercises(token: string, sessionId: string, signal?: AbortSignal): Promise<ManualExerciseListResponse> {
  return readJson(`/api/training/sessions/${encodeURIComponent(sessionId)}/exercises`, { token, signal });
}

export function saveManualExercise(token: string, sessionId: string, entryId: string, body: PerformedExerciseInput): Promise<ManualExerciseEntry> {
  const payload = JSON.stringify(body);
  // The client-chosen entry ID makes the save idempotent: a retry never adds a second exercise.
  return withTransientRetries(() => readJson<ManualExerciseEntry>(`/api/training/sessions/${encodeURIComponent(sessionId)}/exercises/${encodeURIComponent(entryId)}`,
    { method: "PUT", token, body: payload })).then(row => { requestXpRefresh(); return row; });
}

export function deleteManualExercise(token: string, sessionId: string, entryId: string): Promise<void> {
  // A tombstone answers a repeated delete.
  return withTransientRetries(() => requestVoid(`/api/training/sessions/${encodeURIComponent(sessionId)}/exercises/${encodeURIComponent(entryId)}`,
    { method: "DELETE", token }));
}

export function deleteExternalTraining(token: string, id: string): Promise<void> {
  // The backend keeps a tombstone and treats repeated deletes as successful.
  return withTransientRetries(() => requestVoid(`/api/training/sessions/${encodeURIComponent(id)}`, { method: "DELETE", token }));
}

async function requestVoid(path: string, init?: ApiRequestInit): Promise<void> {
  const { response, method, url, durationMs, requestId } = await executeRequest(path, init);

  // Drain the body so the connection can be released even when the server
  // returned content alongside a 200/204. We do not parse or throw on body
  // content here — success status is the contract for void requests.
  if (response.status !== 204) {
    try {
      await response.text();
    } catch {
      // Ignore — the server may have already closed the stream.
    }
  }

  if (shouldLogApiDetails()) {
    console.info("[api] request:success", {
      path,
      method,
      url,
      requestId,
      status: response.status,
      durationMs,
    });
  }
}

export function getApprovedProfile(token: string): Promise<{ approved: boolean }> {
  return readJson<{ approved: boolean }>("/api/me/admission", { token });
}

export function getMe(token: string): Promise<MeResponse> {
  const activeUpdate = meUpdatesByToken.get(token);
  if (activeUpdate) {
    return activeUpdate;
  }

  const activeRequest = meRequestsByToken.get(token);
  if (activeRequest) {
    return activeRequest;
  }

  const request = readJson<MeResponse>("/api/me", { token }).finally(() => {
    if (meRequestsByToken.get(token) === request) {
      meRequestsByToken.delete(token);
    }
  });
  meRequestsByToken.set(token, request);
  return request;
}

export async function claimDailyLoginXp(token: string): Promise<XpAwardResult> {
  const response = await readJson<unknown>("/api/xp/daily-login", {
    method: "POST",
    token,
  });
  return parseXpAwardResponse(response);
}

export function updateMe(token: string, payload: ProfileUpdateRequest): Promise<MeResponse> {
  meRequestsByToken.delete(token);
  const request = readJson<MeResponse>("/api/me", {
    method: "PUT",
    token,
    body: JSON.stringify(payload),
  }).finally(() => {
    if (meUpdatesByToken.get(token) === request) {
      meUpdatesByToken.delete(token);
    }
    meRequestsByToken.delete(token);
  });
  meUpdatesByToken.set(token, request);
  return request;
}

/**
 * Record date of birth, Terms acceptance and health-data consent.
 *
 * Shares `updateMe`'s cache handling: the response is the authoritative profile,
 * so any in-flight `/api/me` read is invalidated rather than allowed to win.
 */
export function recordCompliance(
  token: string,
  payload: ComplianceAcceptanceRequest,
): Promise<MeResponse> {
  meRequestsByToken.delete(token);
  const request = readJson<MeResponse>("/api/me/compliance", {
    method: "POST",
    token,
    body: JSON.stringify(payload),
  }).finally(() => {
    if (meUpdatesByToken.get(token) === request) {
      meUpdatesByToken.delete(token);
    }
    meRequestsByToken.delete(token);
  });
  meUpdatesByToken.set(token, request);
  return request;
}

export function changeUsername(token: string, payload: UsernameChangeRequest): Promise<MeResponse> {
  meRequestsByToken.delete(token);
  const request = readJson<MeResponse>("/api/me/username", {
    method: "POST",
    token,
    body: JSON.stringify(payload),
  }).finally(() => {
    if (meUpdatesByToken.get(token) === request) {
      meUpdatesByToken.delete(token);
    }
    meRequestsByToken.delete(token);
  });
  meUpdatesByToken.set(token, request);
  return request;
}

export type OnboardingDraftSaveResponse = {
  ok: boolean;
  updated_at: string;
};

export function saveOnboardingDraft(
  token: string,
  payload: Pick<
    ProfileUpdateRequest,
    "onboarding_draft" | "full_name" | "technical_style" | "tactical_style" | "stance" | "professional_status" | "record" | "athlete_timezone"
  >,
): Promise<OnboardingDraftSaveResponse> {
  return readJson<OnboardingDraftSaveResponse>("/api/onboarding/draft", {
    method: "PATCH",
    token,
    body: JSON.stringify(payload),
  });
}

export function getNutritionCurrent(token: string): Promise<NutritionWorkspaceState> {
  return readJson<NutritionWorkspaceState>("/api/nutrition/current", { token });
}

export function updateNutritionCurrent(
  token: string,
  payload: NutritionWorkspaceUpdateRequest,
): Promise<NutritionWorkspaceState> {
  return readJson<NutritionWorkspaceState>("/api/nutrition/current", {
    method: "PUT",
    token,
    body: JSON.stringify(payload),
  });
}

export function createGenerationJob(
  token: string,
  payload: PlanRequest,
  clientRequestId?: string,
  planSource?: string | null,
): Promise<GenerationJobResponse> {
  const stableClientRequestId = clientRequestId ?? createClientRequestId("plan_generate");

  return withTransientRetries(() =>
    readJson<GenerationJobResponse>("/api/plans/generate", {
      method: "POST",
      token,
      clientRequestId: stableClientRequestId,
      planSource,
      body: JSON.stringify(payload),
    }),
  );
}

export function getGenerationJob(token: string, jobId: string): Promise<GenerationJobResponse> {
  return withTransientRetries(() =>
    readJson<GenerationJobResponse>(`/api/generation-jobs/${encodeURIComponent(jobId)}`, { token }),
  );
}

export function getActiveGenerationJob(token: string): Promise<GenerationJobResponse | null> {
  return withTransientRetries(() =>
    readJson<GenerationJobResponse | null>("/api/generation-jobs/active", { token }),
  );
}

export function getLatestGenerationJob(token: string): Promise<GenerationJobResponse | null> {
  return withTransientRetries(() =>
    readJson<GenerationJobResponse | null>("/api/generation-jobs/latest", { token }),
  );
}

export function retryGenerationJob(
  token: string,
  jobId: string,
  clientRequestId?: string,
): Promise<GenerationJobResponse> {
  const stableClientRequestId = clientRequestId ?? createClientRequestId(`retry_${jobId}`);

  return withTransientRetries(() =>
    readJson<GenerationJobResponse>(`/api/generation-jobs/${encodeURIComponent(jobId)}/retry`, {
      method: "POST",
      token,
      clientRequestId: stableClientRequestId,
    }),
  );
}

export function cancelGenerationJob(token: string, jobId: string): Promise<GenerationJobResponse> {
  return readJson<GenerationJobResponse>(`/api/generation-jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST",
    token,
  });
}

/** Reuse policy for presentation reads: see shared-reads.ts. */
export type ReadReuse = { reuseForMs?: number };

export function listPlans(token: string): Promise<PlanSummary[]> {
  return sharedRead(token, "/api/plans", signal =>
    withTransientRetries(() => readJson<PlanSummary[]>("/api/plans", { token, signal }), { signal }));
}

/**
 * Plans reads this fresh; the Viewer, which only labels its own plan, may reuse
 * a result from the last few seconds. Any activation, archive or other write
 * discards held results first.
 */
export function getActivePlan(token: string, { reuseForMs }: ReadReuse = {}): Promise<PlanSummary> {
  return sharedRead(token, "/api/plans/active", signal =>
    withTransientRetries(() => readJson<PlanSummary>("/api/plans/active", { token, signal }), { signal }),
    { hold: true, reuseForMs });
}

export function setActivePlan(
  token: string,
  planId: string,
  options?: { overlapAction?: ActivePlanOverlapAction },
): Promise<PlanSummary> {
  const overlapAction = options?.overlapAction;
  return withTransientRetries(() =>
    readJson<PlanSummary>(`/api/plans/${encodeURIComponent(planId)}/set-active`, {
      method: "POST",
      token,
      body: overlapAction ? JSON.stringify({ overlap_action: overlapAction }) : undefined,
    }),
  );
}

/**
 * The saved plan. Home, Today and Plan Detail present the same plan, so they
 * pass `reuseForMs` to share a recent read; background upgrade polls and
 * post-change refreshes omit it and always reach the server.
 */
export function getPlan(
  token: string,
  planId: string,
  options: { signal?: AbortSignal } & ReadReuse = {},
): Promise<PlanDetail> {
  const { signal, reuseForMs } = options;
  const path = `/api/plans/${encodeURIComponent(planId)}`;
  return sharedRead(token, path, shared =>
    withTransientRetries(() => readJson<PlanDetail>(path, { token, signal: shared }), { signal: shared }),
    { signal, hold: true, reuseForMs });
}

/** Completion rows are shared only while in flight: every visit reads them fresh. */
export function getPlanCompletions(token: string, planId: string): Promise<PlanCompletionsResponse> {
  const path = `/api/plans/${encodeURIComponent(planId)}/completions`;
  return sharedRead(token, path, signal =>
    withTransientRetries(() => readJson<PlanCompletionsResponse>(path, { token, signal }), { signal }));
}

export function renamePlan(token: string, planId: string, planName: string): Promise<PlanDetail> {
  return withTransientRetries(() =>
    readJson<PlanDetail>(`/api/plans/${encodeURIComponent(planId)}`, {
      method: "PATCH",
      token,
      body: JSON.stringify({ plan_name: planName }),
    }),
  );
}

// Archives the plan. The server-side DELETE route is archive-only; the plan
// stays recoverable in the athlete's archived list.
export async function deletePlan(token: string, planId: string): Promise<void> {
  return withTransientRetries(() =>
    requestVoid(`/api/plans/${encodeURIComponent(planId)}`, {
      method: "DELETE",
      token,
    }),
  );
}

export async function archivePlan(token: string, planId: string): Promise<void> {
  return deletePlan(token, planId);
}

// Admin-only hard delete. Non-archived plans require the exact plan name as
// typed confirmation; archived plans can be deleted without it (pass no name).
export async function adminPermanentlyDeletePlan(
  token: string,
  planId: string,
  confirmPlanName?: string,
): Promise<void> {
  const trimmedName = confirmPlanName?.trim();
  return requestVoid(`/api/admin/plans/${encodeURIComponent(planId)}/permanent`, {
    method: "DELETE",
    token,
    body: trimmedName ? JSON.stringify({ confirm_plan_name: trimmedName }) : undefined,
  });
}

export type BulkPermanentDeleteResult = {
  deleted: string[];
  skipped: { plan_id: string; reason: string }[];
  deleted_count: number;
  skipped_count: number;
};

// Admin-only bulk hard delete. Only already-archived plans are removed; any
// non-archived ids are reported back in `skipped`.
export async function bulkPermanentlyDeleteArchivedPlans(
  token: string,
  planIds: string[],
): Promise<BulkPermanentDeleteResult> {
  return readJson<BulkPermanentDeleteResult>("/api/admin/plans/bulk-permanent-delete", {
    method: "POST",
    token,
    body: JSON.stringify({ plan_ids: planIds }),
  });
}

export type AdminListQuery = {
  q?: string;
  limit?: number;
  offset?: number;
};

function buildAdminListPath(basePath: string, query?: AdminListQuery): string {
  const params = new URLSearchParams();
  const trimmedQuery = query?.q?.trim();
  if (trimmedQuery) {
    params.set("q", trimmedQuery);
  }
  if (typeof query?.limit === "number") {
    params.set("limit", String(query.limit));
  }
  if (typeof query?.offset === "number") {
    params.set("offset", String(query.offset));
  }
  const search = params.toString();
  return search ? `${basePath}?${search}` : basePath;
}

export function listAdminAthletes(
  token: string,
  query?: AdminListQuery,
): Promise<AdminAthleteRecord[]> {
  return withTransientRetries(() =>
    readJson<AdminAthleteRecord[]>(buildAdminListPath("/api/admin/athletes", query), { token }),
  );
}

export function getAdminTimeToFirstPlan(token: string, days = 30): Promise<AdminTimeToFirstPlanSummary> {
  return withTransientRetries(() =>
    readJson<AdminTimeToFirstPlanSummary>(`/api/admin/metrics/time-to-first-plan?days=${days}`, { token }),
  );
}

export function getAdminAthlete(token: string, athleteId: string): Promise<AdminAthleteRecord> {
  return withTransientRetries(() =>
    readJson<AdminAthleteRecord>(`/api/admin/athletes/${encodeURIComponent(athleteId)}`, { token }),
  );
}

export function updateAdminAthleteLatestIntake(
  token: string,
  athleteId: string,
  payload: AdminLatestIntakeUpdateRequest,
): Promise<AdminAthleteRecord> {
  return withTransientRetries(() =>
    readJson<AdminAthleteRecord>(`/api/admin/athletes/${encodeURIComponent(athleteId)}/latest-intake`, {
      method: "PATCH",
      token,
      body: JSON.stringify(payload),
    }),
  );
}

export function getAdminAthleteNutritionCurrent(
  token: string,
  athleteId: string,
): Promise<NutritionWorkspaceState> {
  return withTransientRetries(() =>
    readJson<NutritionWorkspaceState>(`/api/admin/athletes/${encodeURIComponent(athleteId)}/nutrition/current`, { token }),
  );
}

export function updateAdminAthleteNutritionCurrent(
  token: string,
  athleteId: string,
  payload: NutritionWorkspaceUpdateRequest,
): Promise<NutritionWorkspaceState> {
  return withTransientRetries(() =>
    readJson<NutritionWorkspaceState>(`/api/admin/athletes/${encodeURIComponent(athleteId)}/nutrition/current`, {
      method: "PUT",
      token,
      body: JSON.stringify(payload),
    }),
  );
}

export function generateAdminAthletePlanFromLatestIntake(
  token: string,
  athleteId: string,
  clientRequestId?: string,
): Promise<GenerationJobResponse> {
  const stableClientRequestId =
    clientRequestId ?? createClientRequestId(`admin_latest_intake_${athleteId}`);

  return withTransientRetries(() =>
    readJson<GenerationJobResponse>(
      `/api/admin/athletes/${encodeURIComponent(athleteId)}/plans/generate-from-latest-intake`,
      {
        method: "POST",
        token,
        clientRequestId: stableClientRequestId,
      },
    ),
  );
}

export function getAdminAthleteGenerationJobs(
  token: string,
  athleteId: string,
  limit = 10,
): Promise<AdminGenerationJobDiagnostic[]> {
  return withTransientRetries(() =>
    readJson<AdminGenerationJobDiagnostic[]>(
      `/api/admin/athletes/${encodeURIComponent(athleteId)}/generation-jobs?limit=${limit}`,
      { token },
    ),
  );
}

export function listAdminTriageGenerationJobs(
  token: string,
  limit = 20,
): Promise<AdminGenerationJobDiagnostic[]> {
  return withTransientRetries(() =>
    readJson<AdminGenerationJobDiagnostic[]>(
      `/api/admin/generation-jobs/triage?limit=${limit}`,
      { token },
    ),
  );
}

export function listAdminActiveGenerationJobs(
  token: string,
  limit = 20,
): Promise<AdminGenerationJobDiagnostic[]> {
  return withTransientRetries(() =>
    readJson<AdminGenerationJobDiagnostic[]>(
      `/api/admin/generation-jobs/active?limit=${limit}`,
      { token },
    ),
  );
}

export function cancelAdminGenerationJob(
  token: string,
  jobId: string,
): Promise<GenerationJobResponse> {
  return readJson<GenerationJobResponse>(
    `/api/admin/generation-jobs/${encodeURIComponent(jobId)}`,
    {
      method: "DELETE",
      token,
    },
  );
}

export function listAdminPlans(
  token: string,
  query?: AdminListQuery,
): Promise<AdminPlanSummary[]> {
  return withTransientRetries(() =>
    readJson<AdminPlanSummary[]>(buildAdminListPath("/api/admin/plans", query), { token }),
  );
}

export function listAdminReviewPlans(
  token: string,
  limit = 20,
): Promise<AdminPlanSummary[]> {
  return withTransientRetries(() =>
    readJson<AdminPlanSummary[]>(`/api/admin/plans/review?limit=${limit}`, { token }),
  );
}

export function submitManualStage2(
  token: string,
  planId: string,
  payload: ManualStage2SubmissionRequest,
): Promise<PlanDetail> {
  return readJson<PlanDetail>(`/api/admin/plans/${encodeURIComponent(planId)}/manual-stage2`, {
    method: "POST",
    token,
    body: JSON.stringify(payload),
  });
}

export function approvePlanForRelease(token: string, planId: string): Promise<PlanDetail> {
  return readJson<PlanDetail>(`/api/admin/plans/${encodeURIComponent(planId)}/approve`, {
    method: "POST",
    token,
  });
}

export function approveAndResumeGeneration(
  token: string,
  planId: string,
  payload: ApproveAndResumeGenerationRequest,
  clientRequestId?: string,
): Promise<GenerationJobResponse> {
  const stableClientRequestId = clientRequestId ?? createClientRequestId(`triage_resume_${planId}`);

  return withTransientRetries(() =>
    readJson<GenerationJobResponse>(
      `/api/admin/plans/${encodeURIComponent(planId)}/approve-and-resume-generation`,
      {
        method: "POST",
        token,
        clientRequestId: stableClientRequestId,
        body: JSON.stringify(payload),
      },
    ),
  );
}

export function approveAndResumeGenerationFromJob(
  token: string,
  jobId: string,
  payload: ApproveAndResumeGenerationRequest,
  clientRequestId?: string,
): Promise<GenerationJobResponse> {
  const stableClientRequestId = clientRequestId ?? createClientRequestId(`triage_resume_job_${jobId}`);

  return withTransientRetries(() =>
    readJson<GenerationJobResponse>(
      `/api/admin/generation-jobs/${encodeURIComponent(jobId)}/approve-and-resume-generation`,
      {
        method: "POST",
        token,
        clientRequestId: stableClientRequestId,
        body: JSON.stringify(payload),
      },
    ),
  );
}

export function rejectApprovedPlan(token: string, planId: string): Promise<PlanDetail> {
  return readJson<PlanDetail>(`/api/admin/plans/${encodeURIComponent(planId)}/reject`, {
    method: "POST",
    token,
  });
}

export function rejectAdminGenerationJob(token: string, jobId: string): Promise<GenerationJobResponse> {
  return readJson<GenerationJobResponse>(`/api/admin/generation-jobs/${encodeURIComponent(jobId)}/reject`, {
    method: "POST",
    token,
  });
}

export function adminArchivePlan(token: string, planId: string): Promise<PlanDetail> {
  return readJson<PlanDetail>(`/api/admin/plans/${encodeURIComponent(planId)}/archive`, {
    method: "POST",
    token,
  });
}

export type StructuredPlanBackfillResult = {
  queued: number;
  plan_ids: string[];
};

export type StructuredPlanRebuildResult = {
  queued: boolean;
  plan_id: string;
};

/**
 * Re-run structured-card conversion for one plan. The backend owns
 * idempotency and reapplies every normal validation and safety gate.
 */
export function rebuildStructuredPlan(
  token: string,
  planId: string,
): Promise<StructuredPlanRebuildResult> {
  return readJson<StructuredPlanRebuildResult>(
    `/api/admin/plans/${encodeURIComponent(planId)}/structured-plan/rebuild`,
    {
      method: "POST",
      token,
    },
  );
}

/**
 * Trigger a background backfill that re-runs structured-plan conversion for
 * athlete-displayable plans that have no structured card yet (legacy plans
 * generated before structured generation existed). Returns immediately with the
 * queued plan ids; cards appear on each plan as its conversion lands.
 */
export function backfillStructuredPlans(
  token: string,
  options?: { limit?: number },
): Promise<StructuredPlanBackfillResult> {
  const query =
    typeof options?.limit === "number" ? `?limit=${encodeURIComponent(options.limit)}` : "";
  return readJson<StructuredPlanBackfillResult>(
    `/api/admin/plans/structured-plan/backfill${query}`,
    {
      method: "POST",
      token,
    },
  );
}

// ---------------------------------------------------------------------------
// Live athlete daily flow (dashboard, check-ins, session logs, injury flags,
// admin review queue).
// ---------------------------------------------------------------------------

export function reportInjury(
  token: string,
  payload: InjuryFlagCreateRequest,
): Promise<InjuryFlagRecord> {
  return readJson<InjuryFlagRecord>("/api/injury-flags", {
    method: "POST",
    token,
    body: JSON.stringify(payload),
  });
}

export function listInjuryFlags(token: string, includeResolved = false): Promise<InjuryFlagRecord[]> {
  return withTransientRetries(() =>
    readJson<InjuryFlagRecord[]>(`/api/injury-flags?include_resolved=${includeResolved}`, { token }),
  );
}

export function listAdminReviews(
  token: string,
  status: AdminReviewRecord["status"] | "all" = "pending",
  limit = 50,
): Promise<AdminReviewRecord[]> {
  return withTransientRetries(() =>
    readJson<AdminReviewRecord[]>(
      `/api/admin/reviews?status=${encodeURIComponent(status)}&limit=${limit}`,
      { token },
    ),
  );
}

export function listAdminFeedback(token: string, limit = 50): Promise<AdminFeedbackRecord[]> {
  return withTransientRetries(() =>
    readJson<AdminFeedbackRecord[]>(`/api/admin/feedback?limit=${limit}`, { token }),
  );
}

export function getAdminFeedbackScreenshot(
  token: string,
  feedbackId: string,
): Promise<AdminFeedbackScreenshotAccess> {
  return readJson<AdminFeedbackScreenshotAccess>(
    `/api/admin/feedback/${encodeURIComponent(feedbackId)}/screenshot`,
    { token },
  );
}

export function resolveAdminReview(
  token: string,
  reviewId: string,
  payload: AdminReviewResolveRequest,
): Promise<AdminReviewRecord> {
  return readJson<AdminReviewRecord>(
    `/api/admin/reviews/${encodeURIComponent(reviewId)}/resolve`,
    {
      method: "POST",
      token,
      body: JSON.stringify(payload),
    },
  );
}

export function updateAdminInjuryFlag(
  token: string,
  flagId: string,
  status: InjuryFlagStatus,
): Promise<InjuryFlagRecord> {
  return readJson<InjuryFlagRecord>(`/api/admin/injury-flags/${encodeURIComponent(flagId)}`, {
    method: "PATCH",
    token,
    body: JSON.stringify({ status }),
  });
}

function exerciseChangeQuery(params: Record<string, string | boolean | null | undefined>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== false) query.set(key, String(value));
  }
  return query.toString();
}

/** The best replacements for one exercise of one dated workout. */
export function getExerciseAlternatives(
  token: string,
  planId: string,
  params: { date: string; sessionId: string; blockId: string; all?: boolean },
  signal?: AbortSignal,
): Promise<ExerciseOptionsResponse> {
  const query = exerciseChangeQuery({
    date: params.date, session_id: params.sessionId, block_id: params.blockId, all: params.all,
  });
  return withTransientRetries(() => readJson<ExerciseOptionsResponse>(
    `/api/plans/${encodeURIComponent(planId)}/exercise-changes/alternatives?${query}`, { token, signal },
  ), { signal });
}

/** Exercises to add to one dated workout: ranked picks, or any library match for `q`. */
export function getExerciseAdditions(
  token: string,
  planId: string,
  params: { date: string; sessionId: string; all?: boolean; q?: string },
  signal?: AbortSignal,
): Promise<ExerciseOptionsResponse> {
  const query = exerciseChangeQuery({
    date: params.date, session_id: params.sessionId, all: params.all, q: params.q?.trim() || null,
  });
  return withTransientRetries(() => readJson<ExerciseOptionsResponse>(
    `/api/plans/${encodeURIComponent(planId)}/exercise-changes/additions?${query}`, { token, signal },
  ), { signal });
}

/** Swap, add or remove one exercise. Retry-safe on its client_request_id. */
export function applyExerciseChange(
  token: string,
  planId: string,
  body: ExerciseChangeRequest,
): Promise<ExerciseChangeResult> {
  return withTransientRetries(() => readJson<ExerciseChangeResult>(
    `/api/plans/${encodeURIComponent(planId)}/exercise-changes`,
    { method: "POST", token, body: JSON.stringify(body) },
  ));
}

/** Undo (or restore from ``date``) one change. Idempotent. */
export function undoExerciseChange(
  token: string,
  planId: string,
  changeId: string,
  date?: string | null,
): Promise<ExerciseChangeResult> {
  return withTransientRetries(() => readJson<ExerciseChangeResult>(
    `/api/plans/${encodeURIComponent(planId)}/exercise-changes/${encodeURIComponent(changeId)}/undo`,
    { method: "POST", token, body: JSON.stringify(date ? { date } : {}) },
  ));
}

/**
 * The authoritative Today command view. Home and Today always read it fresh
 * (joining at most an identical read already in flight). Only screens that
 * merely label the active plan pass `reuseForMs`: a reused view never decides
 * readiness, training permission, holds or whether a save succeeded.
 */
export function getToday(token: string, { reuseForMs }: ReadReuse = {}): Promise<TodayCommandView> {
  return sharedRead(token, "/api/today", signal =>
    withTransientRetries(() => readJson<TodayCommandView>("/api/today", { token, signal }), { signal }),
    { hold: true, reuseForMs });
}

export function submitTodayCheckin(
  token: string,
  payload: TodayCheckinRequest,
): Promise<TodayCheckinResponse> {
  return readJson<TodayCheckinResponse>("/api/today/checkin", {
    method: "POST",
    token,
    body: JSON.stringify(payload),
  });
}

export function submitTodaySessionCompletion(
  token: string,
  payload: TodaySessionCompletionRequest,
): Promise<TodaySessionCompletionResponse> {
  return readJson<TodaySessionCompletionResponse>("/api/today/session-completion", {
    method: "POST",
    token,
    body: JSON.stringify(payload),
  });
}

/** Record one block of sparring rounds (from the round timer). */
export function submitSparringLog(
  token: string,
  payload: SparringLogRequest,
): Promise<SparringLogResponse> {
  return readJson<SparringLogResponse>("/api/today/sparring-log", {
    method: "POST",
    token,
    body: JSON.stringify(payload),
  });
}

/** Log what was actually done for one block of today's started session. */
export function submitExerciseLog(
  token: string,
  payload: ExerciseLogRequest,
): Promise<ExerciseLogResponse> {
  return readJson<ExerciseLogResponse>("/api/today/exercise-log", {
    method: "POST",
    token,
    body: JSON.stringify(payload),
  }).then(row => { requestXpRefresh(); return row; });
}

/** Log several blocks of today's started session at once; all or nothing. */
export function submitExerciseLogs(
  token: string,
  payload: ExerciseLogBatchRequest,
): Promise<ExerciseLogBatchResponse> {
  return readJson<ExerciseLogBatchResponse>("/api/today/exercise-logs", {
    method: "POST",
    token,
    body: JSON.stringify(payload),
  }).then(row => { requestXpRefresh(); return row; });
}

/** The athlete's exercise logs for one plan on the current training day. */
export function listExerciseHistory(token: string, offset = 0): Promise<ExerciseHistoryResponse> {
  return withTransientRetries(() => readJson<ExerciseHistoryResponse>(`/api/history/exercises?offset=${offset}`, { token }));
}

export function listTodayExerciseLogs(
  token: string,
  planId: string,
): Promise<ExerciseLogListResponse> {
  const params = new URLSearchParams({ plan_id: planId });
  return withTransientRetries(() =>
    readJson<ExerciseLogListResponse>(`/api/today/exercise-logs?${params.toString()}`, { token }),
  );
}

export function listPendingRehabResponses(
  token: string,
  planId: string,
): Promise<PendingRehabResponsesResponse> {
  const params = new URLSearchParams({ plan_id: planId });
  return readJson<PendingRehabResponsesResponse>(
    `/api/today/rehab-responses/pending?${params.toString()}`,
    { token },
  );
}

/**
 * Append the injury-specific evidence for one completed rehab session.
 *
 * Deliberately a separate call from the completion itself: the completion is a
 * training log, this is an observation about one injury. Sending answers only —
 * no drill, episode or side — is what keeps the client out of the attribution.
 */
export function submitRehabResponses(
  token: string,
  payload: RehabResponseRequest,
): Promise<RehabResponseResult> {
  return readJson<RehabResponseResult>("/api/today/rehab-responses", {
    method: "POST",
    token,
    body: JSON.stringify(payload),
  });
}

export function listSessionCompletionHistory(
  token: string,
  limit = 60,
): Promise<TodaySessionCompletionRecord[]> {
  return withTransientRetries(() =>
    readJson<TodaySessionCompletionRecord[]>(`/api/today/session-completions?limit=${limit}`, {
      token,
    }),
  );
}

/** The athlete's sparring log and its 7- and 28-day totals (History). */
export function listSparringLogHistory(
  token: string,
  limit = 60,
): Promise<SparringLogHistoryResponse> {
  return withTransientRetries(() =>
    readJson<SparringLogHistoryResponse>(`/api/today/sparring-logs?limit=${limit}`, { token }),
  );
}

export function listTodayCheckinHistory(
  token: string,
  limit = 60,
): Promise<TodayCheckinHistoryRecord[]> {
  return withTransientRetries(() =>
    readJson<TodayCheckinHistoryRecord[]>(`/api/today/checkins?limit=${limit}`, { token }),
  );
}

export function submitTodayInjuryCheckin(
  token: string,
  payload: TodayInjuryCheckinRequest,
): Promise<TodayInjuryCheckinResponse> {
  return readJson<TodayInjuryCheckinResponse>("/api/today/injury-checkin", {
    method: "POST",
    token,
    body: JSON.stringify(payload),
  });
}

export function submitInjuryEpisodeObservation(
  token: string,
  payload: import("./api-schema.generated").InjuryEpisodeObservation,
): Promise<unknown> {
  return readJson("/api/today/injury-episode-observation", { method: "POST", token, body: JSON.stringify(payload) });
}

export function getPlanFeedback(token: string, planId: string): Promise<FeedbackRecord | null> {
  return readJson<FeedbackRecord | null>(`/api/plans/${encodeURIComponent(planId)}/feedback`, { token });
}

export function putPlanFeedback(
  token: string,
  planId: string,
  payload: ContextualFeedbackRequest,
): Promise<FeedbackRecord> {
  return readJson<FeedbackRecord>(`/api/plans/${encodeURIComponent(planId)}/feedback`, {
    method: "PUT",
    token,
    body: JSON.stringify(payload),
  });
}

export function getTodayFeedback(token: string): Promise<FeedbackRecord | null> {
  return readJson<FeedbackRecord | null>("/api/today/feedback", { token });
}

export function putTodayFeedback(
  token: string,
  payload: ContextualFeedbackRequest,
): Promise<FeedbackRecord> {
  return readJson<FeedbackRecord>("/api/today/feedback", {
    method: "PUT",
    token,
    body: JSON.stringify(payload),
  });
}

export function submitSessionFeedback(
  token: string,
  payload: SessionFeedbackRequest,
): Promise<FeedbackRecord> {
  const form = new FormData();
  form.set("plan_id", payload.plan_id);
  form.set("session_id", payload.session_id);
  form.set("training_day", payload.training_day ?? "");
  // Unanswered questions are sent empty; the backend drops them rather than
  // recording a default the athlete never chose.
  form.set("difficulty", payload.difficulty ?? "");
  form.set("instructions", payload.instructions ?? "");
  form.set("plan_accuracy", payload.plan_accuracy ?? "");
  form.set("comment", payload.comment ?? "");
  if (payload.screenshot) {
    form.set("screenshot", payload.screenshot);
  }
  return readJson<FeedbackRecord>("/api/feedback/session", {
    method: "POST",
    token,
    body: form,
  });
}

export function submitGlobalFeedback(
  token: string,
  payload: GlobalFeedbackRequest,
): Promise<FeedbackRecord> {
  const form = new FormData();
  form.set("category", payload.category);
  form.set("description", payload.description ?? "");
  form.set("contact_allowed", String(Boolean(payload.contact_allowed)));
  if (payload.screenshot) {
    form.set("screenshot", payload.screenshot);
  }
  return readJson<FeedbackRecord>("/api/feedback/global", {
    method: "POST",
    token,
    body: form,
  });
}

export interface PushSettingsResponse {
  enabled: boolean;
  public_key: string;
}

export interface PushSubscriptionPayload {
  endpoint: string;
  keys: { p256dh: string; auth: string };
  timezone: string;
}

export function getPushSettings(token: string): Promise<PushSettingsResponse> {
  return readJson<PushSettingsResponse>("/api/push/settings", { token });
}

export function savePushSubscription(
  token: string,
  payload: PushSubscriptionPayload,
): Promise<void> {
  return requestVoid("/api/push/subscriptions", {
    method: "POST",
    token,
    body: JSON.stringify(payload),
  });
}

export function deletePushSubscription(token: string, endpoint: string): Promise<void> {
  return requestVoid("/api/push/subscriptions", {
    method: "DELETE",
    token,
    body: JSON.stringify({ endpoint }),
  });
}

export function getCustomDoseSuggestion(token: string, catalogueId: string) {
 return readJson<CustomDoseSuggestion>(`/api/custom-plans/source-suggestion?catalogue_id=${encodeURIComponent(catalogueId)}`, { token });
}
import type { CommunityDraft, CommunityDecision, CommunityEntry, CommunitySearch } from './api-schema.generated';
export function listCommunityExercises(token: string, scope: 'public' | 'review' | 'history' | 'mine', query = '', offset = 0, signal?: AbortSignal) {
  return readJson<CommunitySearch>(`/api/community-exercises${scope === 'public' ? '' : `/${scope}`}?q=${encodeURIComponent(query)}&offset=${offset}`, { token, signal });
}
export function getCommunityExercise(token: string, id: string, scope: 'revisions' | 'review' | 'owned' = 'revisions', signal?: AbortSignal) {
  return readJson<CommunityEntry>(`/api/community-exercises/${scope}/${id}`, { token, signal });
}
export function saveCommunityDraft(token: string, body: CommunityDraft, reviewRevision?: string) {
  return readJson<CommunityEntry>(reviewRevision ? `/api/community-exercises/review/${reviewRevision}` : '/api/community-exercises/drafts', { token, method: reviewRevision ? 'PUT' : 'POST', body: JSON.stringify(body) });
}
export function submitCommunityExercise(token: string, revision: string) {
  return readJson<CommunityEntry>(`/api/community-exercises/revisions/${revision}/submit`, { token, method: 'POST', body: JSON.stringify({ share_rights: true, attribution_consent: true }) });
}
export function decideCommunityExercise(token: string, revision: string, body: CommunityDecision) {
  return readJson<CommunityEntry>(`/api/community-exercises/review/${revision}`, { token, method: 'POST', body: JSON.stringify(body) });
}
export function withdrawCommunityExercise(token: string, revision: string) {
  return readJson<CommunityEntry>(`/api/community-exercises/revisions/${revision}/withdraw`, { token, method: 'POST' });
}
export function reportCommunityExercise(token: string, revision: string, reason: string) {
  return requestVoid(`/api/community-exercises/revisions/${revision}/report`, { token, method: 'POST', body: JSON.stringify({ reason }) });
}

// First-party beta telemetry retries independently of app connectivity.
export async function trackProductEvent(token: string, event_name: string, feature: string, request_id?: string, flow_id?: string) {
  try {
    const body = JSON.stringify({ event_name, feature, request_id: request_id ?? crypto.randomUUID(), flow_id });
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const result = await readJson<{ accepted: boolean }>("/api/analytics/events", { token, method: "POST", body });
        return result.accepted;
      } catch (error) {
        if (error instanceof ApiError && error.status < 500 && error.status !== 429) return false;
        if (attempt === 2) return false;
        await new Promise(resolve => setTimeout(resolve, 250 * 2 ** attempt));
      }
    }
  } catch { /* Analytics must never prevent training or replace Sentry. */ }
}

export function getProductAnalytics(token: string, query: string, signal?: AbortSignal) {
  return readJson<import("./product-analytics").ProductReport>(`/api/admin/product-analytics?${query}`, { token, signal });
}
export function getAthleteActivity(token: string, id: string, offset = 0, signal?: AbortSignal) {
  return readJson<import("./product-analytics").AthleteActivity>(`/api/admin/athletes/${encodeURIComponent(id)}/activity?offset=${offset}`, { token, signal });
}

/** Stable retry identities without persisting browser identifiers or event payloads. */
export async function productEventIdentity(value: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  const bytes = new Uint8Array(digest).slice(0,16);
  bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, b => b.toString(16).padStart(2,'0')).join('');
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}
