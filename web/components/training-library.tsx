"use client";

import Link from "next/link";
import { CommunityDiscovery } from "./community-exercises";
import type { CommunityEntry } from "@/lib/api-schema.generated";
import { useEffect, useRef, useState } from "react";
import { ApiError, getTrainingLibraryEntry, searchTrainingLibrary } from "@/lib/api";
import type { LibraryEntry, LibraryPreview, LibrarySearchResponse } from "@/lib/api-schema.generated";
import { ExerciseDemo } from "./exercise-demo";
import { ExerciseGlyphIcon, type ExerciseGlyphName } from "./exercise-glyph";
import { demoThumbnailUrl } from "@/lib/exercise-demo";
import { AutoHeight } from "./today/auto-height";
import { InjuryDetailSheet } from "./today/injury-detail-sheet";
import { libraryEquipmentText as equipmentText } from "@/lib/equipment-requirements";
import styles from "./training-library.module.css";

type Category = LibraryEntry["category"];
const filterLabels = { type: "Training type", goal: "Goal", sport: "Sport", equipment: "Equipment", body_focus: "Body area", movement: "Movement", training_method: "Training method", skill_focus: "Skill focus", physical_focus: "Physical focus" };
const contentTypes: Record<string, { label: string; icon: ExerciseGlyphName }> = {
  strength_power: { label: "Strength & power", icon: "strength" }, conditioning: { label: "Conditioning", icon: "conditioning" },
  technical_coordination: { label: "Technical drills", icon: "skill" }, rehabilitation: { label: "Rehabilitation", icon: "rehab" },
  care_instruction: { label: "Care", icon: "recovery" }, tactical_educational: { label: "Tactical study", icon: "tactical" },
  visualisation: { label: "Visualisation", icon: "visualisation" },
};
type Filters = Partial<Record<keyof typeof filterLabels, string>>;
const detailFields = ["movement", "training_method", "skill_focus", "physical_focus"] as const;
const otherFields = ["sport", "equipment", "body_focus", "type"] as const;
const examples = { movement: "Squat, hip hinge, rotation", training_method: "Static holds, plyometric drills, lowering-focused", skill_focus: "Footwork, striking, grappling", physical_focus: "Balance, coordination, agility, visual focus" };
const boundaries: Record<Category, string> = {
  training: "For discovery. Injury, phase, equipment and programming checks still apply.",
  rehab: "Rehab pathway only. Your current stage and clinical clearance still apply.",
  learn: "For learning and care, not workouts.",
};
const readable = (value: string) => value.replaceAll("_", " ").replaceAll("-", " ");

