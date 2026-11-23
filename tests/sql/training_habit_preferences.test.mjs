// PGLITE_ROOT points to an isolated QA install; application dependencies are unchanged.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
const require = createRequire(resolve(process.env.PGLITE_ROOT, 'package.json'));
const { PGlite } = require('@electric-sql/pglite');
const db = new PGlite();
await db.exec(`create role authenticated; create table profiles(id text primary key); insert into profiles values ('owner'),('other');
  alter table profiles enable row level security;
  create policy owner_select on profiles for select to authenticated using (id=current_setting('app.owner'));
  create policy owner_update on profiles for update to authenticated using (id=current_setting('app.owner')) with check (id=current_setting('app.owner'));
  grant select,update on profiles to authenticated;`);
const sql = await readFile(new URL('../../supabase/migrations/20261018110229_add_training_habit_preferences.sql', import.meta.url),'utf8');
await db.exec(sql); await db.exec(sql);
assert.deepEqual((await db.query(`select training_habits from profiles where id='owner'`)).rows[0].training_habits,{enabled:true, decisions:[]});
for (const bad of ['{}','{"enabled":true,"decisions":{}}','{"enabled":"true","decisions":[]}']) {
  await assert.rejects(db.query(`update profiles set training_habits=$1::jsonb where id='owner'`,[bad]));
}
await db.exec(`set role authenticated; set app.owner='owner';`);
assert.equal((await db.query(`select * from profiles`)).rows.length,1);
assert.equal((await db.query(`update profiles set training_habits_revision=1 where id='other' returning id`)).rows.length,0);
const update = `update profiles set training_habits=$1::jsonb, training_habits_revision=1 where id='owner' and training_habits_revision=0 returning id`;
const preferences = JSON.stringify({enabled:true,decisions:[{weekday:0,activity:'boxing',duration_minutes:60,state:'confirmed',decided_on:'2026-10-19'}]});
assert.equal((await db.query(update,[preferences])).rows.length,1);
assert.equal((await db.query(update,[preferences])).rows.length,0); // stale device loses compare-and-swap
await db.close(); console.log('Training habit migration, ownership and revision checks passed.');
