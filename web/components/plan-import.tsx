"use client";

import { useEffect, useRef, useState, type CSSProperties } from "react";
import { ApiError, importPlanText, type PlanImportResult } from "@/lib/api";
import type { CustomPlanInput, ImportFlag, NeedsDay } from "@/lib/api-schema.generated";
import { addCalendarDays, calendarMonday, calendarWeekdays, nextCalendarMonday } from "@/lib/custom-plan-calendar";
import { describeLoadFailure } from "@/lib/connectivity";
import { MAX_SCREENSHOTS, PLAN_TEXT_MAX_CHARS, PlanFileError, extractPlanText, planFileKind, screenshotData } from "@/lib/plan-text";
import styles from "./plan-import.module.css";

type Busy = { step: "reading" | "building"; name: string; kind: "file" | "images" | "paste"; lines?: string[]; shots?: string[] };
type Stage = { step: "pick" } | Busy
  | { step: "done"; name: string; result: PlanImportResult };

/**
 * Import a plan someone already follows: a PDF or Word file (its text is read
 * on this device), screenshots (redrawn as plain JPEGs, read once, never kept)
 * or pasted text. It's turned into weeks and sessions and opened in the
 * builder to check and edit. Nothing is saved until they save it there.
 */
export function PlanImport({ accessToken, timezone, onReview, onCancel, forFighter }: {
  accessToken: string;
  timezone?: string | null;
  onReview: (imported: ImportedPlan) => void;
  onCancel: () => void;
  /** A coach importing for a fighter: the wording names them. */
  forFighter?: string | null;
}) {
  const [stage, setStage] = useState<Stage>({ step: "pick" });
  const [problem, setProblem] = useState<string | null>(null);
  const [over, setOver] = useState(false);
  const [pasting, setPasting] = useState(false);
  const [pasted, setPasted] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);

  /** One PDF or Word file, or up to four screenshots; or pasted text. */
  async function run(source: { files: File[] } | { text: string }) {
    setProblem(null);
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const files = "files" in source ? source.files : [];
    const images = files.filter(file => planFileKind(file) === "image");
    const name = "text" in source ? "pasted text"
      : images.length ? images.length === 1 ? images[0].name : `${images.length} screenshots` : files[0].name;
    try {
      if (files.length > 1 && images.length !== files.length) throw new PlanFileError("Choose one PDF or Word file, or up to 4 screenshots.");
      if (images.length > MAX_SCREENSHOTS) throw new PlanFileError(`Choose up to ${MAX_SCREENSHOTS} screenshots.`);
      setStage({ step: "reading", name, kind: images.length ? "images" : "text" in source ? "paste" : "file",
        lines: "text" in source ? previewLines(source.text) : undefined });
      const text = "text" in source ? source.text.slice(0, PLAN_TEXT_MAX_CHARS) : images.length ? "" : await extractPlanText(files[0]);
      if ("text" in source && text.replace(/\s/g, "").length < 20) throw new PlanFileError("Paste the whole programme: sessions, days and exercises.");
      const shots = await Promise.all(images.map(screenshotData));
      if (controller.signal.aborted) return;
      setStage({ step: "building", name, kind: images.length ? "images" : "text" in source ? "paste" : "file", lines: previewLines(text), shots });
      const result = await importPlanText(accessToken, {
        text, images: shots, file_name: name.slice(0, 200), start_date: nextCalendarMonday(timezone ?? "UTC"),
      }, controller.signal);
      if (!controller.signal.aborted) { setStage({ step: "done", name, result }); setPasting(false); setPasted(""); }
    } catch (error) {
      if (controller.signal.aborted) return;
      setStage({ step: "pick" });
      setProblem(error instanceof PlanFileError ? error.message
        : error instanceof ApiError && error.status < 500 ? error.message.replace(/\s*\(request id: [^)]*\)$/, "")
        : describeLoadFailure(error, "That plan couldn't be read just now. Try again in a minute."));
    } finally {
      if (input.current) input.current.value = "";
    }
  }

  function take(list: FileList | null | undefined) {
    const files = Array.from(list ?? []);
    if (files.length) void run({ files });
  }

  const busy = stage.step === "reading" || stage.step === "building";
  return <section className={styles.root} aria-labelledby="plan-import-heading">
    <header className={styles.head}>
      <p className="kicker">Import</p>
      <h1 id="plan-import-heading">Bring in {forFighter ? `${forFighter}'s` : "your"} plan</h1>
      <p className={styles.lede}>Upload your training programme or paste it below. We’ll turn it into an editable plan for you to review.</p>
    </header>

    {stage.step === "done" ? <Result stage={stage} onReview={days => onReview({ plan: withDays(stage.result.plan, stage.result.needs_day, days), name: stage.name, flags: stage.result.flags, library: stage.result.library })}
      onAnother={() => setStage({ step: "pick" })} />
      : <>
        {pasting && !busy ? <form className={styles.paste} onSubmit={event => { event.preventDefault(); void run({ text: pasted }); }}>
          <label htmlFor="plan-import-paste">Programme text</label>
          <textarea id="plan-import-paste" value={pasted} onChange={event => setPasted(event.target.value)} rows={10}
            maxLength={PLAN_TEXT_MAX_CHARS} placeholder={"Mon: Strength\nTrap bar deadlift 3x5 @ 140kg\nWed: Pads 6 x 3 min rounds"} autoFocus />
          <div className={styles.actions}>
            <button type="submit" className="cta" disabled={!pasted.trim()}>Import text</button>
            <button type="button" className={styles.cancel} onClick={() => setPasting(false)}>Upload a file instead</button>
          </div>
        </form>
        : <div className={styles.drop} data-over={over || undefined} data-busy={busy || undefined}
          onDragOver={event => { event.preventDefault(); if (!busy) setOver(true); }} onDragLeave={() => setOver(false)}
          onDrop={event => { event.preventDefault(); setOver(false); if (!busy) take(event.dataTransfer.files); }}>
          {busy ? <ImportProgress stage={stage} /> : <>
            <svg className={styles.fileIcon} viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5M9 13h6M9 17h4" /></svg>
            <button type="button" className="cta" onClick={() => input.current?.click()}>Choose a file</button>
            <span className={styles.hint}>PDF, Word (.docx) or up to {MAX_SCREENSHOTS} screenshots</span>
            <button type="button" className={styles.link} onClick={() => { setProblem(null); setPasting(true); }}>Paste text instead</button>
          </>}
        </div>}
        <input ref={input} type="file" hidden multiple aria-label="Plan file (PDF, Word or screenshots)"
          accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,image/jpeg,image/png,image/webp,image/heic,image/heif"
          onChange={event => take(event.target.files)} />
        {problem ? <p className={styles.problem} role="alert">{problem}</p> : null}
        <details className={styles.handling}>
          <summary>How your files are handled</summary>
          <ul className={styles.notes}>
            <li>PDF and Word files stay on this device: only their text is sent. Screenshots are sent once to read them and aren&apos;t kept.</li>
            <li>For a scanned or paper plan, take screenshots or photos of each page.</li>
          </ul>
        </details>
        <button type="button" className={styles.cancel} onClick={() => { request.current?.abort(); onCancel(); }}>
          {busy ? "Cancel import" : "Back"}</button>
      </>}
  </section>;
}