export function DrillPreview({ entry, token, onClose, onAdd, selfDirected = false }: {
  entry: LibraryEntry; token: string; onClose: () => void;
  onAdd?: (entry: LibraryPreview) => void; selfDirected?: boolean;
}) {
  const [detail, setDetail] = useState<LibraryPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void getTrainingLibraryEntry(token, entry.catalogue_id, controller.signal).then(value => {
      if (!controller.signal.aborted) setDetail(value);
    }).catch(reason => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Unable to load drill.");
    });
    return () => controller.abort();
  }, [entry.catalogue_id, token, retry]);
  const current = detail ?? entry;
  return <InjuryDetailSheet title={current.name} onClose={onClose} closeLabel="Close drill preview">
    {/* "Loading drill…" gives way to the drill by fading in, not popping. */}
    <AutoHeight swapKey={detail ? "loaded" : error ? "error" : "loading"} swapMotion="fade">
    <div className={`injury-sheet-body ${styles.preview}`}>
      <p className={styles.meta}>{current.context} · {equipmentText(current)}</p>
      {!selfDirected ? <p className={styles.boundary}>{boundaries[current.category]}</p> : null}
      {error ? <div role="alert"><p>{error}</p><button className="secondary-button" onClick={() => { setError(null); setRetry(n => n + 1); }}>Retry preview</button></div>
        : !detail ? <p role="status">Loading drill…</p> : <>
          {detail.media ? <ExerciseDemo media={detail.media} exerciseName={detail.name} /> : null}
          {detail.metadata_projection.filter(value => value.status === "reviewed_goal").map(value => <section key={value.canonical_id}>
            <h3>Goal: {value.label}</h3>
            <p>{String(value.goal_evidence.why ?? value.source_value)}</p>
            {value.goal_evidence.cue ? <p>{String(value.goal_evidence.cue)}</p> : null}
            {value.association_note ? <p className={styles.meta}>{value.association_note}</p> : null}
          </section>)}
          {detail.category === "training" ? detail.verified_goals?.filter(goal => !detail.metadata_projection.some(value => value.status === "reviewed_goal" && value.label === goal)).map(goal => <p key={goal} className={styles.focus}>Goal: {goal}</p>) : null}
          {detail.category === "training" && detail.exercise_details?.length ? <section>
            <h3>Exercise details</h3>
            <dl className={styles.exerciseDetails}>{detailFields.map(group => {
              const values = detail.exercise_details.filter(value => value.group === group);
              return values.length ? <div key={group}><dt>{filterLabels[group]}</dt><dd>{values.map(value => value.label).join(" · ")}</dd></div> : null;
            })}</dl>
          </section> : null}
          {detail.equipment_requirements ? <section>
            <h3>Equipment setup</h3><p>{equipmentText(detail)}</p>
            <p className={styles.meta}>{detail.equipment_requirements.evidence}</p>
          </section> : null}
          {["Instructions", "Cues"].map(title => detail.guidance[title]?.length ? <section key={title}>
            <h3>{title}</h3>{detail.guidance[title].map(text => <p key={text}>{text}</p>)}
          </section> : null)}
          {Object.entries(detail.guidance).some(([title, lines]) => !["Instructions", "Cues"].includes(title) && lines.length) ?
            <details className={`ex-detail-section ${styles.coaching}`}><summary>Coaching details</summary>
              {Object.entries(detail.guidance).filter(([title]) => !["Instructions", "Cues"].includes(title)).map(([title, lines]) => <section key={title}>
                <h3>{title}</h3>{lines.map(text => <p key={text}>{text}</p>)}
              </section>)}
            </details> : null}
          {!Object.keys(detail.guidance).length ? <p className={styles.meta}>Coaching guidance unavailable.</p> : null}
        </>}
      {onAdd && detail?.category === "training" ? <button className="injury-sheet-save" onClick={() => onAdd(detail)}>Add to Session</button> : null}
    </div>
    </AutoHeight>
  </InjuryDetailSheet>;
}

