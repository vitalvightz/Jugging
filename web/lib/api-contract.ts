// The web app's API types (lib/types.ts) checked against the types generated
// from the API's own schema (lib/api-schema.generated.ts, written by
// tools/generate_api_types.py). `npm run typecheck` fails when they drift:
//
// - Reads<Served, WebType>: what the API sends must fit the type the web reads
//   it as (a renamed, retyped or newly-nullable response field fails here).
// - Sends<WebType, Accepted>: what the web sends must fit what the API accepts
//   (a value the API would reject with a 422 fails here).
//
// Nothing here runs; it is type checking only. A web type with the same name
// as an API schema must appear below (tests/test_api_types_generated.py checks).
import type * as Api from "./api-schema.generated";
import type * as Web from "./types";

type Reads<Served extends WebType, WebType> = [Served, WebType];
type Sends<WebType extends Accepted, Accepted> = [WebType, Accepted];

// Every field the web reads must be one the API declares; an optional web
// field the API renamed or never sends would otherwise always read undefined.
// Fails with the undeclared field names.
type Expect<Check extends true> = Check;
type Declared<WebType, Served, Unsent extends PropertyKey = never> = [
  Exclude<keyof WebType, keyof Served | Unsent>,
] extends [never]
  ? true
  : Exclude<keyof WebType, keyof Served | Unsent>;

// Known narrowings: the web relies on more than the API declares for these
// fields, so they are checked without the field. Each is a real gap to close
// on the API side, not a formality.
//
// - MeResponse.latest_intake, AdminAthleteRecord.latest_intake and
//   AdminAthleteRecord.onboarding_draft: stored intake
//   JSON the API returns unvalidated as an object; the web reads it as a
//   PlanRequest.
// - NutritionSandCPreferences.goal_weakness_collision_details: the API declares
//   a list of string maps; the web reads each as { tag, label, detail }.
//
// - ExerciseChangeResult.session: the occurrence's effective session, sent as
//   an untyped object; the web reads it as the StructuredSession it renders.
//
// Fields the web reads that the API's schema does not declare:
// - InjuryFlagRecord label / canonical_location / region_group / load_region /
//   consequence: added to Today's injury rows, which the API sends untyped
//   (CommandView.open_injuries), not as InjuryFlagRecord.

export type ReadActiveInjuryRegion = Reads<Api.ActiveInjuryRegion, Web.ActiveInjuryRegion>;
export type ReadBlockComponent = Reads<Api.BlockComponent, Web.BlockComponent>;
export type DeclaredBlockComponent = Expect<Declared<Web.BlockComponent, Api.BlockComponent>>;
export type ReadAdminAthleteRecord = Reads<
  Omit<Api.AdminAthleteRecord, "onboarding_draft" | "latest_intake">,
  Omit<Web.AdminAthleteRecord, "onboarding_draft" | "latest_intake">
