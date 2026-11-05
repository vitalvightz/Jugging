"use client";

import dynamic from "next/dynamic";
import { useEffect, useState } from "react";

import {
  applyPerfQueryFlag,
  isPerfDiagnosticsEnabled,
  PERF_TOGGLE_EVENT,
  setPerfDiagnosticsEnabled,
} from "@/lib/perf-diagnostics";

// The recorder and its panel load only on devices that switched diagnostics on.
const PerfRecorder = dynamic(() => import("./perf-diagnostics-recorder").then(module => module.PerfRecorder), { ssr: false });

function usePerfEnabled(): boolean {
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    const sync = () => setEnabled(isPerfDiagnosticsEnabled());
    applyPerfQueryFlag(window.location.search);
    sync();
    window.addEventListener(PERF_TOGGLE_EVENT, sync);
    return () => window.removeEventListener(PERF_TOGGLE_EVENT, sync);
  }, []);
  return enabled;
}

/** Mounted once in the root layout; renders nothing unless switched on. */
export function PerfDiagnostics() {
  return usePerfEnabled() ? <PerfRecorder /> : null;
}

/** Settings switch: the installed iPhone app has no address bar for `?perf=1`. */
export function PerfDiagnosticsToggle() {
  const enabled = usePerfEnabled();
  return (
    <button type="button" className="ghost-button" aria-pressed={enabled} onClick={() => setPerfDiagnosticsEnabled(!enabled)}>
      {enabled ? "Speed report: on" : "Speed report: off"}
    </button>
  );
}
