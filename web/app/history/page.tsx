"use client";

import { Suspense } from "react";
import { RequireAuth } from "@/components/auth-guard";
import { HistoryScreen } from "@/components/history-screen";

export default function HistoryPage() {
  return (
    <RequireAuth>
      <Suspense fallback={null}><HistoryScreen /></Suspense>
    </RequireAuth>
  );
}