>;
export type ReadAdminFeedbackRecord = Reads<Api.AdminFeedbackRecord, Web.AdminFeedbackRecord>;
export type ReadAdminFeedbackScreenshotAccess = Reads<Api.AdminFeedbackScreenshotAccess, Web.AdminFeedbackScreenshotAccess>;
export type ReadAdminGenerationJobDiagnostic = Reads<Api.AdminGenerationJobDiagnostic, Web.AdminGenerationJobDiagnostic>;
export type SendAdminLatestIntakeUpdateRequest = Sends<Web.AdminLatestIntakeUpdateRequest, Api.AdminLatestIntakeUpdateRequest>;
export type ReadAdminPlanOutputs = Reads<Api.AdminPlanOutputs, Web.AdminPlanOutputs>;
export type ReadAdminPlanSummary = Reads<Api.AdminPlanSummary, Web.AdminPlanSummary>;
export type ReadAdminReviewRecord = Reads<Api.AdminReviewRecord, Web.AdminReviewRecord>;
export type SendAdminReviewResolveRequest = Sends<Web.AdminReviewResolveRequest, Api.AdminReviewResolveRequest>;
export type SendApproveAndResumeGenerationRequest = Sends<Web.ApproveAndResumeGenerationRequest, Api.ApproveAndResumeGenerationRequest>;
export type SendAthleteProfileInput = Sends<Web.AthleteProfileInput, Api.AthleteProfileInput>;
export type SendComplianceAcceptanceRequest = Sends<Web.ComplianceAcceptanceRequest, Api.ComplianceAcceptanceRequest>;
export type SendContextualFeedbackRequest = Sends<Web.ContextualFeedbackRequest, Api.ContextualFeedbackRequest>;
export type ReadEffortPrescription = Reads<Api.EffortPrescription, Web.EffortPrescription>;
export type ReadExerciseMedia = Reads<Api.ExerciseMedia, Web.ExerciseMedia>;
export type ReadFeedbackRecord = Reads<Api.FeedbackRecord, Web.FeedbackRecord>;
export type ReadGenerationJobResponse = Reads<Api.GenerationJobResponse, Web.GenerationJobResponse>;
export type ReadGenerationRequestPayloadSummary = Reads<Api.GenerationRequestPayloadSummary, Web.GenerationRequestPayloadSummary>;
export type ReadGuidedInjuryInput = Reads<Api.GuidedInjuryInput, Web.GuidedInjuryInput>;
export type SendGuidedInjuryInput = Sends<Web.GuidedInjuryInput, Api.GuidedInjuryInputRequest>;
export type SendInjuryFlagCreateRequest = Sends<Web.InjuryFlagCreateRequest, Api.InjuryFlagCreateRequest>;
export type ReadInjuryFlagRecord = Reads<Api.InjuryFlagRecord, Web.InjuryFlagRecord>;
export type ReadLoadPrescription = Reads<Api.LoadPrescription, Web.LoadPrescription>;
export type SendManualStage2SubmissionRequest = Sends<Web.ManualStage2SubmissionRequest, Api.ManualStage2SubmissionRequest>;
export type ReadMeResponse = Reads<Omit<Api.MeResponse, "latest_intake">, Omit<Web.MeResponse, "latest_intake">>;
export type ReadMeasuredValue = Reads<Api.MeasuredValue, Web.MeasuredValue>;
export type ReadMindsetAnchor = Reads<Api.MindsetAnchor, Web.MindsetAnchor>;
export type ReadNutritionBodyweightLogEntry = Reads<Api.NutritionBodyweightLogEntry, Web.NutritionBodyweightLogEntry>;
export type SendNutritionBodyweightLogEntry = Sends<Web.NutritionBodyweightLogEntry, Api.NutritionBodyweightLogEntryRequest>;
export type ReadNutritionCoachControlsInput = Reads<Api.NutritionCoachControlsInput, Web.NutritionCoachControlsInput>;
export type SendNutritionCoachControlsInput = Sends<Web.NutritionCoachControlsInput, Api.NutritionCoachControlsInputRequest>;
export type ReadNutritionBodyweightTrendPoint = Reads<Api.NutritionBodyweightTrendPoint, Web.NutritionBodyweightTrendPoint>;
export type ReadNutritionDerivedState = Reads<Api.NutritionDerivedState, Web.NutritionDerivedState>;
export type ReadNutritionMonitoringInput = Reads<Api.NutritionMonitoringInput, Web.NutritionMonitoringInput>;
export type SendNutritionMonitoringInput = Sends<Web.NutritionMonitoringInput, Api.NutritionMonitoringInputRequest>;
export type ReadNutritionProfileInput = Reads<Api.NutritionProfileInput, Web.NutritionProfileInput>;
export type SendNutritionProfileInput = Sends<Web.NutritionProfileInput, Api.NutritionProfileInputRequest>;
export type ReadNutritionReadinessInput = Reads<Api.NutritionReadinessInput, Web.NutritionReadinessInput>;
export type SendNutritionReadinessInput = Sends<Web.NutritionReadinessInput, Api.NutritionReadinessInputRequest>;
export type ReadNutritionSandCPreferences = Reads<
    Omit<Api.NutritionSandCPreferences, "goal_weakness_collision_details">,
    Omit<Web.NutritionSandCPreferences, "goal_weakness_collision_details">
  >;
