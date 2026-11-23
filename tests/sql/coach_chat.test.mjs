// Coach chat and check-in nudge migration: permissions, idempotency, limits.
// Run with PGLITE_ROOT pointing at an isolated npm install of @electric-sql/pglite.
// No production database or credentials are used.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { resolve } from "node:path";

const require = createRequire(resolve(process.env.PGLITE_ROOT ?? "web", "package.json"));
const { PGlite } = require("@electric-sql/pglite");
const db = new PGlite();

const coach = "00000000-0000-4000-8000-000000000001";
const fighter = "00000000-0000-4000-8000-000000000002";
const minor = "00000000-0000-4000-8000-000000000003";
const stranger = "00000000-0000-4000-8000-000000000004";
const otherFighter = "00000000-0000-4000-8000-000000000005";
const rehab = "00000000-0000-4000-8000-000000000010";
const otherRecord = "00000000-0000-4000-8000-000000000011";
const coachPhoto = "00000000-0000-4000-8000-000000000020";
const strangerPhoto = "00000000-0000-4000-8000-000000000021";
const uuid = (n) => `10000000-0000-4000-8000-${String(n).padStart(12, "0")}`;

// A fixed-offset zone whose local time is about noon now, inside the nudge window.
const offset = new Date().getUTCHours() - 12;
const noonZone = offset === 0 ? "Etc/GMT" : offset > 0 ? `Etc/GMT+${offset}` : `Etc/GMT${offset}`;

await db.exec(`
  create role anon; create role authenticated; create role service_role bypassrls;
  create schema auth;
  create function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
  create schema coach_private; grant usage on schema coach_private to authenticated;
  grant usage on schema public to authenticated, service_role;
  create table public.profiles (id uuid primary key, role text, access_status text, full_name text, avatar_url text,
    health_data_consent boolean, date_of_birth date, athlete_timezone text, active_plan_id uuid);
  create table public.plans (id uuid primary key, athlete_id uuid, fight_date date);
  create table public.coach_connections (id uuid primary key default gen_random_uuid(), coach_id uuid, athlete_id uuid,
    status text, share_injuries boolean not null default false);
  alter table public.coach_connections enable row level security;
  grant select on public.coach_connections to authenticated;
  create policy participants on public.coach_connections for select to authenticated
    using (coach_id = (select auth.uid()) or athlete_id = (select auth.uid()));
  create table public.session_completions (id uuid primary key, athlete_id uuid, status text, completion_origin text,
    training_day date, session_rpe integer, prescription_snapshot jsonb);
  create table public.external_training_sessions (id uuid primary key, athlete_id uuid, deleted_at timestamptz,
    training_day date, activity text, duration_minutes integer, effort text);
  create table public.sparring_logs (id uuid primary key, athlete_id uuid, external_session_id uuid, training_day date,
    rounds_completed integer, intensity text, rocked boolean, head_contact text);
  create table public.today_checkins (athlete_id uuid, training_day date, unique (athlete_id, training_day));
  create table public.injury_flags (athlete_id uuid, status text);
  create table public.attachment_assets (id uuid primary key, owner_id uuid, kind text, status text,
    updated_at timestamptz not null default now(), intake_removed boolean not null default false);
  create table public.training_attachments (asset_id uuid);
  create table public.notification_deliveries (id serial primary key, notification_class text);
  create function coach_private.health_session(p_kind text, p_title text, p_blocks jsonb) returns boolean
    language sql stable as $$ select coalesce(p_kind, '') in ('rehab', 'recovery') or coalesce(p_title, '') ~* 'rehab' $$;

  insert into public.profiles values
    ('${coach}', 'coach', 'approved', 'Coach Rey', null, true, '1985-01-01', 'UTC', null),
    ('${fighter}', 'athlete', 'approved', 'Leo Martins', null, true, '1999-01-01', '${noonZone}', null),
    ('${minor}', 'athlete', 'approved', 'Junior', null, true, (current_date - interval '15 years')::date, '${noonZone}', null),
    ('${stranger}', 'coach', 'approved', 'Other Coach', null, true, '1980-01-01', 'UTC', null),
    ('${otherFighter}', 'athlete', 'approved', 'Not Connected', null, true, '1998-01-01', 'UTC', null);
  insert into public.coach_connections (coach_id, athlete_id, status) values
    ('${coach}', '${fighter}', 'accepted'), ('${coach}', '${minor}', 'accepted');
  insert into public.session_completions values
    ('${rehab}', '${fighter}', 'done', null, current_date, 6,
      '{"session": {"session_type": "rehab", "title": "Knee rehab"}}'),
    ('${otherRecord}', '${otherFighter}', 'done', null, current_date, 6,
      '{"session": {"session_type": "strength", "title": "Lower body"}}');
  insert into public.attachment_assets (id, owner_id, kind, status) values
    ('${coachPhoto}', '${coach}', 'photo', 'ready'), ('${strangerPhoto}', '${stranger}', 'photo', 'ready');
`);
for (const name of ["20261123020539_coach_chat_and_checkin_nudges.sql", "20261123023816_coach_chat_review_fixes.sql"]) {
  await db.exec(await readFile(new URL(`../../supabase/migrations/${name}`, import.meta.url), "utf8"));
}
// Supabase's service role can write the stub tables; the migration's own grants are untouched.
await db.exec("grant all on public.today_checkins, public.attachment_assets, public.training_attachments, public.notification_deliveries, public.coach_connections to service_role; grant usage on all sequences in schema public to service_role;");

