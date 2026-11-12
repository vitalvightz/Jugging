"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  appendNavigation,
  readPerfLog,
  resourcePhases,
  sanitizePath,
  sanitizeRequestId,
  setPerfDiagnosticsEnabled,
  summarize,
  writePerfLog,
  type PerfNavigation,
  type PerfReport,
  type PerfRequest,
} from "@/lib/perf-diagnostics";

const PLACEHOLDERS = "[aria-busy='true'], .skeleton, .loading-card";
const SETTLE_GRACE_MS = 300;
const WINDOW_MS = 15_000;

type Tracking = PerfNavigation & { tap: number; targetPath: string; lastFrame: number; settledAt: number | null };

const shown = (element: Element) => element.getClientRects().length > 0;

function visiblePlaceholders(main: Element | null): number {
  if (!main) return 0;
  let count = 0;
  main.querySelectorAll(PLACEHOLDERS).forEach(element => { if (shown(element)) count++; });
  return count;
}

/** Whether the screen shows real text outside any placeholder (text is never recorded). */
function hasContent(main: Element | null): boolean {
  if (!main) return false;
  const walker = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
  let total = 0;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const parent = node.parentElement;
    if (!parent || parent.closest(PLACEHOLDERS) || !shown(parent)) continue;
    total += (node.textContent ?? "").trim().length;
    if (total > 20) return true;
  }
  return false;
}

type FetchRecord = { kind: "api" | "route"; path: string; start: number; ttfb: number | null; end: number | null; bytes: number | null; status: number | null; server: PerfRequest["server"]; requestId: string | null; phases: PerfRequest["phases"] };

/** This request's resource timing entry: same URL, started with or just after the fetch call. */
function phasesFor(href: string, start: number): PerfRequest["phases"] {
  const entries = performance.getEntriesByName(href, "resource") as PerformanceResourceTiming[];
  const entry = entries.filter(candidate => candidate.startTime >= start - 1).sort((a, b) => a.startTime - b.startTime)[0];
  return resourcePhases(entry);
}
const fetchLog: FetchRecord[] = [];

function parseServerTiming(header: string | null): PerfRequest["server"] {
  if (!header) return [];
  return header.split(",").slice(0, 12).flatMap(part => {
    const [name, ...params] = part.trim().split(";");
    const duration = params.map(param => param.trim()).find(param => param.startsWith("dur="));
    const ms = duration ? Number(duration.slice(4)) : NaN;
    return name && Number.isFinite(ms) ? [{ name: name.slice(0, 40), ms: Math.round(ms) }] : [];
  });
}

/**
 * Times this tab's own API and route-payload requests while diagnostics are on.
 * Reads status, size, the Server-Timing header and the API's random
 * X-Request-ID only: never other request or response headers, and never
 * bodies' contents.
 */
function installFetchRecorder(): () => void {
  const original = window.fetch;
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    let url: URL | null = null;
    try { url = new URL(typeof input === "string" || input instanceof URL ? String(input) : input.url, location.origin); } catch { /* not ours */ }
    const kind = url && url.origin === location.origin
      ? url.pathname.startsWith("/api/") ? "api" : url.searchParams.has("_rsc") ? "route" : null
      : null;
    if (!url || !kind) return original(input, init);
    const record: FetchRecord = { kind, path: sanitizePath(url.href, location.origin), start: performance.now(), ttfb: null, end: null, bytes: null, status: null, server: [], requestId: null, phases: null };
    const href = url.href;
    fetchLog.push(record);
    if (fetchLog.length > 400) fetchLog.splice(0, fetchLog.length - 400);
    try {
      const response = await original(input, init);
      record.ttfb = performance.now();
      record.status = response.status;
      record.server = parseServerTiming(response.headers.get("server-timing"));
      record.requestId = sanitizeRequestId(response.headers.get("x-request-id"));
      // Size and completion from a copy, so the app's own read is untouched.
      void response.clone().arrayBuffer()
        .then(buffer => { record.bytes = buffer.byteLength; })
        .catch(() => undefined)
        .finally(() => {
          record.end = performance.now();
          // The resource entry is queued after the body ends; read it next task.
          setTimeout(() => { record.phases = phasesFor(href, record.start); }, 0);
        });
      return response;
    } catch (error) {
      record.end = performance.now();
      throw error;
    }
  };
  return () => { window.fetch = original; };
}