export type SendNutritionSandCPreferences = Sends<Web.NutritionSandCPreferences, Api.NutritionSandCPreferencesRequest>;
export type ReadNutritionSharedCampContext = Reads<Api.NutritionSharedCampContext, Web.NutritionSharedCampContext>;
export type SendNutritionSharedCampContext = Sends<Web.NutritionSharedCampContext, Api.NutritionSharedCampContextRequest>;
export type ReadNutritionWorkspaceState = Reads<
    Omit<Api.NutritionWorkspaceState, "s_and_c_preferences">,
    Omit<Web.NutritionWorkspaceState, "s_and_c_preferences">
  >;
export type SendNutritionWorkspaceUpdateRequest = Sends<Web.NutritionWorkspaceUpdateRequest, Api.NutritionWorkspaceUpdateRequest>;
export type ReadPendingRehabResponsesResponse = Reads<Api.PendingRehabResponsesResponse, Web.PendingRehabResponsesResponse>;
export type ReadPlanAdvisory = Reads<Api.PlanAdvisory, Web.PlanAdvisory>;
export type ReadPlanCalendar = Reads<Api.PlanCalendar, Web.PlanCalendar>;
export type ReadPlanCompletionsResponse = Reads<Api.PlanCompletionsResponse, Web.PlanCompletionsResponse>;
export type ReadPlanDetail = Reads<Api.PlanDetail, Web.PlanDetail>;
export type ReadPlanOutputs = Reads<Api.PlanOutputs, Web.PlanOutputs>;
export type SendPlanRequest = Sends<Web.PlanRequest, Api.PlanRequest>;
export type ReadPlanScheduleContext = Reads<Api.PlanScheduleContext, Web.PlanScheduleContext>;
export type ReadPlanSummary = Reads<Api.PlanSummary, Web.PlanSummary>;
export type ReadPriorityMicrodose = Reads<Api.PriorityMicrodose, Web.PriorityMicrodose>;
export type ReadProfileRecord = Reads<Api.ProfileRecord, Web.ProfileRecord>;
export type SendProfileUpdateRequest = Sends<Web.ProfileUpdateRequest, Api.ProfileUpdateRequest>;
export type ReadProgressMilestone = Reads<Api.ProgressMilestone, Web.ProgressMilestone>;
export type ReadRehabLabelPolicy = Reads<Api.RehabLabelPolicy, Web.RehabLabelPolicy>;
export type SendRehabResponseAnswer = Sends<Web.RehabResponseAnswer, Api.RehabResponseAnswer>;
export type SendRehabResponseRequest = Sends<Web.RehabResponseRequest, Api.RehabResponseRequest>;
export type ReadRehabResponseResult = Reads<Api.RehabResponseResult, Web.RehabResponseResult>;
export type SendExerciseLogActual = Sends<Web.ExerciseLogActual, Api.ExerciseLogActualRequest>;
export type SendExerciseSetResult = Sends<Web.ExerciseSetResult, Api.ExerciseSetResultRequest>;
export type ReadExerciseSetResult = Reads<Api.ExerciseSetResult, Web.ExerciseSetResult>;
export type DeclaredExerciseSetResult = Expect<Declared<Web.ExerciseSetResult, Api.ExerciseSetResult>>;
export type SendExerciseLogEffort = Sends<Web.ExerciseLogEffort, Api.ExerciseLogEffortRequest>;
export type SendExerciseLogMeasure = Sends<Web.ExerciseLogMeasure, Api.ExerciseLogMeasureRequest>;
export type SendExerciseLogRequest = Sends<Web.ExerciseLogRequest, Api.ExerciseLogRequest>;
export type SendExerciseLogBatchRequest = Sends<Web.ExerciseLogBatchRequest, Api.ExerciseLogBatchRequest>;
export type ReadExerciseAlternative = Reads<Api.ExerciseAlternative, Web.ExerciseAlternative>;
export type SendExerciseChangeRequest = Sends<Web.ExerciseChangeRequest, Api.ExerciseChangeRequest>;
export type ReadExerciseChangeResult = Reads<
  Omit<Api.ExerciseChangeResult, "session">, Omit<Web.ExerciseChangeResult, "session">
