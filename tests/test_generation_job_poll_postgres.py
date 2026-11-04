"""The worker's claim poll returns exactly what the original OR filter did.

Runs against a disposable local PostgreSQL cluster (skips otherwise): the
original filter is kept here verbatim and compared with the migrated function
over random jobs, every limit edge and both legacy-blank settings.
"""

import os
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
POLL_MIGRATION = ROOT / "supabase/migrations/20261104190005_index_friendly_generation_job_poll.sql"

_ORIGINAL = """
create function original_claim(p_limit integer, p_stale_before timestamptz, p_include_legacy_blank boolean)
returns setof uuid language sql stable as $$
  select job.id from public.generation_jobs as job
  where job.status = 'queued'
    or (p_include_legacy_blank and coalesce(job.status, '') = '')
    or (job.status = 'running' and job.completed_at is null and job.stage1_result is null
        and job.final_result is null and coalesce(job.progress_milestones, '[]'::jsonb) = '[]'::jsonb
        and coalesce(job.heartbeat_at, job.started_at) <= p_stale_before)
  order by job.created_at asc
  limit least(greatest(coalesce(p_limit, 1), 1), 100);
$$;
"""

_JOBS = """
insert into public.generation_jobs(status, created_at, started_at, heartbeat_at, completed_at,
                                   progress_milestones, stage1_result, final_result)
select (array['queued','running','completed','failed','',null])[1 + floor(random() * 6)],
       now() - make_interval(secs => g),
       case when random() < .7 then now() - make_interval(secs => random() * 3600) end,
       case when random() < .6 then now() - make_interval(secs => random() * 3600) end,
       case when random() < .3 then now() end,
       (array[null, '[]'::jsonb, '["x"]'::jsonb])[1 + floor(random() * 3)],
       case when random() < .3 then '{}'::jsonb end,
       case when random() < .3 then '{}'::jsonb end
from generate_series(1, 1500) as g;
"""


@pytest.fixture(scope="module")
def poll_db():
    dsn = os.environ.get("XP_TEST_DATABASE_URL") or os.environ.get("REHAB_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("set XP_TEST_DATABASE_URL to a disposable localhost PostgreSQL cluster")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    assert conninfo_to_dict(dsn).get("host") in {"localhost", "127.0.0.1", "::1"}, "test cluster must be local"
    name = "unlxck_poll_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        for role in ("anon", "authenticated", "service_role"):
            if not admin.execute("select 1 from pg_roles where rolname=%s", (role,)).fetchone():
                admin.execute(sql.SQL("create role {}").format(sql.Identifier(role)))
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    local = make_conninfo(dsn, dbname=name)
    try:
        with psycopg.connect(local, autocommit=True) as db:
            db.execute("""
                create table public.generation_jobs(
                  id uuid primary key default gen_random_uuid(), status text, created_at timestamptz,
                  started_at timestamptz, heartbeat_at timestamptz, completed_at timestamptz,
                  progress_milestones jsonb, stage1_result jsonb, final_result jsonb);
                create index on public.generation_jobs (status, created_at);
            """)
            db.execute(POLL_MIGRATION.read_text(encoding="utf-8"))
            db.execute(_ORIGINAL)
            db.execute(_JOBS)
        yield local
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))


def test_union_poll_matches_the_original_filter_row_for_row(poll_db):
    import psycopg

    with psycopg.connect(poll_db, autocommit=True) as db:
        checked = 0
        for limit in (None, 0, 1, 7, 100, 500):
            for stale_seconds in (0, 600, 1800, 7200):
                for legacy in (True, False):
                    args = (limit, stale_seconds, legacy)
                    original = db.execute(
                        "select * from original_claim(%s, now() - make_interval(secs => %s), %s)", args
                    ).fetchall()
                    migrated = db.execute(
                        "select id from list_claimable_generation_jobs_v2(%s, now() - make_interval(secs => %s), %s)",
                        args,
                    ).fetchall()
                    assert migrated == original, args
                    checked += bool(original)
        assert checked  # the random jobs exercised non-empty results


def test_legacy_branch_is_skipped_without_the_flag(poll_db):
    import psycopg

    with psycopg.connect(poll_db, autocommit=True) as db:
        statuses = {row[0] for row in db.execute(
            "select status from list_claimable_generation_jobs_v2(100, now(), false)"
        )}
        assert statuses <= {"queued", "running"}