function requestsSince(tap: number, until: number): PerfRequest[] {
  return fetchLog
    .filter(record => record.start >= tap - 5 && record.start <= until)
    .map(record => ({
      kind: record.kind,
      path: record.path,
      start: Math.round(record.start - tap),
      ttfb: record.ttfb === null ? null : Math.round(record.ttfb - tap),
      end: record.end === null ? -1 : Math.round(record.end - tap),
      bytes: record.bytes,
      status: record.status,
      server: record.server,
      requestId: record.requestId,
      phases: record.phases,
    }));
}

function appStart(): Record<string, number | null> {
  const nav = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming | undefined;
  const round = (value: number | undefined) => (typeof value === "number" && value > 0 ? Math.round(value) : null);
  return {
    htmlFirstByte: round(nav?.responseStart),
    htmlDone: round(nav?.responseEnd),
    domInteractive: round(nav?.domInteractive),
    domContentLoaded: round(nav?.domContentLoadedEventEnd),
    loaded: round(nav?.loadEventEnd),
    htmlBytes: round(nav?.transferSize),
    firstApiRequest: round((performance.getEntriesByType("resource") as PerformanceResourceTiming[])
      .find(entry => entry.name.includes("/api/"))?.startTime),
    recorderStarted: round(performance.now()),
  };
}

function buildReport(navigations: PerfNavigation[], start: Record<string, number | null>): PerfReport {
  const nav = navigator as Navigator & { deviceMemory?: number; connection?: { effectiveType?: string } };
  return {
    version: 1,
    createdAt: new Date().toISOString(),
    build: document.querySelector<HTMLScriptElement>("script[src*='/_next/static/']")?.src.split("/_next/static/")[1]?.split("/")[0] ?? "unknown",
    device: {
      userAgent: navigator.userAgent,
      standalone: window.matchMedia("(display-mode: standalone)").matches || Boolean((navigator as Navigator & { standalone?: boolean }).standalone),
      viewport: `${window.innerWidth}x${window.innerHeight}`,
      pixelRatio: window.devicePixelRatio,
      cpuCores: navigator.hardwareConcurrency ?? null,
      memoryGb: nav.deviceMemory ?? null,
      connection: nav.connection?.effectiveType ?? null,
    },
    appStart: start,
    navigations,
    summary: summarize(navigations),
  };
}