>;
export type ReadExerciseChangeScope = Reads<Api.ExerciseChangeScope, Web.ExerciseChangeScope>;
export type ReadExerciseOptionsResponse = Reads<Api.ExerciseOptionsResponse, Web.ExerciseOptionsResponse>;
export type ReadExerciseLogActual = Reads<Api.ExerciseLogActual, Web.ExerciseLogActual>;
export type ReadExerciseLogEffort = Reads<Api.ExerciseLogEffort, Web.ExerciseLogEffort>;
export type ReadExerciseLogMeasure = Reads<Api.ExerciseLogMeasure, Web.ExerciseLogMeasure>;
export type ReadExerciseLogRecord = Reads<Api.ExerciseLogRecord, Web.ExerciseLogRecord>;
export type ReadExerciseLogResponse = Reads<Api.ExerciseLogResponse, Web.ExerciseLogResponse>;
export type ReadExerciseLogListResponse = Reads<Api.ExerciseLogListResponse, Web.ExerciseLogListResponse>;
export type ReadExerciseLogBatchResponse = Reads<Api.ExerciseLogBatchResponse, Web.ExerciseLogBatchResponse>;
export type ReadExerciseRecentLoad = Reads<Api.ExerciseRecentLoad, Web.ExerciseRecentLoad>;
export type ReadSparringLogRecord = Reads<Api.SparringLogRecord, Web.SparringLogRecord>;
export type SendSparringLogRequest = Sends<Web.SparringLogRequest, Api.SparringLogRequest>;
export type ReadSparringLogResponse = Reads<Api.SparringLogResponse, Web.SparringLogResponse>;
export type ReadSparringLogHistoryResponse = Reads<Api.SparringLogHistoryResponse, Web.SparringLogHistoryResponse>;
export type ReadSparringWindowSummary = Reads<Api.SparringWindowSummary, Web.SparringWindowSummary>;
export type ReadStructuredCardState = Reads<Api.StructuredCardState, Web.StructuredCardState>;
export type SendTodayCheckinRequest = Sends<Web.TodayCheckinRequest, Api.TodayCheckinRequest>;
export type ReadTodayCheckinResponse = Reads<Api.TodayCheckinResponse, Web.TodayCheckinResponse>;
export type SendTodayInjuryCheckinRequest = Sends<Web.TodayInjuryCheckinRequest, Api.TodayInjuryCheckinRequest>;
export type ReadTodayInjuryCheckinResponse = Reads<Api.TodayInjuryCheckinResponse, Web.TodayInjuryCheckinResponse>;
export type SendTodayInjuryDeclaration = Sends<Web.TodayInjuryDeclaration, Api.TodayInjuryDeclaration>;
export type SendUsernameChangeRequest = Sends<Web.UsernameChangeRequest, Api.UsernameChangeRequest>;
export type ReadUsernameRateLimitInfo = Reads<Api.UsernameRateLimitInfo, Web.UsernameRateLimitInfo>;
export type ReadWeeklyDayEntry = Reads<Api.WeeklyDayEntry, Web.WeeklyDayEntry>;
export type ReadWeeklySchedule = Reads<Api.WeeklySchedule, Web.WeeklySchedule>;