export function TrainingLibrary({ accessToken, onSelect, onBack, onSelectCommunity, initialQuery = "" }: {
  accessToken: string; onSelect?: (entry: LibraryPreview) => void; onBack?: () => void; onSelectCommunity?: (entry: CommunityEntry) => void;
  /** A search the athlete already typed elsewhere carries over. */
  initialQuery?: string;
}) {
  const [query, setQuery] = useState(initialQuery);
  const [category, setCategory] = useState<Category>("training");
  const [filters, setFilters] = useState<Filters>({});
  const [selectedLabels, setSelectedLabels] = useState<Filters>({});
  const [filterOpen, setFilterOpen] = useState(false);
  const [otherOpen, setOtherOpen] = useState(false);
  const [picker, setPicker] = useState<keyof Filters | null>(null);
  const [optionQuery, setOptionQuery] = useState("");
  const pickerBack = useRef<HTMLButtonElement>(null);
  const filterTriggers = useRef<Partial<Record<keyof Filters, HTMLButtonElement | null>>>({});
  const previousPicker = useRef<keyof Filters | null>(null);
  const [selected, setSelected] = useState<LibraryEntry | null>(null);
  const [result, setResult] = useState<LibrarySearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [paging, setPaging] = useState(false);
  const [retry, setRetry] = useState(0);
  const generation = useRef(0);
  const pageRequest = useRef<AbortController | null>(null);
  const count = Object.values(filters).filter(Boolean).length;

  useEffect(() => {
    const controller = new AbortController();
    const requestGeneration = ++generation.current;
    pageRequest.current?.abort();
    const timer = setTimeout(() => {
      const params = new URLSearchParams({ q: query, category });
      if (onSelect) params.set("for_plan", "true");
      Object.entries(filters).forEach(([key, value]) => { if (value) params.set(key, value); });
      void searchTrainingLibrary(accessToken, params, controller.signal).then(value => {
        if (!controller.signal.aborted && generation.current === requestGeneration) setResult(value);
      }).catch(reason => {
        if (!controller.signal.aborted && generation.current === requestGeneration) { setResult(null); setError(reason instanceof Error ? reason.message : "Unable to search library."); }
      }).finally(() => { if (!controller.signal.aborted && generation.current === requestGeneration) setLoading(false); });
    }, query ? 200 : 0);
    return () => { clearTimeout(timer); controller.abort(); pageRequest.current?.abort(); };
  }, [accessToken, query, category, filters, retry, onSelect]);

  useEffect(() => {
    if (filterOpen && picker) pickerBack.current?.focus({ preventScroll: true });
    else if (filterOpen && previousPicker.current) filterTriggers.current[previousPicker.current]?.focus({ preventScroll: true });
    previousPicker.current = picker;
  }, [filterOpen, picker]);

  function closeFilters() { setFilterOpen(false); setPicker(null); setOptionQuery(""); setOtherOpen(false); }

  function changeSearch(update: () => void) {
    generation.current++;
    pageRequest.current?.abort();
    setLoading(true); setPaging(false); setError(null); update();
  }

  async function more() {
    if (!result || result.next_offset === null || paging || loading) return;
    const requestGeneration = generation.current;
    const controller = new AbortController();
    pageRequest.current = controller;
    setPaging(true); setError(null);
    const params = new URLSearchParams({ q: query, category, offset: String(result.next_offset), revision: result.revision });
    if (onSelect) params.set("for_plan", "true");
    Object.entries(filters).forEach(([key, value]) => { if (value) params.set(key, value); });
    try {
      const next = await searchTrainingLibrary(accessToken, params, controller.signal);
      if (generation.current === requestGeneration && !controller.signal.aborted) {
        setResult({ ...next, entries: [...result.entries, ...next.entries] });
      }
    } catch (reason) {
      if (controller.signal.aborted || generation.current !== requestGeneration) return;
      if (reason instanceof ApiError && reason.status === 409) {
        changeSearch(() => setRetry(n => n + 1));
      } else setError(reason instanceof Error ? reason.message : "Unable to load more drills.");
    } finally { if (generation.current === requestGeneration) setPaging(false); }
  }

  function selectedLabel(field: keyof Filters, value: string) {
    return result?.facets[field]?.find(choice => choice.value === value)?.label ?? selectedLabels[field] ?? (detailFields.some(key => key === field) ? "Selected" : readable(value));
  }
  function removeFilter(field: keyof Filters) {
    changeSearch(() => setFilters(previous => { const next = { ...previous }; delete next[field]; return next; }));
  }
  function selector(field: keyof Filters) {
    const choices = result?.facets[field] ?? [];
    if (category === "training") return <button key={field} className={styles.filterTrigger}
      ref={element => { filterTriggers.current[field] = element; }}
      aria-label={`${filterLabels[field]}: ${filters[field] ? selectedLabel(field, filters[field]!) : "Any"}`}
      onClick={() => { setOptionQuery(""); setPicker(field); }}>
      <span><span className={styles.filterTitle}>{filterLabels[field]}</span>
        <span className={styles.filterValue}>{filters[field] ? selectedLabel(field, filters[field]!) : field in examples ? examples[field as keyof typeof examples] : "Any"}</span>
      </span><span className={styles.chevron} aria-hidden="true" />
    </button>;
    return <label className="field" key={field}><span>{field === "body_focus" ? "Body focus" : filterLabels[field]}</span>
      <select value={filters[field] ?? ""} onChange={event => {
        const value = event.target.value;
        setSelectedLabels(previous => ({ ...previous, [field]: choices.find(choice => choice.value === value)?.label ?? "" }));
        changeSearch(() => setFilters(previous => ({ ...previous, [field]: value })));
      }}>
        <option value="">All</option>
        {filters[field] && !choices.some(choice => choice.value === filters[field]) ? <option value={filters[field]}>{selectedLabel(field, filters[field]!)} (0)</option> : null}
        {choices.map(choice => <option key={choice.value} value={choice.value}>{choice.label} ({choice.count})</option>)}
      </select>
      {field in examples && !filters[field] ? <small className={styles.meta}>{examples[field as keyof typeof examples]}</small> : null}
    </label>;
  }

  return <section className={styles.library} aria-label="Training library">
    <header className={styles.header}>
      <div className={styles.headerTop}>
        {onBack ? <button className={styles.back} onClick={onBack}>← Back to plan</button> : <Link href="/plan" className={styles.back}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m14 6-6 6 6 6" /></svg>Plan</Link>}
        <p className={styles.eyebrow}>UNLXCK / LIBRARY</p>
      </div>
      <h1>Find a Drill</h1>
    </header>
    <div className={styles.searchRow}>
      <label className={styles.search}><span className="sr-only">Search drills</span>
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10" cy="10" r="6" /><path d="m15 15 5 5" /></svg>
        <input type="search" maxLength={100} placeholder="Search exercises…" value={query}
          onChange={event => changeSearch(() => setQuery(event.target.value))} />
      </label>
      <button className={`secondary-button ${styles.filterButton}`} onClick={() => setFilterOpen(true)} aria-haspopup="dialog">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 17h16M8 4v6M16 14v6" /></svg>
        Filters{count ? ` (${count})` : ""}
      </button>
    </div>
    {!onSelect ? <div className={styles.categories} role="group" aria-label="Library category">
      {(["training", "rehab", "learn"] as const).map(value => <button key={value} aria-pressed={category === value}
        onClick={() => { if (value !== category) changeSearch(() => { setCategory(value); setFilters({}); }); }}>
        {value === "training" ? "Training" : value === "rehab" ? "Rehab" : "Learn"}
      </button>)}
    </div> : null}
    {category !== "training" ? <p className={styles.boundary}>{boundaries[category]}</p> : null}
    <div className={styles.resultHeading}>
      <p role="status" aria-live="polite">{loading ? "Searching…" : result ? `${result.total.toLocaleString()} ${category === "learn" ? result.total === 1 ? "entry" : "entries" : result.total === 1 ? "drill" : "drills"}` : "Library unavailable"}</p>
      {count ? <button className={styles.reset} onClick={() => changeSearch(() => setFilters({}))}>Reset filters</button> : null}
    </div>
    {count ? <div className={styles.activeFilters} aria-label="Active filters">
      {Object.entries(filters).filter(([, value]) => value).map(([key, value]) => {
        const field = key as keyof Filters;
        return <button key={field} aria-label={`Remove ${filterLabels[field]} filter`} onClick={() => removeFilter(field)}>
          {field === "training_method" ? "Method" : filterLabels[field]}: {selectedLabel(field, value!)} <span aria-hidden="true">×</span>
        </button>;
      })}
    </div> : null}
    {error ? <div className={styles.empty} role="alert"><p>{error}</p>
      <button className="secondary-button" onClick={() => changeSearch(() => setRetry(n => n + 1))}>Retry search</button>
    </div> : null}
    {!loading && result ? <>
      {result.entries.length ? <ul className={styles.results} aria-label="Drill results">
        {result.entries.map(entry => <li key={entry.catalogue_id}>
          <button className={styles.drill} onClick={() => setSelected(entry)} aria-haspopup="dialog">
            <span className={styles.thumbnail} aria-hidden="true">
              <ExerciseGlyphIcon name={contentTypes[entry.content_type]?.icon ?? "skill"} />
              {entry.media ? <>
                {/* eslint-disable-next-line @next/next/no-img-element -- same curated YouTube facade as Today */}
                <img src={demoThumbnailUrl(entry.media.video_id, "mq")} alt="" loading="lazy"
                  onError={event => { event.currentTarget.style.display = "none"; }} />
                <span className={styles.play} />
              </> : null}
            </span>
            <span className={styles.drillCopy}><span className={styles.name}>{entry.name}</span>
              <span className={styles.focus}>{entry.category === "training" ? entry.verified_goals?.length ? `Goal: ${entry.verified_goals.slice(0, 2).join(" · ")}` : entry.exercise_details?.slice(0, 2).map(value => value.label).join(" · ") || contentTypes[entry.content_type]?.label : entry.focus.slice(0, 2).join(" · ") || contentTypes[entry.content_type]?.label}</span>
              {entry.discovery_match ? <span className={styles.meta}>{entry.discovery_match}</span> : null}
              <span className={styles.meta}>{equipmentText(entry)} · {entry.context}</span>
              {entry.media ? <span className="sr-only">Has demo video.</span> : null}
            </span><span className={styles.chevron} aria-hidden="true" />
          </button>
        </li>)}
      </ul> : <div className={styles.empty}><h2>{category === "training" ? count ? query.trim() ? "No exercises match this search and these filters." : "No exercises match these filters." : "No exercises match your search." : "No drills found"}</h2>
        {category !== "training" ? <><p>Try a shorter name or reset your filters.</p><button className="secondary-button" onClick={() => changeSearch(() => { setQuery(""); setFilters({}); })}>Reset search</button></> : count ? <div className={styles.emptyActions}>
          <button className="secondary-button" onClick={() => changeSearch(() => setFilters({}))}>Clear all filters</button>
        </div> : <button className="secondary-button" onClick={() => changeSearch(() => setQuery(""))}>Clear search</button>}
      </div>}
      {result.next_offset !== null ? <button className={`secondary-button ${styles.more}`} disabled={paging} onClick={() => void more()}>{paging ? "Loading…" : "Load more"}</button> : null}
    </> : null}
    {category === "training" && !count ? <CommunityDiscovery key={accessToken} token={accessToken} query={query} onAdd={onSelectCommunity} /> : null}
    {filterOpen ? <InjuryDetailSheet title={picker ? filterLabels[picker] : "Filters"} onClose={closeFilters} closeLabel="Close filters">
      {/* Opening a filter slides its options in from the right; Back slides
          the filter list in from the left, and the sheet glides between sizes. */}
      <AutoHeight swapKey={picker ?? "filters"} swapMotion={picker ? "forward" : "back"}>
      <div className={`injury-sheet-body ${styles.filters}`}>
        {picker ? <>
          <button ref={pickerBack} className={styles.pickerBack} onClick={() => setPicker(null)}>← Back to filters</button>
          <label className={styles.optionSearch}><span className="sr-only">Search {filterLabels[picker].toLowerCase()} options</span>
            <input type="search" value={optionQuery} placeholder="Search options…"
              onChange={event => setOptionQuery(event.target.value)} />
          </label>
          <div className={styles.optionList} role="group" aria-label={`${filterLabels[picker]} options`}>
            <button data-value="" aria-pressed={!filters[picker]} onClick={() => { removeFilter(picker); setPicker(null); }}>
              <span>Any</span><span aria-hidden="true">{!filters[picker] ? "✓" : ""}</span>
            </button>
            {filters[picker] && !(result?.facets[picker] ?? []).some(choice => choice.value === filters[picker]) ?
              <button aria-pressed="true" onClick={() => setPicker(null)}><span>{selectedLabel(picker, filters[picker]!)}</span><span>0 <span aria-hidden="true">✓</span></span></button> : null}
            {(result?.facets[picker] ?? []).filter(choice => choice.label.toLocaleLowerCase().includes(optionQuery.trim().toLocaleLowerCase())).map(choice =>
              <button key={choice.value} data-value={choice.value} aria-label={`${choice.label}, ${choice.count} drills`} aria-pressed={filters[picker] === choice.value} onClick={() => {
                setSelectedLabels(previous => ({ ...previous, [picker]: choice.label }));
                changeSearch(() => setFilters(previous => ({ ...previous, [picker]: choice.value })));
                setPicker(null);
              }}><span>{choice.label}</span><span className={styles.optionCount}>{choice.count} <span aria-hidden="true">{filters[picker] === choice.value ? "✓" : ""}</span></span></button>)}
          </div>
          {!(result?.facets[picker] ?? []).some(choice => choice.label.toLocaleLowerCase().includes(optionQuery.trim().toLocaleLowerCase())) ?
            <p role="status" className={styles.meta}>No matching options.</p> : null}
        </> : <>
        {category === "training" ? <>
          <h3>What do you want to improve?</h3>
          {selector("goal")}
          <p className={styles.meta}>Pick a goal, or explore below.</p>
          <h3>Explore exercises</h3>
          <div className={styles.explore}>{detailFields.map(selector)}</div>
          <details className={styles.otherFilters} open={otherOpen} onToggle={event => setOtherOpen(event.currentTarget.open)}><summary>Other filters</summary>
            <p className={styles.meta}>Sport, equipment, body area and training type</p>
            <div className={styles.filters}>{otherFields.map(selector)}</div>
          </details>
        </> : (["type", "goal", "sport", "equipment", "body_focus"] as const).map(selector)}
        <div className={styles.filterActions}>
          <button className="secondary-button" onClick={() => changeSearch(() => setFilters({}))}>Clear filters</button>
          <button className="injury-sheet-save" onClick={closeFilters}>Show results</button>
        </div>
        </>}
      </div>
      </AutoHeight>
    </InjuryDetailSheet> : null}
    {selected ? <DrillPreview key={selected.catalogue_id} entry={selected} token={accessToken} onClose={() => setSelected(null)} onAdd={onSelect} selfDirected={Boolean(onSelect)} /> : null}
  </section>;
}