/** Records every in-app navigation from tap to settled screen. Diagnostic mode only. */
export function PerfRecorder() {
  const [log, setLog] = useState<PerfNavigation[]>(() => readPerfLog());
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);
  const tracking = useRef<Tracking | null>(null);
  const [start] = useState(appStart);

  const finish = useCallback((entry: Tracking, timedOut: boolean) => {
    const { tap, targetPath: _target, lastFrame: _frame, settledAt: _settled, ...navigation } = entry;
    void _target; void _frame; void _settled;
    const done: PerfNavigation = { ...navigation, timedOut, requests: requestsSince(tap, tap + WINDOW_MS) };
    setLog(current => {
      const next = appendNavigation(current, done);
      writePerfLog(next);
      return next;
    });
  }, []);

  useEffect(() => {
    const uninstall = installFetchRecorder();
    let frame = 0;
    const loop = (now: number) => {
      const entry = tracking.current;
      if (entry) {
        const gap = now - entry.lastFrame;
        if (gap > 50) entry.blockedMs += Math.round(gap);
        entry.longestFrameMs = Math.max(entry.longestFrameMs, Math.round(gap));
        entry.lastFrame = now;
        const since = Math.round(now - entry.tap);
        const main = document.querySelector("main");
        if (entry.routeChanged === null && location.pathname === entry.targetPath) {
          entry.routeChanged = since;
          entry.placeholdersAtStart = visiblePlaceholders(main);
        } else if (entry.routeChanged !== null) {
          if (entry.firstFrame === null) entry.firstFrame = since;
          if (entry.firstContent === null && hasContent(main)) entry.firstContent = since;
          if (entry.firstContent !== null && entry.placeholdersGone === null && visiblePlaceholders(main) === 0) {
            entry.placeholdersGone = since;
            entry.settledAt = now;
          }
        }
        if (entry.settledAt !== null && now - entry.settledAt > SETTLE_GRACE_MS) {
          tracking.current = null;
          finish(entry, false);
        } else if (since > WINDOW_MS) {
          tracking.current = null;
          finish(entry, true);
        }
      }
      frame = requestAnimationFrame(loop);
    };
    frame = requestAnimationFrame(loop);

    const onClick = (event: MouseEvent) => {
      const link = (event.target as Element | null)?.closest?.("a[href]") as HTMLAnchorElement | null;
      if (!link || link.closest("[data-perf-panel]")) return;
      let url: URL;
      try { url = new URL(link.href); } catch { return; }
      if (url.origin !== location.origin || url.pathname === location.pathname) return;
      const tap = performance.now();
      if (tracking.current) finish({ ...tracking.current, interrupted: true }, false);
      tracking.current = {
        from: sanitizePath(location.pathname), to: sanitizePath(url.pathname), at: new Date().toISOString(),
        routeChanged: null, firstFrame: null, firstContent: null, placeholdersGone: null, placeholdersAtStart: 0,
        blockedMs: 0, longestFrameMs: 0, requests: [], timedOut: false,
        tap, targetPath: url.pathname, lastFrame: tap, settledAt: null,
      };
    };
    document.addEventListener("click", onClick, true);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("click", onClick, true);
      uninstall();
    };
  }, [finish]);

  const report = () => JSON.stringify(buildReport(log, start), null, 2);
  const share = async () => {
    const text = report();
    try {
      if (navigator.share) {
        await navigator.share({ title: "UNLXCK speed report", text });
        setCopied("Shared");
        return;
      }
    } catch { /* fall through to copy */ }
    try {
      await navigator.clipboard.writeText(text);
      setCopied("Copied");
    } catch {
      setCopied("Copy failed — use Show report");
    }
  };
  const summary = summarize(log);

  return (
    <div data-perf-panel style={{ position: "fixed", left: 8, bottom: "calc(env(safe-area-inset-bottom, 0px) + 76px)", zIndex: 2147483000, fontSize: 12 }}>
      <button type="button" className="ghost-button" style={{ padding: "4px 8px", fontSize: 12 }} onClick={() => setOpen(value => !value)}>
        ⏱ {log.length}
      </button>
      {open ? (
        <div role="dialog" aria-label="Speed report" style={{ marginTop: 6, width: "min(92vw, 360px)", maxHeight: "60vh", overflow: "auto", background: "var(--surface, #111)", border: "1px solid rgba(255,255,255,0.2)", borderRadius: 10, padding: 10 }}>
          <p style={{ margin: "0 0 6px" }}>Last {log.length} navigations (ms from tap: content / settled / route / API).</p>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <tbody>
              {summary.map(row => (
                <tr key={row.route}>
                  <td>{row.route} ×{row.visits}</td>
                  <td style={{ textAlign: "right" }}>{row.medianContentMs ?? "–"} / {row.medianSettledMs ?? "–"} / {row.medianRouteMs ?? "–"} / {row.medianApiMs ?? "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
            <button type="button" className="cta" onClick={() => void share()}>Share report</button>
            <button type="button" className="ghost-button" onClick={() => { writePerfLog([]); setLog([]); }}>Clear</button>
            <button type="button" className="ghost-button" onClick={() => setPerfDiagnosticsEnabled(false)}>Turn off</button>
          </div>
          {copied ? <p role="status">{copied}</p> : null}
          <details>
            <summary>Show report</summary>
            <pre style={{ whiteSpace: "pre-wrap", fontSize: 10, userSelect: "all" }}>{report()}</pre>
          </details>
        </div>
      ) : null}
    </div>
  );
}
