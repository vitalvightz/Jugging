"use client";
import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { RequireAuth } from "@/components/auth-guard";
import { useAppSession } from "@/components/auth-provider";
import { CustomPlanBuilder } from "@/components/custom-plan-builder";
import { takeImportedPlan } from "@/components/plan-import";
function Editor() {
  const { session, me } = useAppSession();
  const params = useSearchParams();
  const planId = params.get("plan") ?? undefined;
  const addSessionDay = params.get("addSession");
  const addSessionDate = params.get("date");
  // A plan brought in from a file opens as an unsaved draft.
  const imported = params.get("imported") && !planId ? takeImportedPlan() : null;
  return (
    <RequireAuth>
      {session?.access_token && me ? (
        <CustomPlanBuilder
          key={`${session.user_id}:${planId ?? "new"}:${addSessionDay ?? ""}:${addSessionDate ?? ""}:${imported ? "imported" : ""}`}
          importedDraft={imported ?? undefined}
          accessToken={session.access_token}
          planId={planId}
          addSessionDay={addSessionDay}
          addSessionDate={addSessionDate}
          onboarding={me}
        />
      ) : null}
    </RequireAuth>
  );
}
export default function CustomPlansPage() {
  return (
    <Suspense fallback={<p role="status">Unlxcking plan…</p>}>
      <Editor />
    </Suspense>
  );
}
