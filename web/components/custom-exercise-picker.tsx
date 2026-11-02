"use client";
import { useEffect, useState } from "react";
import { searchTrainingLibrary } from "@/lib/api";
import type { LibraryEntry } from "@/lib/api-schema.generated";
import { libraryEquipmentText as equipmentText } from "@/lib/equipment-requirements";
import styles from "./custom-plan-builder.module.css";

/** Enough to pick from on a phone; the full library holds the rest. */
const SHOWN = 8;

/**
 * Library search inside the custom-plan add sheet. "+" adds in one tap and
 * keeps the search, so several exercises go in without leaving the session;
 * the name opens the preview. The full library (filters, community) stays
 * one tap away through `onBrowse`.
 */
export function CustomExercisePicker({ accessToken, full, initialQuery = "", onAdd, onPreview, onCreate, onBrowse }: {
  accessToken: string;
  /** The session holds its maximum exercises. */
  full: boolean;
  initialQuery?: string;
  onAdd: (entry: LibraryEntry) => boolean;
  onPreview: (entry: LibraryEntry) => void;
  onCreate: (name: string) => void;
  onBrowse: (query: string) => void;
}) {
  const [query, setQuery] = useState(initialQuery);
  const [found, setFound] = useState<{ term: string; entries: LibraryEntry[]; total: number } | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const [added, setAdded] = useState("");
  const term = query.trim();
  useEffect(() => {
    if (!term) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      const params = new URLSearchParams({ q: term, category: "training", for_plan: "true" });
      void searchTrainingLibrary(accessToken, params, controller.signal).then(result => {
        if (controller.signal.aborted) return;
        setFound({ term, entries: result.entries.filter(entry => entry.category === "training"), total: result.total });
        setFailed(null);
      }).catch(() => {
        if (!controller.signal.aborted) { setFound(null); setFailed(term); }
      });
    }, 200);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [accessToken, term, retry]);
  // Results always belong to the current query; never offer stale exercises while searching.
  const results = found?.term === term && failed !== term ? found : null;
  return <div className={styles.picker}>
    <label className={styles.pickerSearch}>
      <span className="sr-only">Search exercises</span>
      <input type="search" value={query} maxLength={100} placeholder="Search the library" autoComplete="off" enterKeyHint="search"
        onChange={event => { setQuery(event.target.value); setFound(null); setFailed(null); setAdded(""); }} />
    </label>
    <p className="sr-only" role="status" aria-live="polite">{added ? `Added ${added}` : ""}</p>
    {failed === term && term ? <div className={styles.pickerNotice} role="alert">
      <span>Library search unavailable.</span>
      <button type="button" className={styles.textButton} onClick={() => setRetry(value => value + 1)}>Retry</button>
    </div> : null}
    {term && !results && failed !== term ? <p className={styles.pickerNotice} role="status">Searching…</p> : null}
    {results ? results.entries.length ? <ul className={styles.results} aria-label="Library results">
      {results.entries.slice(0, SHOWN).map(entry => <li key={entry.catalogue_id} className={styles.result}>
        <button type="button" className={styles.resultMain} aria-label={`Preview ${entry.name}`} aria-haspopup="dialog" onClick={() => onPreview(entry)}>
          <span className={styles.resultName}>{entry.name}</span>
          <span className={styles.resultMeta}>{equipmentText(entry)}</span>
        </button>
        <button type="button" className={styles.resultAdd} aria-label={`Add ${entry.name}`} disabled={full}
          onClick={() => { if (onAdd(entry)) setAdded(entry.name); }}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
        </button>
      </li>)}
    </ul> : <p className={styles.pickerNotice}>No library match for “{results.term}”.</p> : null}
    {results && results.total > Math.min(SHOWN, results.entries.length) ? <button type="button" className={styles.textButton} onClick={() => onBrowse(term)}>
      See all {results.total.toLocaleString()} in the library
    </button> : null}
    {full ? <p className={styles.pickerNotice}>This session has the maximum 100 exercises.</p> : null}
    <div className={styles.pickerLinks}>
      <button type="button" className={styles.textButton} disabled={full} onClick={() => onCreate(term)}>
        + {term ? `Create “${term}”` : "Create my exercise"}
      </button>
      <button type="button" className={styles.textButton} onClick={() => onBrowse(term)}>Filters &amp; community</button>
    </div>
  </div>;
}
