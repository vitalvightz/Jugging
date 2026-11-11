"use client";
import { useEffect, useRef, useState } from 'react';
import { getCommunityExercise, saveCommunityDraft, submitCommunityExercise, withdrawCommunityExercise, reportCommunityExercise, listCommunityExercises } from '@/lib/api';
import type { CommunityEntry, CommunitySearch, ManualExercise } from '@/lib/api-schema.generated';
import { InjuryDetailSheet } from './today/injury-detail-sheet';
import { PrivateExerciseVideo, PrivateExerciseVideoSheet, PrivateVideoPlayer } from './private-exercise-video';
import { blankDose } from '@/lib/custom-plan-dose';
import { ExerciseGlyphIcon } from './exercise-glyph';
import { EQUIPMENT_ACCESS_GROUPS, EQUIPMENT_ACCESS_OPTIONS } from '@/lib/intake-options';
import library from './training-library.module.css';
import fields from './custom-plan-builder.module.css';
import layout from './community-exercises.module.css';
export const communityStatus = (status: CommunityEntry['status']) => ({ private: 'Private', in_review: 'In review', changes_requested: 'Changes requested', rejected: 'Rejected', published: 'Published', superseded: 'Updated' })[status];
export const communityError = (reason: unknown) => reason instanceof Error ? reason.message : 'Unable to complete this action. Try again.';
export function Contributor({ entry }: { entry: CommunityEntry }) {
  return <span className={layout.attribution}>
    {/* eslint-disable-next-line @next/next/no-img-element -- live consenting profile */}
    {entry.contributor?.avatar_url ? <img src={entry.contributor.avatar_url} alt="" loading="lazy" /> : null}
    <span>Community{entry.contributor ? ` · Added by @${entry.contributor.username}` : ''}</span>
  </span>;
}
const EQUIPMENT_LIMIT = 120;
const equipmentItems = (equipment: string | null | undefined) => (equipment ?? '').split(',').map(item => item.trim()).filter(Boolean);
const equipmentOption = (item: string) => EQUIPMENT_ACCESS_OPTIONS.find(option => option.label.toLowerCase() === item.toLowerCase());
/** Public exercises pick from the same equipment list athletes declare, so the
 * library vocabulary stays consistent. Free-text items the athlete typed stay. */
