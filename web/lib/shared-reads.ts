/**
 * Request sharing for a few read-only API calls.
 *
 * - Identical reads in flight together share one network request.
 * - A caller may opt in to a result resolved at most `reuseForMs` ago.
 *
 * Both are keyed by the bearer token and the full request path, so separate
 * sessions or accounts never share a request or a result. Both also end at
 * the first write: every non-neutral API write starts a new epoch, drops held
 * results, and stops later callers joining reads that started before it. While
 * a write is in flight nothing is shared at all. Results live in memory only,
 * for at most SHARED_READ_MAX_AGE_MS, and never more than MAX_HELD of them.
 * See docs/performance/frontend-optimisation-results.md.
 */

export const SHARED_READ_MAX_AGE_MS = 30_000;
const MAX_HELD = 12;
/**
 * How long a screen may show its last response while it reads again. A
 * remembered view is display only: every caller still starts a fresh read on
 * every visit and replaces the view with it. Writes and sign-out drop it.
 */
export const REMEMBERED_VIEW_MAX_AGE_MS = 10 * 60_000;
const MAX_REMEMBERED = 8;

type InFlight = {
  promise: Promise<unknown>;
  controller: AbortController;
  consumers: number;
  settled: boolean;
};

const inFlight = new Map<string, InFlight>();
const held = new Map<string, { value: unknown; at: number }>();
const remembered = new Map<string, { value: unknown; at: number; epoch: number }>();
let writeEpoch = 0;
let writesInFlight = 0;

// Monotonic: a wall-clock change can neither extend nor revive a held result.
const now = () => performance.now();

function abortReason(signal: AbortSignal): unknown {
  return signal.reason ?? new DOMException("The operation was aborted.", "AbortError");
}

function pruneHeld() {
  const oldest = now() - SHARED_READ_MAX_AGE_MS;
  for (const [key, entry] of held) if (entry.at < oldest) held.delete(key);
}

function hold(key: string, value: unknown) {
  held.delete(key);
  held.set(key, { value, at: now() });
  while (held.size > MAX_HELD) held.delete(held.keys().next().value!);
}

function consume<T>(key: string, entry: InFlight, signal?: AbortSignal): Promise<T> {
  entry.consumers += 1;
  return new Promise<T>((resolve, reject) => {
    let done = false;
    const finish = () => {
      done = true;
      entry.consumers -= 1;
      signal?.removeEventListener("abort", onAbort);
    };
    // One consumer leaving never cancels another's read; the last one to leave
    // cancels the network request nobody is waiting for any more.
    const onAbort = () => {
      if (done) return;
      finish();
      if (entry.consumers === 0 && !entry.settled) {
        if (inFlight.get(key) === entry) inFlight.delete(key);
        entry.controller.abort();
      }
      reject(abortReason(signal!));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
    entry.promise.then(
      (value) => { if (!done) { finish(); resolve(value as T); } },
      (error) => { if (!done) { finish(); reject(error); } },
    );
  });
}

export type SharedReadOptions = {
  /** Cancels this caller's wait; the read continues while others need it. */
  signal?: AbortSignal;
  /** Keep the resolved result so callers passing `reuseForMs` can use it. */
  hold?: boolean;
  /** Accept a result resolved this recently (capped at SHARED_READ_MAX_AGE_MS). */
  reuseForMs?: number;
  /** Keep the result for `recallView` (display while re-reading only). */
  remember?: boolean;
};

export function sharedRead<T>(
  token: string,
  path: string,
  load: (signal: AbortSignal) => Promise<T>,
  { signal, hold: holdResult = false, reuseForMs = 0, remember = false }: SharedReadOptions = {},
): Promise<T> {
  // An already-cancelled caller gets nothing and starts nothing.
  if (signal?.aborted) return Promise.reject(abortReason(signal));
  const key = `${token}\u0000${path}`;
  const sharing = writesInFlight === 0;
  if (sharing && reuseForMs > 0) {
    pruneHeld();
    const result = held.get(key);
    if (result && now() - result.at <= Math.min(reuseForMs, SHARED_READ_MAX_AGE_MS)) {
      return Promise.resolve(result.value as T);
    }
  }
  let entry = sharing ? inFlight.get(key) : undefined;
  if (!entry) {
    const epoch = writeEpoch;
    const controller = new AbortController();
    const created: InFlight = { controller, consumers: 0, settled: false, promise: Promise.resolve() };
    created.promise = load(controller.signal)
      .then((value) => {
        // Only a read that began and ended with no write in between is reusable.
        if (epoch === writeEpoch && writesInFlight === 0) {
          if (holdResult) hold(key, value);
          if (remember) {
            remembered.delete(key);
            remembered.set(key, { value, at: now(), epoch });
            while (remembered.size > MAX_REMEMBERED) remembered.delete(remembered.keys().next().value!);
          }
        }
        return value;
      })
      .finally(() => {
        created.settled = true;
        if (inFlight.get(key) === created) inFlight.delete(key);
      });
    if (sharing) inFlight.set(key, created);
    entry = created;
  }
  return consume<T>(key, entry, signal);
}

/**
 * The last response remembered for this token and path, if no write has
 * started since it was read and it is recent. For showing a revisited screen
 * at once while its fresh read runs — never a substitute for that read.
 */
export function recallView<T>(token: string, path: string, maxAgeMs = REMEMBERED_VIEW_MAX_AGE_MS): T | null {
  const entry = remembered.get(`${token}\u0000${path}`);
  if (!entry || entry.epoch !== writeEpoch || writesInFlight > 0) return null;
  if (now() - entry.at > Math.min(maxAgeMs, REMEMBERED_VIEW_MAX_AGE_MS)) return null;
  return entry.value as T;
}

/**
 * The write epoch to pass to `rememberView` from a screen that combines several
 * reads. Taken before the reads start; -1 while a write is in flight.
 */
export function readEpoch(): number {
  return writesInFlight > 0 ? -1 : writeEpoch;
}

/**
 * Remembers a screen's combined view under `key` for `recallView`, unless a
 * write started since `startedAt` (from `readEpoch`). Display only.
 */
export function rememberView(token: string, key: string, value: unknown, startedAt: number): void {
  if (startedAt < 0 || startedAt !== writeEpoch || writesInFlight > 0) return;
  const id = `${token}\u0000${key}`;
  remembered.delete(id);
  remembered.set(id, { value, at: now(), epoch: writeEpoch });
  while (remembered.size > MAX_REMEMBERED) remembered.delete(remembered.keys().next().value!);
}

function invalidate() {
  writeEpoch += 1;
  held.clear();
  remembered.clear();
  inFlight.clear();
}

/** Marks an API write. Call the returned function once it has settled. */
export function beginSharedReadWrite(): () => void {
  writesInFlight += 1;
  invalidate();
  let ended = false;
  return () => {
    if (ended) return;
    ended = true;
    writesInFlight -= 1;
    invalidate();
  };
}

/** Forget everything, e.g. at sign-out. In-flight callers still settle. */
export function clearSharedReads() {
  invalidate();
}