async function as(user, sql, params = []) {
  await db.exec("reset role");
  await db.query("select set_config('request.jwt.claim.sub', $1, false)", [user ?? ""]);
  await db.exec(user ? "set role authenticated" : "set role service_role");
  try { return (await db.query(sql, params)).rows; } finally { await db.exec("reset role"); }
}
async function refused(user, sql, params, pattern) {
  await assert.rejects(() => as(user, sql, params), pattern);
}
const send = (user, other, client, body, extra = {}) => as(user,
  `select public.coach_chat_send($1, $2, $3, $4, $5, $6, $7, $8) as m`,
  [other, client, extra.kind ?? "text", body, extra.asset ?? null, extra.quoteSource ?? null, extra.quoteTarget ?? null, extra.replyTo ?? null]);

// Fighter starts the chat; a retry is the same message.
const [{ m: first }] = await send(fighter, coach, uuid(1), "  Round 3 felt slow  ");
assert.equal(first.created, true);
assert.equal(first.body, "Round 3 felt slow");
const [{ m: retry }] = await send(fighter, coach, uuid(1), "Round 3 felt slow");
assert.equal(retry.created, false);
assert.equal(retry.id, first.id);
const [chat] = await as(null, "select * from public.coach_chats");
assert.equal(chat.coach_unread, 1);
assert.equal(chat.athlete_unread, 0);
assert.equal(chat.last_preview, "Round 3 felt slow");

// Both participants read it; nobody else does.
assert.equal((await as(coach, "select id from public.coach_messages")).length, 1);
assert.equal((await as(fighter, "select id from public.coach_messages")).length, 1);
assert.equal((await as(stranger, "select id from public.coach_messages")).length, 0);
assert.equal((await as(stranger, "select id from public.coach_chats")).length, 0);
await refused(stranger, "insert into public.coach_messages (chat_id, coach_id, athlete_id, sender_id, client_id, kind) values ($1, $2, $3, $2, $4, 'text')",
  [chat.id, stranger, fighter, uuid(99)], /permission denied/);
await refused(fighter, "update public.coach_chats set athlete_unread = 0", [], /permission denied/);
await refused(stranger, "select public.coach_chat_send($1, $2, 'text', 'hi')", [fighter, uuid(2)], /Chat unavailable/);
await refused(otherFighter, "select public.coach_chat_send($1, $2, 'text', 'hi')", [coach, uuid(3)], /Chat unavailable/);
// A retry id reused by the other side never returns someone else's message.
await refused(coach, "select public.coach_chat_send($1, $2, 'text', 'x')", [fighter, uuid(1)], /Chat unavailable/);