export type DeclaredActiveInjuryRegion = Expect<Declared<Web.ActiveInjuryRegion, Api.ActiveInjuryRegion>>;
export type DeclaredAdminAthleteRecord = Expect<Declared<Web.AdminAthleteRecord, Api.AdminAthleteRecord>>;
export type DeclaredAdminFeedbackRecord = Expect<Declared<Web.AdminFeedbackRecord, Api.AdminFeedbackRecord>>;
export type DeclaredAdminFeedbackScreenshotAccess = Expect<Declared<Web.AdminFeedbackScreenshotAccess, Api.AdminFeedbackScreenshotAccess>>;
export type DeclaredAdminGenerationJobDiagnostic = Expect<Declared<Web.AdminGenerationJobDiagnostic, Api.AdminGenerationJobDiagnostic>>;
export type DeclaredAdminPlanOutputs = Expect<Declared<Web.AdminPlanOutputs, Api.AdminPlanOutputs>>;
export type DeclaredAdminPlanSummary = Expect<Declared<Web.AdminPlanSummary, Api.AdminPlanSummary>>;
export type DeclaredAdminReviewRecord = Expect<Declared<Web.AdminReviewRecord, Api.AdminReviewRecord>>;
export type DeclaredEffortPrescription = Expect<Declared<Web.EffortPrescription, Api.EffortPrescription>>;
export type DeclaredExerciseMedia = Expect<Declared<Web.ExerciseMedia, Api.ExerciseMedia>>;
export type DeclaredFeedbackRecord = Expect<Declared<Web.FeedbackRecord, Api.FeedbackRecord>>;
export type DeclaredGenerationJobResponse = Expect<Declared<Web.GenerationJobResponse, Api.GenerationJobResponse>>;
export type DeclaredGenerationRequestPayloadSummary = Expect<Declared<Web.GenerationRequestPayloadSummary, Api.GenerationRequestPayloadSummary>>;
export type DeclaredGuidedInjuryInput = Expect<Declared<Web.GuidedInjuryInput, Api.GuidedInjuryInput>>;
export type DeclaredInjuryFlagRecord = Expect<Declared<Web.InjuryFlagRecord, Api.InjuryFlagRecord, "label" | "canonical_location" | "region_group" | "load_region" | "consequence">>;
export type DeclaredLoadPrescription = Expect<Declared<Web.LoadPrescription, Api.LoadPrescription>>;
export type DeclaredMeResponse = Expect<Declared<Web.MeResponse, Api.MeResponse>>;
export type DeclaredMeasuredValue = Expect<Declared<Web.MeasuredValue, Api.MeasuredValue>>;
export type DeclaredMindsetAnchor = Expect<Declared<Web.MindsetAnchor, Api.MindsetAnchor>>;
export type DeclaredNutritionBodyweightLogEntry = Expect<Declared<Web.NutritionBodyweightLogEntry, Api.NutritionBodyweightLogEntry>>;
export type DeclaredNutritionCoachControlsInput = Expect<Declared<Web.NutritionCoachControlsInput, Api.NutritionCoachControlsInput>>;
export type DeclaredNutritionBodyweightTrendPoint = Expect<Declared<Web.NutritionBodyweightTrendPoint, Api.NutritionBodyweightTrendPoint>>;
export type DeclaredNutritionDerivedState = Expect<Declared<Web.NutritionDerivedState, Api.NutritionDerivedState>>;
export type DeclaredNutritionMonitoringInput = Expect<Declared<Web.NutritionMonitoringInput, Api.NutritionMonitoringInput>>;
export type DeclaredNutritionProfileInput = Expect<Declared<Web.NutritionProfileInput, Api.NutritionProfileInput>>;
export type DeclaredNutritionReadinessInput = Expect<Declared<Web.NutritionReadinessInput, Api.NutritionReadinessInput>>;
export type DeclaredNutritionSandCPreferences = Expect<Declared<Web.NutritionSandCPreferences, Api.NutritionSandCPreferences>>;
export type DeclaredNutritionSharedCampContext = Expect<Declared<Web.NutritionSharedCampContext, Api.NutritionSharedCampContext>>;
export type DeclaredNutritionWorkspaceState = Expect<Declared<Web.NutritionWorkspaceState, Api.NutritionWorkspaceState>>;
export type DeclaredPendingRehabResponsesResponse = Expect<Declared<Web.PendingRehabResponsesResponse, Api.PendingRehabResponsesResponse>>;
export type DeclaredPlanAdvisory = Expect<Declared<Web.PlanAdvisory, Api.PlanAdvisory>>;
export type DeclaredPlanCalendar = Expect<Declared<Web.PlanCalendar, Api.PlanCalendar>>;
export type DeclaredPlanCompletionsResponse = Expect<Declared<Web.PlanCompletionsResponse, Api.PlanCompletionsResponse>>;
export type DeclaredPlanDetail = Expect<Declared<Web.PlanDetail, Api.PlanDetail>>;
export type DeclaredExerciseAlternative = Expect<Declared<Web.ExerciseAlternative, Api.ExerciseAlternative>>;
export type DeclaredExerciseChangeResult = Expect<Declared<Web.ExerciseChangeResult, Api.ExerciseChangeResult>>;
export type DeclaredExerciseChangeScope = Expect<Declared<Web.ExerciseChangeScope, Api.ExerciseChangeScope>>;
export type DeclaredExerciseOptionsResponse = Expect<Declared<Web.ExerciseOptionsResponse, Api.ExerciseOptionsResponse>>;
export type DeclaredPlanOutputs = Expect<Declared<Web.PlanOutputs, Api.PlanOutputs>>;
export type DeclaredPlanScheduleContext = Expect<Declared<Web.PlanScheduleContext, Api.PlanScheduleContext>>;
export type DeclaredPlanSummary = Expect<Declared<Web.PlanSummary, Api.PlanSummary>>;
export type DeclaredPriorityMicrodose = Expect<Declared<Web.PriorityMicrodose, Api.PriorityMicrodose>>;
export type DeclaredProfileRecord = Expect<Declared<Web.ProfileRecord, Api.ProfileRecord>>;
export type DeclaredProgressMilestone = Expect<Declared<Web.ProgressMilestone, Api.ProgressMilestone>>;
export type DeclaredRehabLabelPolicy = Expect<Declared<Web.RehabLabelPolicy, Api.RehabLabelPolicy>>;
export type DeclaredRehabResponseResult = Expect<Declared<Web.RehabResponseResult, Api.RehabResponseResult>>;
export type DeclaredExerciseLogActual = Expect<Declared<Web.ExerciseLogActual, Api.ExerciseLogActual>>;
export type DeclaredExerciseLogEffort = Expect<Declared<Web.ExerciseLogEffort, Api.ExerciseLogEffort>>;
export type DeclaredExerciseLogMeasure = Expect<Declared<Web.ExerciseLogMeasure, Api.ExerciseLogMeasure>>;
export type DeclaredExerciseLogRecord = Expect<Declared<Web.ExerciseLogRecord, Api.ExerciseLogRecord>>;
export type DeclaredExerciseHistoryResponse = Expect<Declared<Web.ExerciseHistoryResponse, Api.ExerciseHistoryResponse>>;
export type ReadExerciseHistoryResponse = Reads<Api.ExerciseHistoryResponse, Web.ExerciseHistoryResponse>;
export type DeclaredExerciseLogResponse = Expect<Declared<Web.ExerciseLogResponse, Api.ExerciseLogResponse>>;
export type DeclaredExerciseLogListResponse = Expect<
  Declared<Web.ExerciseLogListResponse, Api.ExerciseLogListResponse>