/** The first lines of the programme, shown being read while it's processed. */
function previewLines(text: string) {
  return text.split("\n").map(line => line.replace(/\s+/g, " ").trim()).filter(Boolean).slice(0, 9)
    .map(line => line.length > 64 ? `${line.slice(0, 63)}…` : line);
}

const TICKER: Record<Busy["step"], string[]> = {
  reading: ["Opening it on this device", "Pulling out the text", "Lining up the pages"],
  building: ["Finding the weeks and sessions", "Reading sets, reps and rounds", "Matching each session to its day",
    "Keeping the coach's cues and notes", "Fixing spelling, never the numbers", "Flagging anything unclear"],
};

/** The programme being read: its own lines (or screenshots) under a scan, a
 * running clock, and what's happening now. Motion stops for reduced motion. */
function ImportProgress({ stage }: { stage: Busy }) {
  const [seconds, setSeconds] = useState(0);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const started = Date.now();
    const timer = window.setInterval(() => {
      setSeconds(Math.floor((Date.now() - started) / 1000));
      setTick(Math.floor((Date.now() - started) / 2400));
    }, 400);
    return () => window.clearInterval(timer);
  }, [stage.name]);
  const messages = TICKER[stage.step];
  const message = messages[tick % messages.length];
  const lines = stage.lines?.length ? stage.lines : null;
  const shots = stage.shots?.length ? stage.shots : null;
  const clock = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  const phase = stage.step === "reading" ? 0 : 1;
  return <div className={styles.progress} role="status" aria-live="polite">
    <div className={styles.progressHead}>
      <span className={styles.live}><i aria-hidden="true" />{stage.step === "reading" ? "Reading" : "Building"}</span>
      <span className={styles.clock} aria-hidden="true">{clock}</span>
    </div>
    <div className={styles.doc} data-kind={shots ? "images" : lines ? "lines" : "blank"} aria-hidden="true"
      style={{ "--n": lines?.length ?? 7 } as CSSProperties}>
      {shots ? <div className={styles.shots}>{shots.map((shot, index) =>
        // eslint-disable-next-line @next/next/no-img-element -- a local data URL, never fetched
        <img key={index} src={shot} alt="" />)}</div>
        : lines ? lines.map((line, index) => <span key={index} className={styles.line} style={{ "--i": index } as CSSProperties}>{line}</span>)
        : Array.from({ length: 7 }, (_, index) => <span key={index} className={styles.bar} style={{ "--i": index, width: `${[72, 54, 88, 40, 66, 80, 48][index]}%` } as CSSProperties} />)}
      <span className={styles.scan} />
    </div>
    <p className={styles.progressTitle}>
      {stage.step === "reading" ? "Getting it ready" : "Building weeks and sessions"}
      <span className={styles.fileName}>{stage.name}</span>
    </p>
    <p key={message} className={styles.ticker}>{message}…</p>
    <ol className={styles.rail} aria-label="Import steps">
      {["Read", "Build", "You check"].map((label, index) =>
        <li key={label} data-state={index < phase ? "done" : index === phase ? "now" : "next"}><span>{label}</span></li>)}
    </ol>
    <p className={styles.wait}>{stage.step === "building" ? "Usually under a minute. Nothing is saved until you check it." : "This stays on your device."}</p>
  </div>;
}