// The inbox: the chat and the connected fighter without one, one call.
const [{ inbox }] = await as(coach, "select public.coach_chat_inbox() as inbox");
assert.deepEqual(inbox.map((row) => row.name), ["Leo Martins", "Junior"]);
assert.equal(inbox[0].unread, 1);
assert.equal(inbox[1].chat_id, null);
await as(coach, "select public.coach_chat_read($1)", [chat.id]);
const [read] = await as(null, "select coach_unread, coach_read_at from public.coach_chats");
assert.equal(read.coach_unread, 0);
assert.ok(read.coach_read_at);

// Validation.
await refused(coach, "select public.coach_chat_send($1, $2, 'text', '   ')", [fighter, uuid(4)], /Write a message/);
await refused(coach, "select public.coach_chat_send($1, $2, 'text', $3)", [fighter, uuid(5), "x".repeat(2001)], /2000/);
await refused(coach, "select public.coach_chat_send($1, $2, 'video', 'x')", [fighter, uuid(6)], /2000/);

// Files: only the sender's own clean file, used once.
await refused(coach, "select public.coach_chat_send($1, $2, 'photo', '', $3)", [fighter, uuid(7), strangerPhoto], /File unavailable/);
await send(coach, fighter, uuid(8), "", { kind: "photo", asset: coachPhoto });
await refused(coach, "select public.coach_chat_send($1, $2, 'photo', '', $3)", [fighter, uuid(9), coachPhoto], /File unavailable/);

// Training cards are built by the database and masked without injury sharing.
const [{ m: card }] = await send(coach, fighter, uuid(10), "", { quoteSource: "unlxck", quoteTarget: rehab });
assert.equal(card.quote.title, "Programme session");
assert.equal(card.quote.meta, "Done · RPE 6");
await refused(coach, "select public.coach_chat_send($1, $2, 'text', '', null, 'unlxck', $3)", [fighter, uuid(11), otherRecord], /Record unavailable/);
await refused(coach, "select public.coach_chat_send($1, $2, 'text', 'hi', null, null, null, 999999)", [fighter, uuid(12)], /Message unavailable/);

// Reactions and reports.
const [{ r }] = await as(coach, "select public.coach_chat_react($1, '🔥') as r", [first.id]);
assert.equal(r[coach], "🔥");
await refused(coach, "select public.coach_chat_react($1, 'nope')", [first.id], /Reaction unavailable/);
await refused(stranger, "select public.coach_chat_react($1, '🔥')", [first.id], /Message unavailable/);
await refused(fighter, "select public.coach_chat_report($1, 'spam')", [first.id], /Message unavailable/);
await as(coach, "select public.coach_chat_report($1, 'spam')", [first.id]);
assert.equal((await as(null, "select * from public.coach_message_reports")).length, 1);
assert.equal((await as(coach, "select * from public.coach_message_reports").catch(() => [])).length, 0);

// Flood guard: 30 a minute per sender per chat (the photo and the card count).
for (let n = 100; n < 128; n += 1) await send(coach, fighter, uuid(n), `m${n}`);
await refused(coach, "select public.coach_chat_send($1, $2, 'text', 'one too many')", [fighter, uuid(200)], /Too many messages/);