>;
export type DeclaredExerciseLogBatchResponse = Expect<
  Declared<Web.ExerciseLogBatchResponse, Api.ExerciseLogBatchResponse>
>;
export type DeclaredExerciseRecentLoad = Expect<Declared<Web.ExerciseRecentLoad, Api.ExerciseRecentLoad>>;
export type DeclaredSparringLogRecord = Expect<Declared<Web.SparringLogRecord, Api.SparringLogRecord>>;
export type DeclaredSparringLogResponse = Expect<Declared<Web.SparringLogResponse, Api.SparringLogResponse>>;
export type DeclaredSparringLogHistoryResponse = Expect<
  Declared<Web.SparringLogHistoryResponse, Api.SparringLogHistoryResponse>
>;
export type DeclaredSparringWindowSummary = Expect<Declared<Web.SparringWindowSummary, Api.SparringWindowSummary>>;
export type DeclaredStructuredCardState = Expect<Declared<Web.StructuredCardState, Api.StructuredCardState>>;
export type DeclaredTodayCheckinResponse = Expect<Declared<Web.TodayCheckinResponse, Api.TodayCheckinResponse>>;
export type DeclaredTodayInjuryCheckinResponse = Expect<Declared<Web.TodayInjuryCheckinResponse, Api.TodayInjuryCheckinResponse>>;
export type DeclaredUsernameRateLimitInfo = Expect<Declared<Web.UsernameRateLimitInfo, Api.UsernameRateLimitInfo>>;
export type DeclaredWeeklyDayEntry = Expect<Declared<Web.WeeklyDayEntry, Api.WeeklyDayEntry>>;
export type DeclaredWeeklySchedule = Expect<Declared<Web.WeeklySchedule, Api.WeeklySchedule>>;

export type ReadAdminTimeToFirstPlanSummary = Reads<Api.AdminTimeToFirstPlanSummary, Web.AdminTimeToFirstPlanSummary>;
export type DeclaredAdminTimeToFirstPlanSummary = Expect<Declared<Web.AdminTimeToFirstPlanSummary, Api.AdminTimeToFirstPlanSummary>>;