function CommunityEquipmentPicker({ equipment, onChange }: { equipment: string | null | undefined; onChange: (equipment: string | null) => void }) {
  const [full, setFull] = useState(false);
  const items = equipmentItems(equipment);
  const selected = items.map(item => equipmentOption(item)?.value).filter((value): value is string => Boolean(value));
  function toggle(value: string) {
    const option = EQUIPMENT_ACCESS_OPTIONS.find(candidate => candidate.value === value)!;
    const next = selected.includes(value) ? items.filter(item => equipmentOption(item)?.value !== value) : [...items, option.label];
    const text = next.join(', ');
    if (text.length > EQUIPMENT_LIMIT) { setFull(true); return; }
    setFull(false); onChange(text || null);
  }
  return <details className={layout.equipment}><summary>Equipment · {items.length ? items.join(', ') : 'None selected'}</summary>
    <div className="equipment-groups" role="group" aria-label="Exercise equipment">
      {EQUIPMENT_ACCESS_GROUPS.map(group => <fieldset className="equipment-group" key={group.label}><legend>{group.label}</legend>
        <div className="equipment-grid">{group.options.map(option => {
          const checked = selected.includes(option.value);
          return <label key={option.value} className={`equipment-tile${checked ? ' equipment-tile-checked' : ''}`}>
            <input type="checkbox" checked={checked} onChange={() => toggle(option.value)} /><span>{option.label}</span>
          </label>;
        })}</div>
      </fieldset>)}
    </div>
    {full ? <p role="status" className={layout.notice}>Equipment list full. Remove one to add another.</p> : null}
  </details>;
}
/** The private exercise and immutable submitted revision must never be conflated. */
export function communityDetailsChanged(entry: CommunityEntry, exercise: ManualExercise): boolean {
  return entry.name !== exercise.name ||
    (entry.equipment ?? '') !== (exercise.equipment ?? '') ||
    (entry.notes ?? '') !== (exercise.notes ?? '') ||
    (entry.media_id ?? null) !== (exercise.media_id ?? null);
}
export function CommunityContributionSheet({ token, exercise, onClose, onIdentity, onEquipment, standalone = false }: {
  token: string; exercise: ManualExercise; onClose: () => void; onIdentity: (id: string) => void; onEquipment?: (equipment: string | null) => void; standalone?: boolean;
}) {
  const [workingExercise, setWorkingExercise] = useState(exercise);
  const [editVideoOpen, setEditVideoOpen] = useState(false);
  const candidate = standalone ? workingExercise : exercise;
  const [entry, setEntry] = useState<CommunityEntry>(), [loading, setLoading] = useState(Boolean(exercise.community_id));
  const [busy, setBusy] = useState(false), [error, setError] = useState<string>(), [rights, setRights] = useState(false), [consent, setConsent] = useState(false), [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!exercise.community_id || entry?.id === exercise.community_id) return;
    const controller = new AbortController();
    void getCommunityExercise(token, exercise.community_id, 'owned', controller.signal).then(value => {
      if (!controller.signal.aborted) { setEntry(value); setError(undefined); }
    }).catch(reason => { if (!controller.signal.aborted) setError(communityError(reason)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [token, exercise.community_id, entry?.id, retry]);
  const changed = entry ? communityDetailsChanged(entry, candidate) : false;
  const videoChanged = Boolean(entry && (entry.media_id ?? null) !== (candidate.media_id ?? null));
  const canSubmit = !entry || changed || ['private', 'changes_requested', 'rejected'].includes(entry.status);
  async function save(submit: boolean) {
    setBusy(true); setError(undefined);
    try {
      let current = entry;
      if (!current || changed) {
        current = await saveCommunityDraft(token, { exercise_id: current?.id ?? null, expected_revision_id: current?.revision_id ?? null, name: candidate.name, equipment: candidate.equipment ?? null, notes: candidate.notes ?? null, media_id: candidate.media_id ?? null });
        setEntry(current); onIdentity(current.id);
      }
      if (submit) current = await submitCommunityExercise(token, current.revision_id);
      setEntry(current); setRights(false); setConsent(false);
    } catch (reason) { setError(communityError(reason)); } finally { setBusy(false); }
  }
  return <><InjuryDetailSheet title="Community exercise" onClose={onClose} busy={busy}><div className={`${fields.sheet} ${layout.stack}`}>
    <div><strong>{candidate.name}</strong><p className="muted">{entry ? communityStatus(entry.status) : 'Private'}</p></div>
    {entry?.reason ? <p>{entry.reason}</p> : null}
    {error ? <div role="alert"><p>{error}</p>{exercise.community_id && !entry ? <button type="button" className="secondary-button" onClick={() => { setLoading(true); setRetry(n => n + 1); }}>Retry</button> : null}</div> : null}
    {loading ? <p role="status">Opening submission…</p> : <>
      {standalone ? <details><summary>Edit exercise or video</summary><div className={layout.stack}>
        {(['name', 'equipment', 'notes'] as const).map(field => <label key={field}><span>{field === 'name' ? 'Exercise name' : field === 'notes' ? 'Instructions' : 'Equipment'}</span>
          <input value={candidate[field] ?? ''} required={field === 'name'} maxLength={field === 'name' ? 100 : field === 'notes' ? 2000 : 120}
            onChange={event => setWorkingExercise(current => ({ ...current, [field]: event.target.value }))} /></label>)}
        <button type="button" className="ghost-button" onClick={() => setEditVideoOpen(true)}>{candidate.media_id ? 'Replace video' : 'Add video'}</button>
        {candidate.media_id ? <button type="button" className="ghost-button" onClick={() => setWorkingExercise(current => ({ ...current, media_id: null }))}>Remove video</button> : null}
      </div></details> : null}
      {videoChanged ? <p role="status" className={layout.notice}>Video not yet submitted. Update this revision to send it for review.</p> : null}
      {changed && !videoChanged ? <p role="status" className={layout.notice}>Details changed since submission. Update it for another review.</p> : null}
      {entry?.media_id ? entry.media ? <><p className="muted">Video currently submitted for review</p><PrivateVideoPlayer key={entry.revision_id} media={entry.media} name={entry.name} /></> :
        <p role="alert">Submitted video can’t be previewed. Don’t publish until it can.</p> :
        <p className="muted">No video attached to this community submission.</p>}
      {videoChanged && candidate.media_id ? <><p className="muted">New video in your private exercise</p><PrivateExerciseVideo token={token} id={candidate.media_id} name={candidate.name} /></> : null}
      {canSubmit ? <>
        <CommunityEquipmentPicker equipment={candidate.equipment} onChange={equipment => standalone
          ? setWorkingExercise(current => ({ ...current, equipment })) : onEquipment?.(equipment)} />
        <p className="muted">{changed ? 'A new revision will require fresh admin approval. Your previous publication stays unchanged until then.' : 'Approval makes this exercise public.'}</p>
        <label className={layout.check}><input type="checkbox" checked={rights} disabled={busy} onChange={e => setRights(e.target.checked)} /><span>I have permission to share this exercise and video.</span></label>
        <label className={layout.check}><input type="checkbox" checked={consent} disabled={busy} onChange={e => setConsent(e.target.checked)} /><span>Show my username and profile picture publicly.</span></label>
        <button type="button" className="cta" disabled={busy || !rights || !consent} onClick={() => void save(true)}>{busy ? 'Submitting…' : changed ? 'Update community submission' : 'Submit for review'}</button>
      </> : <p className="muted">{entry?.status === 'published' ? 'This exact revision is public.' : 'This revision is awaiting review.'}</p>}
      {entry && (entry.has_publication || entry.status !== 'private') ? <details><summary>Submission options</summary><button type="button" className="ghost-button" disabled={busy} onClick={async () => {
        setBusy(true); setError(undefined); try { setEntry(await withdrawCommunityExercise(token, entry.revision_id)); } catch (reason) { setError(communityError(reason)); } finally { setBusy(false); }
      }}>Withdraw submission and public credit</button></details> : null}
    </>}
  </div></InjuryDetailSheet>
    {standalone && editVideoOpen ? <PrivateExerciseVideoSheet token={token} name={candidate.name || 'My exercise'}
      onClose={() => setEditVideoOpen(false)} onAttach={id => { setWorkingExercise(current => ({ ...current, media_id: id })); setEditVideoOpen(false); }} /> : null}
  </>;
}
export function CommunityExercisePreview({ token, revision, onClose, onAdd }: { token: string; revision: string; onClose: () => void; onAdd?: (entry: CommunityEntry) => void }) {
  const [entry, setEntry] = useState<CommunityEntry>(), [error, setError] = useState<string>(), [retry, setRetry] = useState(0);
  const [reason, setReason] = useState(''), [busy, setBusy] = useState(false), [reported, setReported] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void getCommunityExercise(token, revision, 'revisions', controller.signal).then(value => { if (!controller.signal.aborted) setEntry(value); }).catch(reason => { if (!controller.signal.aborted) setError(communityError(reason)); });
    return () => controller.abort();
  }, [token, revision, retry]);
  return <InjuryDetailSheet title={entry?.name ?? 'Community exercise'} onClose={onClose} busy={busy}><div className={`${fields.sheet} ${layout.stack}`}>
    {error ? <div role="alert"><p>{error}</p><button className="secondary-button" onClick={() => { setError(undefined); setRetry(n => n + 1); }}>Retry</button></div> : !entry ? <p role="status">Opening exercise…</p> : <>
      <Contributor entry={entry} /><p className="muted">Self-directed use</p>
      {entry.equipment ? <p>{entry.equipment}</p> : null}{entry.notes ? <p>{entry.notes}</p> : null}
      {entry.media ? <PrivateVideoPlayer key={entry.revision_id} media={entry.media} name={entry.name} visibility="community" /> : null}
      {onAdd && entry.published ? <button className="cta" onClick={() => onAdd(entry)}>Add to Session</button> : null}
      {entry.published ? <details><summary>Report exercise</summary>{reported ? <p role="status">Report sent for review.</p> : <form className={layout.stack} onSubmit={async e => {
        e.preventDefault(); setBusy(true); try { await reportCommunityExercise(token, entry.revision_id, reason); setReported(true); } catch (reason) { setError(communityError(reason)); } finally { setBusy(false); }
      }}><label><span>Reason</span><textarea required maxLength={1000} value={reason} onChange={e => setReason(e.target.value)} /></label><button className="ghost-button" disabled={busy}>Send report</button></form>}</details> : null}
    </>}
  </div></InjuryDetailSheet>;
}
export function CommunityDiscovery({ token, query, onAdd }: { token: string; query: string; onAdd?: (entry: CommunityEntry) => void }) {
  const paging = useRef<AbortController | null>(null);
  const [open, setOpen] = useState(false), [result, setResult] = useState<CommunitySearch>(), [selected, setSelected] = useState<string>();
  const [error, setError] = useState<string>(), [loading, setLoading] = useState(false), [retry, setRetry] = useState(0);
  useEffect(() => {
    paging.current?.abort();
    if (!open) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      setLoading(true); setError(undefined);
      void listCommunityExercises(token, 'public', query, 0, controller.signal).then(value => { if (!controller.signal.aborted) setResult(value); }).catch(reason => { if (!controller.signal.aborted) { setResult(undefined); setError(communityError(reason)); } }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    }, query ? 200 : 0);
    return () => { clearTimeout(timer); controller.abort(); paging.current?.abort(); };
  }, [open, token, query, retry]);
  return <details className={layout.discovery} onToggle={e => setOpen(e.currentTarget.open)}><summary>Community exercises</summary>{open ? <>
    {loading ? <p role="status">Searching community exercises…</p> : null}
    {error ? <div role="alert"><p>{error}</p><button className="secondary-button" onClick={() => setRetry(n => n + 1)}>Retry community search</button></div> : null}
    <ul className={library.results} aria-label="Community drill results">{!loading && result?.entries.map(entry => <li key={entry.revision_id}><button className={library.drill} onClick={() => setSelected(entry.revision_id)} aria-haspopup="dialog">
      <span className={library.thumbnail} aria-hidden="true"><ExerciseGlyphIcon name="skill" />
        {/* eslint-disable-next-line @next/next/no-img-element -- existing YouTube facade thumbnail */}
        {entry.thumbnail ? <img src={entry.thumbnail} alt="" loading="lazy" /> : null}
      </span><span className={library.drillCopy}><span className={library.name}>{entry.name}</span><Contributor entry={entry} /></span><span className={library.chevron} aria-hidden="true" />
    </button></li>)}</ul>
    {!loading && result?.total === 0 ? <p className="muted">No community exercises match this search.</p> : null}
    {result?.next_offset != null ? <button className="secondary-button" disabled={loading} onClick={async () => {
      const controller = new AbortController(); paging.current = controller;
      setLoading(true); try { const next = await listCommunityExercises(token, 'public', query, result.next_offset!, controller.signal); if (!controller.signal.aborted) setResult({ ...next, entries: [...result.entries, ...next.entries] }); } catch (reason) { if (!controller.signal.aborted) setError(communityError(reason)); } finally { if (!controller.signal.aborted) setLoading(false); }
    }}>Load more community exercises</button> : null}
    {selected ? <CommunityExercisePreview key={selected} token={token} revision={selected} onClose={() => setSelected(undefined)} onAdd={onAdd} /> : null}
  </> : null}</details>;
}