// Check-in nudges: coaches only, adults only, a claim is a lease, a confirmed push ends the day.
await refused(fighter, "select public.coach_set_checkin_nudge($1, 127::smallint, '12:00')", [coach], /Fighter not found/);
await refused(coach, "select public.coach_set_checkin_nudge($1, 127::smallint, '12:00')", [minor], /18 and over/);
await refused(coach, "select public.coach_set_checkin_nudge($1, 0::smallint, '12:00')", [fighter], /at least one day/);
await refused(coach, "select public.coach_set_checkin_nudge($1, 127::smallint, '23:00')", [fighter], /05:00 and 21:00/);
await as(coach, "select public.coach_set_checkin_nudge($1, 127::smallint, '12:00')", [fighter]);
await refused(coach, "select * from public.claim_coach_checkin_nudges()", [], /permission denied/);
const claimed = await as(null, "select * from public.claim_coach_checkin_nudges()");
assert.equal(claimed.length, 1);
assert.equal(claimed[0].athlete_id, fighter);
assert.equal(claimed[0].coach_name, "Coach Rey");
// A held lease is not claimed twice; an unconfirmed claim is not "missed".
assert.equal((await as(null, "select * from public.claim_coach_checkin_nudges()")).length, 0);
const inboxNow = async () => (await as(coach, "select public.coach_chat_inbox() as inbox"))[0].inbox;
assert.equal((await inboxNow()).find((row) => row.other_id === fighter).missed_checkin, false);
// A failed push is never confirmed: once the lease lapses, the nudge is claimed again.
await as(null, "update public.coach_checkin_nudges set last_claimed_at = now() - interval '16 minutes'");
assert.equal((await as(null, "select * from public.claim_coach_checkin_nudges()")).length, 1);
await refused(coach, "select public.confirm_coach_checkin_nudge($1, $2)", [claimed[0].nudge_id, claimed[0].training_day], /permission denied/);
await as(null, "select public.confirm_coach_checkin_nudge($1, $2)", [claimed[0].nudge_id, claimed[0].training_day]);
assert.equal((await inboxNow()).find((row) => row.other_id === fighter).missed_checkin, true);
// Confirmed: never claimed again that day, even after the lease.
await as(null, "update public.coach_checkin_nudges set last_claimed_at = now() - interval '16 minutes'");
assert.equal((await as(null, "select * from public.claim_coach_checkin_nudges()")).length, 0);
// A fighter who already checked in is never nudged.
await as(null, "update public.coach_checkin_nudges set last_sent_day = null, last_sent_at = null, last_claimed_at = null");
await as(null, "insert into public.today_checkins values ($1, $2)", [fighter, claimed[0].training_day]);
assert.equal((await as(null, "select * from public.claim_coach_checkin_nudges()")).length, 0);
const [{ inbox: cleared }] = await as(coach, "select public.coach_chat_inbox() as inbox");
assert.equal(cleared.find((row) => row.other_id === fighter).missed_checkin, false);
assert.equal((await as(fighter, "select id from public.coach_checkin_nudges")).length, 1);

// Chat pushes have their own class; files in a chat survive cleanup.
await as(null, "insert into public.notification_deliveries (notification_class) values ('conversation')");
await as(null, "update public.attachment_assets set updated_at = now() - interval '2 days'");
await as(null, "select * from public.attachment_cleanup_candidates()");
const assets = await as(null, "select id, status from public.attachment_assets order by id");
assert.equal(assets.find((a) => a.id === coachPhoto).status, "ready");
assert.equal(assets.find((a) => a.id === strangerPhoto).status, "rejected");

// Disconnect: the coach loses access on the next request; the fighter keeps history.
await as(null, "update public.coach_connections set status = 'revoked' where athlete_id = $1", [fighter]);
assert.equal((await as(coach, "select id from public.coach_messages")).length, 0);
assert.equal((await as(coach, "select id from public.coach_chats")).length, 0);
assert.ok((await as(fighter, "select id from public.coach_messages")).length > 1);
await refused(coach, "select public.coach_chat_send($1, $2, 'text', 'still there?')", [fighter, uuid(300)], /Chat unavailable/);
await refused(fighter, "select public.coach_chat_react($1, '👍')", [first.id], /Message unavailable/);
// Receipts: a disconnected coach changes nothing; the fighter still can.
await as(null, "update public.coach_chats set coach_unread = 3, athlete_unread = 2 where athlete_id = $1", [fighter]);
await as(coach, "select public.coach_chat_read($1)", [chat.id]);
await as(fighter, "select public.coach_chat_read($1)", [chat.id]);
const [receipts] = await as(null, "select coach_unread, athlete_unread from public.coach_chats where id = $1", [chat.id]);
assert.deepEqual([receipts.coach_unread, receipts.athlete_unread], [3, 0]);
const [{ inbox: after }] = await as(coach, "select public.coach_chat_inbox() as inbox");
assert.deepEqual(after.map((row) => row.name), ["Junior"]);
const [{ inbox: kept }] = await as(fighter, "select public.coach_chat_inbox() as inbox");
assert.equal(kept.length, 1);
assert.equal(kept[0].active, false);
assert.equal((await as(null, "select * from public.claim_coach_checkin_nudges()")).length, 0);

console.log("coach chat migration: all checks passed");
