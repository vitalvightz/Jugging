"use client";

import { useRouter } from "next/navigation";
import { RequireAuth } from "@/components/auth-guard";
import { useAppSession } from "@/components/auth-provider";
import { PlanImport, stashImportedPlan } from "@/components/plan-import";

// Import a plan from a PDF or Word file. The draft opens in the plan builder,
// where it is checked, edited and saved; this page saves nothing.
export default function PlanImportPage() {
  const { session, me } = useAppSession();
  const router = useRouter();
  return <RequireAuth>
    {session?.access_token ? <PlanImport accessToken={session.access_token} timezone={me?.profile.athlete_timezone}
      onCancel={() => router.push("/create")}
      onReview={(plan, name) => { stashImportedPlan(plan, name); router.push("/plans/custom?imported=1"); }} /> : null}
  </RequireAuth>;
}