/** Recover contributions even when their original custom-plan draft was discarded. */
export function MyCommunitySubmissions({ token }: { token: string }) {
  const [open, setOpen] = useState(false), [entries, setEntries] = useState<CommunityEntry[]>([]);
  const [offset, setOffset] = useState(0), [nextOffset, setNextOffset] = useState<number | null>(null);
  const [selected, setSelected] = useState<CommunityEntry>(), [error, setError] = useState<string>();
  const [loading, setLoading] = useState(false), [refresh, setRefresh] = useState(0);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setLoading(true);
    void listCommunityExercises(token, 'mine', '', offset, controller.signal).then(result => {
      if (!controller.signal.aborted) {
        setEntries(previous => offset ? [...previous, ...result.entries] : result.entries);
        setNextOffset(result.next_offset); setError(undefined);
      }
    }).catch(reason => { if (!controller.signal.aborted) setError(communityError(reason)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [token, open, offset, refresh]);
  function reload() { setOffset(0); setRefresh(value => value + 1); }
  const exercise = selected ? {
    id: selected.id, origin: 'manual' as const, exercise_type: null, name: selected.name,
    equipment: selected.equipment, notes: selected.notes, media_id: selected.media_id,
    community_id: selected.id, community_revision_id: null, dose: blankDose(), incomplete: true, coaching_cues: null, stop_rules: null,
  } : null;
  return <div className={layout.submissions}>
    <button type="button" className="ghost-button" aria-expanded={open} onClick={() => { setOpen(value => !value); if (!open) reload(); }}>My submissions</button>
    {open ? <div className={layout.stack}>
      {error ? <p role="alert">{error} <button type="button" className="secondary-button" onClick={reload}>Retry</button></p> : null}
      {loading && !entries.length ? <p role="status">Loading submissions…</p> : null}
      {!loading && !entries.length && !error ? <p className="muted">No community submissions yet.</p> : null}
      {entries.map(entry => <div className={layout.submissionRow} key={entry.id}>
        <span><strong>{entry.name}</strong><span className="muted">{communityStatus(entry.status)}{entry.reason ? ` · ${entry.reason}` : ''}</span></span>
        <button type="button" className="ghost-button" onClick={() => setSelected(entry)}>Manage</button>
      </div>)}
      {nextOffset != null ? <button type="button" className="secondary-button" disabled={loading} onClick={() => setOffset(nextOffset)}>Load more</button> : null}
    </div> : null}
    {exercise && selected ? <CommunityContributionSheet key={selected.revision_id} token={token} exercise={exercise} standalone
      onIdentity={() => undefined} onClose={() => { setSelected(undefined); reload(); }} /> : null}
  </div>;
}