type Weekday = (typeof calendarWeekdays)[number];

/** Put each session the coach left without a day on the day the person picked. */
function withDays(plan: CustomPlanInput, groups: NeedsDay[], days: Weekday[]): CustomPlanInput {
  const chosen = new Map(groups.flatMap((group, index) => group.session_ids.map(id => [id, days[index]] as const)));
  return { ...plan, sessions: (plan.sessions ?? []).map(session => {
    const day = chosen.get(session.id);
    if (!day) return session;
    return session.scheduled_date
      ? { ...session, weekday: day, scheduled_date: addCalendarDays(calendarMonday(session.scheduled_date), calendarWeekdays.indexOf(day)) }
      : { ...session, weekday: day };
  }) };
}

function Result({ stage, onReview, onAnother }: {
  stage: Extract<Stage, { step: "done" }>; onReview: (days: Weekday[]) => void; onAnother: () => void;
}) {
  const { result } = stage;
  const weekly = result.plan.schedule_mode === "weekly";
  const [days, setDays] = useState<(Weekday | "")[]>(() => result.needs_day.map(() => ""));
  const left = days.filter(day => !day).length;
  const firstDate = weekly ? null : (result.plan.sessions ?? []).map(s => s.scheduled_date).filter(Boolean).sort()[0];
  const starts = firstDate && result.plan.start_date && firstDate !== result.plan.start_date
    ? `First session ${dayLabel(firstDate)}. Calendar weeks start Monday ${dayLabel(result.plan.start_date, false)}.` : null;
  return <div className={styles.result}>
    <div className={styles.summary} role="status">
      <p className={styles.found}>Found in <strong className={styles.fileName}>{stage.name}</strong></p>
      <dl className={styles.counts}>
        <div><dt>{weekly ? "Repeats" : "Weeks"}</dt><dd>{weekly ? "Weekly" : result.weeks}</dd></div>
        <div><dt>Sessions</dt><dd>{result.sessions}</dd></div>
        <div><dt>Exercises</dt><dd>{result.exercises}</dd></div>
      </dl>
      <p className={styles.planName}>{result.plan.name}</p>
      {starts ? <p className={styles.matched}>{starts}</p> : null}
      {result.matched ? <p className={styles.matched}>{result.matched} {result.matched === 1 ? "exercise" : "exercises"} matched to the exercise library, with demos ready where there is one.</p> : null}
    </div>
    {result.needs_day.length ? <fieldset className={styles.days}>
      <legend>Choose a day</legend>
      <p>No day was written for {result.needs_day.length === 1 ? "this session" : "these sessions"}. Pick one{weekly ? "" : " and it applies to every week it repeats"}.</p>
      {result.needs_day.map((group, index) => <label key={group.session_ids[0]} className={styles.dayRow}>
        <span>{group.where}</span>
        <select value={days[index]} onChange={event => setDays(current => current.map((day, at) => at === index ? event.target.value as Weekday : day))}>
          <option value="" disabled>Choose a day</option>
          {calendarWeekdays.map(day => <option key={day}>{day}</option>)}
        </select>
      </label>)}
    </fieldset> : null}
    {result.flags.length ? <ImportFlags flags={result.flags} open /> : null}
    <p className={styles.check}>{result.flags.length ? "Everything else came in as written." : "Everything came in as written."} Check each session against your file before saving.</p>
    <div className={styles.actions}>
      <button type="button" className="cta" disabled={left > 0} onClick={() => onReview(days as Weekday[])}>Review and edit</button>
      <button type="button" className={styles.cancel} onClick={onAnother}>Choose another file</button>
    </div>
    {left ? <p className={styles.hint}>Choose {left === 1 ? "a day" : `${left} days`} above to continue.</p> : null}
  </div>;
}

