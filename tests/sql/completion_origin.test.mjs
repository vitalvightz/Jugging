// Isolated Postgres (PGlite). No app dependency or production credentials.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
const require = createRequire(resolve(process.env.PGLITE_ROOT, 'package.json'));
const { PGlite } = require('@electric-sql/pglite');
const db = new PGlite();
const owner = '00000000-0000-4000-8000-000000000001';
const other = '00000000-0000-4000-8000-000000000002';
const plan = '00000000-0000-4000-8000-000000000010';
await db.exec(`create role anon; create role authenticated; create role service_role bypassrls;
  create schema auth; create function auth.uid() returns uuid language sql as
    $$ select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
  grant usage on schema auth to authenticated; grant execute on function auth.uid() to authenticated;
  create function public.is_admin() returns boolean language sql as $$ select false $$;
  create table profiles(id uuid primary key); create table plans(id uuid primary key);
  create function public.set_updated_at() returns trigger language plpgsql as
    $$ begin new.updated_at = now(); return new; end $$;
  insert into profiles values ('${owner}'),('${other}'); insert into plans values ('${plan}');`);
await db.exec(await readFile(new URL('../../supabase/migrations/20260618120000_add_today_checkins_and_completions.sql', import.meta.url), 'utf8'));
await db.exec(`alter table session_completions add column prescription_snapshot jsonb,
  add column rehab_performance text, add column rehab_response_contexts jsonb;
  insert into session_completions(athlete_id,plan_id,session_id,training_day,status,session_rpe,notes)
    values ('${owner}','${plan}','legacy','2026-10-18','done',9,'Original');`);
const migration = await readFile(new URL('../../supabase/migrations/20261018155432_add_completion_origin.sql', import.meta.url), 'utf8');
await db.exec(migration); await db.exec(migration);
const legacy = (await db.query("select * from session_completions where session_id='legacy'")).rows[0];
assert.equal(legacy.completion_origin, null);
assert.equal(legacy.notes, 'Original');
assert.equal(legacy.session_rpe, 9);
await db.exec('set role service_role');
const insertCredit = `insert into session_completions(athlete_id,plan_id,session_id,training_day,status,completion_origin)
  values ($1,$2,$3,'2026-10-18','done','day_credit')
  on conflict(athlete_id,session_id,training_day) do nothing`;
const updateCredit = `update session_completions set status='skipped',session_rpe=null,pain_after=null,notes=''
  where athlete_id=$1 and plan_id=$2 and session_id=$3 and training_day='2026-10-18' and completion_origin='day_credit'
  returning *`;
const report = `insert into session_completions(athlete_id,plan_id,session_id,training_day,status,completion_origin,session_rpe,pain_after,notes)
  values ($1,$2,$3,'2026-10-18','modified','reported',8,3,'Actual report')
  on conflict(athlete_id,session_id,training_day) do update set status=excluded.status,
  completion_origin=excluded.completion_origin,session_rpe=excluded.session_rpe,pain_after=excluded.pain_after,notes=excluded.notes returning *`;
const fields = [owner,plan,'second'];
await db.query(insertCredit,fields);
const creditId = (await db.query("select id from session_completions where session_id='second'")).rows[0].id;
await db.query(report,fields); // Direct report wins between credit insert and conditional update.
assert.equal((await db.query(updateCredit,fields)).rows.length,0);
await db.query(insertCredit,fields); // Late retry cannot downgrade actuals either.
assert.equal((await db.query(updateCredit,fields)).rows.length,0);
const actual = (await db.query("select * from session_completions where session_id='second'")).rows[0];
assert.equal(actual.id,creditId);
assert.equal(actual.status,'modified'); assert.equal(actual.session_rpe,8);
assert.equal(actual.pain_after,3); assert.equal(actual.notes,'Actual report');
assert.equal(actual.completion_origin,'reported');
assert.equal((await db.query(updateCredit,[owner,plan,'legacy'])).rows.length,0);
await db.query(insertCredit,[owner,plan,'support']);
assert.equal((await db.query(updateCredit,[owner,plan,'support'])).rows.length,1);
for (const mutation of ["session_rpe=8", "pain_after=2", "notes='Copied'", "prescription_snapshot='{}'", "rehab_performance='changed'", "rehab_response_contexts='[]'", "completion_origin='guessed'"]) {
  await assert.rejects(db.query(`update session_completions set ${mutation} where session_id='support'`),/check constraint/);
}
await db.exec(`reset role; set role authenticated; set request.jwt.claim.sub='${other}'`);
assert.equal((await db.query('select * from session_completions')).rows.length,0);
await db.exec(`set request.jwt.claim.sub='${owner}'`);
assert.equal((await db.query('select * from session_completions')).rows.length,3);
await assert.rejects(db.query("update session_completions set completion_origin='reported'"),/permission denied/);
await assert.rejects(db.query(insertCredit,[owner,plan,'forged']),/permission denied/);
await db.close();
console.log('PASS: replay, NULL legacy, unique occurrence, conditional credit/report race, actual/clinical isolation, inherited RLS and write permissions');
