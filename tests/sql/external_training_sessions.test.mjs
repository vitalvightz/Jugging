// Run with PGLITE_ROOT pointing at an isolated npm install of @electric-sql/pglite.
// No production database or credentials are used.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { resolve } from "node:path";

const require = createRequire(resolve(process.env.PGLITE_ROOT ?? "web", "package.json"));
const { PGlite } = require("@electric-sql/pglite");
const db = new PGlite();
const owner = "00000000-0000-4000-8000-000000000001";
const other = "00000000-0000-4000-8000-000000000002";
const request = "00000000-0000-4000-8000-000000000010";
const migration = await readFile(new URL("../../supabase/migrations/20261010184612_add_external_training_sessions.sql", import.meta.url), "utf8");
await db.exec(`
  create role anon; create role authenticated; create role service_role bypassrls;
  create schema auth;
  create function auth.uid() returns uuid language sql as $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
  grant usage on schema auth to authenticated; grant execute on function auth.uid() to authenticated;
  create table public.profiles (id uuid primary key);
  create table public.plans (id uuid primary key);
  create function public.set_updated_at() returns trigger language plpgsql as $$ begin new.updated_at = now(); return new; end $$;
  insert into public.profiles values ('${owner}'), ('${other}');
  insert into public.plans values ('${request}');
`);
await db.exec(migration);
await db.exec(migration); // Safe migration reapplication.
const insert = `insert into public.external_training_sessions
  (athlete_id, client_request_id, request_fingerprint, training_day, activity, duration_minutes, effort)
  values ($1,$2,$3,'2026-10-10',$4,$5,$6) on conflict (athlete_id,client_request_id) do nothing returning *`;
const fields = [owner, request, "a".repeat(64), "sparring", 60, "hard"];
await db.exec("set role service_role");
const [first, retry] = await Promise.all([db.query(insert, fields), db.query(insert, fields)]);
assert.equal(first.rows.length + retry.rows.length, 1);
let result = await db.query("select * from public.external_training_sessions");
assert.equal(result.rows.length, 1);
const id = result.rows[0].id;
assert.equal(result.rows[0].duration_minutes, 60);
assert.equal(result.rows[0].effort, "hard");
assert.equal("session_rpe" in result.rows[0], false);
// Request IDs are scoped to owners, not globally shared.
await db.query(insert, [other, ...fields.slice(1)]);
for (const changes of [{ 4: 0 }, { 4: 601 }, { 5: "rpe8" }, { 3: "rehab" }, { 2: "bad" }]) {
  const invalid = [...fields];
  invalid[1] = crypto.randomUUID();
  for (const [key, value] of Object.entries(changes)) invalid[Number(key)] = value;
  await assert.rejects(db.query(insert, invalid), /check constraint/);
}
await db.exec(`reset role; set role authenticated; set request.jwt.claim.sub = '${owner}'`);
result = await db.query("select * from public.external_training_sessions");
assert.equal(result.rows.length, 1);
assert.equal(result.rows[0].athlete_id, owner);
await assert.rejects(db.query("update public.external_training_sessions set duration_minutes = 90"), /permission denied/);
await assert.rejects(db.query("delete from public.external_training_sessions"), /permission denied/);
await assert.rejects(db.query(insert, [owner, crypto.randomUUID(), ...fields.slice(2)]), /permission denied/);
await db.exec("reset role; set role anon");
await assert.rejects(db.query("select * from public.external_training_sessions"), /permission denied/);
await db.exec("reset role; delete from public.plans; set role service_role");
assert.equal((await db.query("select * from public.external_training_sessions")).rows.length, 2);
await db.query("update public.external_training_sessions set duration_minutes = 45 where id = $1 and athlete_id = $2", [id, owner]);
await db.query(insert, fields);
assert.equal((await db.query("select duration_minutes from public.external_training_sessions where id = $1", [id])).rows[0].duration_minutes, 45);
await db.query("update public.external_training_sessions set deleted_at = now() where id = $1", [id]);
await db.query(insert, fields); // Deleted occurrence cannot be recreated by retry.
await db.exec(`reset role; set role authenticated; set request.jwt.claim.sub = '${owner}'`);
assert.equal((await db.query("select * from public.external_training_sessions")).rows.length, 0);
await db.exec(`set request.jwt.claim.sub = '${other}'`);
assert.equal((await db.query("select * from public.external_training_sessions")).rows.length, 1);
// Optional sport and session type: rows that existed stay unclassified.
await db.exec("reset role");
const before = (await db.query("select id, activity from public.external_training_sessions order by id")).rows;
const discipline = await readFile(new URL("../../supabase/migrations/20261019121808_add_external_training_discipline.sql", import.meta.url), "utf8");
await db.exec(discipline);
await db.exec(discipline); // Safe migration reapplication.
const after = (await db.query("select id, activity, sport, session_type from public.external_training_sessions order by id")).rows;
assert.deepEqual(after.map(({ id, activity }) => ({ id, activity })), before);
assert.ok(after.every(row => row.sport === null && row.session_type === null));
const classified = `insert into public.external_training_sessions
  (athlete_id, client_request_id, request_fingerprint, training_day, activity, sport, session_type, duration_minutes, effort)
  values ($1, gen_random_uuid(), $2, '2026-10-10', $3, $4, $5, 60, 'hard') returning sport, session_type`;
for (const [activity, sport, sessionType] of [["boxing", "boxing", "pads"], ["other", "bjj", "rolling"], ["other", "muay_thai", "clinch"],
  ["sparring", "mma", "sparring"], ["other", "wrestling", null], ["strength", null, null], ["roadwork", null, null]]) {
  const row = (await db.query(classified, [owner, "b".repeat(64), activity, sport, sessionType])).rows[0];
  assert.deepEqual([row.sport, row.session_type], [sport, sessionType]);
}
// BJJ rolling is never head-contact sparring; pairs and activities must agree.
for (const [activity, sport, sessionType] of [["sparring", "bjj", "rolling"], ["boxing", "muay_thai", "pads"], ["boxing", "boxing", "clinch"],
  ["other", null, "pads"], ["other", "judo", null], ["sparring", "mma", null], ["strength", "strength_conditioning", "power"]]) {
  await assert.rejects(db.query(classified, [owner, "b".repeat(64), activity, sport, sessionType]), /check constraint/);
}
await db.close();
console.log("PASS: migration replay, atomic retries, constraints, owner RLS, forbidden client writes, plan independence, edit/delete retry safety, optional discipline");