/** What couldn't be carried over exactly, grouped by where it belongs. */
export function ImportFlags({ flags, open, checked, onCheck, onCheckAll, onOpen }: {
  flags: ImportFlag[]; open?: boolean;
  /** In the builder: tapping a title opens that session or exercise. */
  onOpen?: (flag: ImportFlag) => void;
  /** In the builder: each one is ticked once it's been fixed or checked, and saving waits for all of them. */
  checked?: number[]; onCheck?: (index: number, done: boolean) => void;
  /** Ticks (or unticks) every flag at once; each can still be unticked on its own. */
  onCheckAll?: (done: boolean) => void;
}) {
  const groups = new Map<string, { message: string; index: number }[]>();
  flags.forEach((flag, index) => groups.set(flag.where, [...(groups.get(flag.where) ?? []), { message: flag.message, index }]));
  const left = onCheck ? flags.length - (checked?.length ?? 0) : 0;
  return <details className={styles.flags} open={open}>
    <summary>{flags.length} {flags.length === 1 ? "thing" : "things"} to check{onCheck ? left ? ` · ${left} left` : " · all checked" : ""}</summary>
    {onCheck && onCheckAll ? <div className={styles.flagTools}>
      <button type="button" className={styles.tickAll} onClick={() => onCheckAll(left > 0)}>{left > 0 ? "Tick all" : "Untick all"}</button>
    </div> : null}
    <dl>{[...groups].map(([where, items]) => <div key={where}><dt>{onOpen && flags[items[0].index].session_id
      ? <button type="button" className={styles.flagLink} onClick={() => onOpen(flags[items[0].index])}>{where}</button> : where}</dt>
      <dd>{items.map(({ message, index }) => onCheck
        ? <label key={index} className={styles.tick}><input type="checkbox" checked={checked?.includes(index) ?? false}
          onChange={event => onCheck(index, event.target.checked)} /><span>{message}</span></label>
        : <span key={index}>{message}</span>)}</dd></div>)}</dl>
  </details>;
}

export type ImportedPlan = { plan: CustomPlanInput; name: string; flags: ImportFlag[]; library: PlanImportResult["library"] };

// The imported draft travels to the builder page: in memory for an in-app
// move, and in this tab's session storage so a reload doesn't lose it.
const KEY = "unlxck:imported-plan";
let pending: ImportedPlan | null = null;

export function stashImportedPlan(imported: ImportedPlan) {
  pending = imported;
  try { sessionStorage.setItem(KEY, JSON.stringify(pending)); } catch { /* memory copy is enough */ }
}

export function takeImportedPlan(): ImportedPlan | null {
  let value = pending;
  if (!value) {
    try { value = JSON.parse(sessionStorage.getItem(KEY) ?? "null"); } catch { value = null; }
  }
  return value && Array.isArray(value.plan?.sessions) ? { ...value, flags: Array.isArray(value.flags) ? value.flags : [] } : null;
}

export function clearImportedPlan() {
  pending = null;
  try { sessionStorage.removeItem(KEY); } catch { /* nothing stored */ }
}

function dayLabel(iso: string, weekday = true) {
  const d = new Date(`${iso}T00:00:00Z`);
  return d.toLocaleDateString("en-GB", { timeZone: "UTC", day: "numeric", month: "short", ...(weekday ? { weekday: "long" } : {}) });
}
