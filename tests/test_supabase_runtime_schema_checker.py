"""Unit tests for the live Supabase runtime schema checker logic.

These tests exercise the pure comparison helpers in ``api.schema_requirements``
using *fake* catalog data. They never touch the network and do not require
Supabase credentials, so they run as part of the normal pytest suite.
"""

from __future__ import annotations

import pytest

from api.schema_requirements import (
    INDEX_REQUIREMENTS,
    PLAN_RUNTIME_REQUIRED_COLUMNS,
    REQUIRED_COLUMNS,
    REQUIRED_FUNCTIONS,
    REQUIRED_TABLES,
    RLS_REQUIRED_TABLES,
    SchemaIntrospection,
    SchemaIntrospectionError,
    evaluate_payload,
    evaluate_schema,
    find_missing_columns,
    find_missing_functions,
    find_missing_index_constraints,
    find_missing_tables,
    find_rls_issues,
)


# --- helpers to build a fully-valid fake catalog snapshot ------------------


def _all_required_index_names() -> list[str]:
    # Use the first accepted name of each requirement as the "present" one.
    return [req.accepted_names[0] for req in INDEX_REQUIREMENTS]


def _bare_function_names(schema: str = "public") -> list[str]:
    return [
        qualified.split(".", 1)[-1]
        for qualified in REQUIRED_FUNCTIONS
        if qualified.split(".", 1)[0] == schema
    ]


def _valid_payload() -> dict:
    return {
        "tables": list(REQUIRED_TABLES),
        "columns": {table: list(cols) for table, cols in REQUIRED_COLUMNS.items()},
        "functions": _bare_function_names(),
        "private_functions": _bare_function_names("private"),
        "indexes": _all_required_index_names(),
        "constraints": [],
        "rls": {table: True for table in RLS_REQUIRED_TABLES},
    }


# --- table checks ----------------------------------------------------------


def test_find_missing_tables_none_when_all_present():
    assert find_missing_tables(REQUIRED_TABLES) == []


def test_find_missing_tables_reports_absent():
    present = [t for t in REQUIRED_TABLES if t != "generation_jobs"]
    assert find_missing_tables(present) == ["generation_jobs"]


def test_canonical_intakes_table_is_athlete_intakes():
    # The app stores intakes in athlete_intakes; there is no bare "intakes" table.
    assert "athlete_intakes" in REQUIRED_TABLES
    assert "intakes" not in REQUIRED_TABLES


# --- column checks ---------------------------------------------------------


def test_find_missing_columns_none_when_all_present():
    columns = {table: list(cols) for table, cols in REQUIRED_COLUMNS.items()}
    assert find_missing_columns(columns) == []


def test_find_missing_columns_reports_table_qualified_name():
    columns = {table: list(cols) for table, cols in REQUIRED_COLUMNS.items()}
    columns["plans"] = [c for c in columns["plans"] if c != "stage2_payload"]
    assert find_missing_columns(columns) == ["plans.stage2_payload"]


def test_find_missing_columns_absent_table_reports_all_its_columns():
    columns = {table: list(cols) for table, cols in REQUIRED_COLUMNS.items()}
    del columns["generation_jobs"]
    missing = find_missing_columns(columns)
    expected = [f"generation_jobs.{c}" for c in REQUIRED_COLUMNS["generation_jobs"]]
    assert missing == expected


def test_plan_runtime_columns_are_subset_of_required_plan_columns():
    for column in PLAN_RUNTIME_REQUIRED_COLUMNS:
        assert column in REQUIRED_COLUMNS["plans"]


def test_profiles_active_plan_id_is_required_runtime_column():
    assert "active_plan_id" in REQUIRED_COLUMNS["profiles"]
    columns = {table: list(cols) for table, cols in REQUIRED_COLUMNS.items()}
    columns["profiles"] = [c for c in columns["profiles"] if c != "active_plan_id"]
    assert "profiles.active_plan_id" in find_missing_columns(columns)


# --- function checks -------------------------------------------------------


def test_find_missing_functions_none_when_all_present():
    assert find_missing_functions(_bare_function_names(), _bare_function_names("private")) == []


def test_find_missing_functions_reports_schema_qualified_name():
    present = [n for n in _bare_function_names() if n != "check_plan_generation_short_window_limit"]
    assert find_missing_functions(present, _bare_function_names("private")) == [
        "public.check_plan_generation_short_window_limit"
    ]


def test_private_helpers_are_required_in_the_private_schema():
    # Moved to `private` by harden_internal_functions_and_search_paths; a
    # same-named function in `public` does not satisfy the requirement.
    for name in ("is_admin", "prevent_self_role_escalation", "prevent_username_policy_bypass"):
        assert f"private.{name}" in REQUIRED_FUNCTIONS
        assert f"public.{name}" not in REQUIRED_FUNCTIONS
    everything_public = _bare_function_names() + _bare_function_names("private")
    assert find_missing_functions(everything_public) == [
        q for q in REQUIRED_FUNCTIONS if q.startswith("private.")
    ]


def test_introspection_reads_private_functions():
    snapshot = SchemaIntrospection.from_payload({"private_functions": ["is_admin"]})
    assert snapshot.private_functions == {"is_admin"}
    assert SchemaIntrospection.from_payload({}).private_functions == frozenset()


# --- index / constraint checks --------------------------------------------


def test_find_missing_index_constraints_none_when_all_present():
    assert find_missing_index_constraints(_all_required_index_names()) == []


def test_exercise_log_schema_is_required_by_the_deploy_gate():
    from api.schema_requirements import INDEX_REQUIREMENTS, REQUIRED_COLUMNS

    assert "exercise_logs" in REQUIRED_TABLES
    assert "exercise_logs" in RLS_REQUIRED_TABLES
    for column in ("athlete_id", "plan_id", "block_id", "training_day", "status", "prescribed", "actual"):
        assert column in REQUIRED_COLUMNS["exercise_logs"]
    # The upsert names this constraint's columns as its conflict target.
    assert any(
        "exercise_logs_occurrence_key" in requirement.accepted_names
        for requirement in INDEX_REQUIREMENTS
    )


def test_index_requirement_satisfied_by_constraint_alias():
    # The athlete/client-request requirement accepts either the unique index or
    # the uniqueness constraint name.
    present = {
        "product_events_athlete_id_event_name_request_id_key",
        "product_event_discovery_daily", "product_events_athlete_time", "product_events_retention",
        "generation_jobs_one_active_job_per_athlete",
        "generation_jobs_athlete_client_request_key",  # constraint form
        "plan_generation_rate_limits_athlete_created_idx",
        "profiles_username_idx",
        "profiles_active_plan_id_idx",
        "daily_checkins_athlete_date_key",
        "today_checkins_athlete_plan_day_key",
        "today_checkins_athlete_plan_free_day_key",
        "session_completions_athlete_session_day_key",
        "sparring_logs_athlete_day_idx",
        "sparring_logs_external_session_key",
        "sparring_log_requests_pkey",
        "exercise_logs_occurrence_key",
        "external_training_sessions_request_key",
        "external_training_sessions_athlete_day_idx",
        "manual_exercise_entries_request_key",
        "personal_exercises_name_key",
        "custom_training_plans_athlete_updated_idx",
        "private_video_assets_owner_created_idx",
        "community_revision_queue_idx", "community_exercises_creator_idx",
        "private_exercise_media_owner_idx",
        "private_media_budget_owner_created_idx",
        "injury_episode_events_delayed_once_idx",
        "xp_awards_athlete_idempotency_key",
        "xp_awards_one_daily_login_per_calendar_date",
        "beta_feedback_submitter_context_key",
        "beta_feedback_rate_limits_claim_idx",
        "beta_feedback_screenshot_expiry_idx",
        "notification_deliveries_profile_day_class_idx",
        "notification_evaluations_diagnostic_idx",
        # #3170 adds the two training-focus ON CONFLICT targets.
        "training_focus_inferences_source_key",
        "training_focus_call_budget_pkey",
    }
    assert find_missing_index_constraints(present) == []


def test_find_missing_index_constraints_reports_label():
    present = set(_all_required_index_names())
    present.discard("plan_generation_rate_limits_athlete_created_idx")
    missing = find_missing_index_constraints(present)
    assert missing == ["plan_generation_rate_limits athlete/created index"]


def test_profiles_active_plan_id_index_is_required():
    present = set(_all_required_index_names())
    present.discard("profiles_active_plan_id_idx")
    assert "profiles active_plan_id index" in find_missing_index_constraints(present)


# --- RLS checks ------------------------------------------------------------


def test_find_rls_issues_none_when_all_enabled():
    assert find_rls_issues({table: True for table in RLS_REQUIRED_TABLES}) == []


def test_find_rls_issues_reports_disabled_and_missing():
    rls = {table: True for table in RLS_REQUIRED_TABLES}
    rls["generation_jobs"] = False
    del rls["plans"]
    issues = find_rls_issues(rls)
    assert "generation_jobs RLS is not enabled" in issues
    assert "plans RLS is not enabled" in issues


# --- end-to-end evaluation + reporting -------------------------------------


def test_evaluate_payload_passes_for_valid_schema():
    result = evaluate_payload(_valid_payload())
    assert result.ok
    assert result.format_report() == "✅ Supabase runtime schema check passed."


def test_evaluate_payload_aggregates_all_problem_categories():
    payload = _valid_payload()
    payload["tables"] = [t for t in payload["tables"] if t != "plan_generation_rate_limits"]
    payload["columns"]["plans"] = [
        c for c in payload["columns"]["plans"] if c != "stage2_payload"
    ]
    payload["functions"] = [
        f for f in payload["functions"] if f != "check_plan_generation_short_window_limit"
    ]
    payload["indexes"] = [
        i
        for i in payload["indexes"]
        if i != "plan_generation_rate_limits_athlete_created_idx"
    ]
    payload["rls"]["generation_jobs"] = False

    result = evaluate_payload(payload)
    assert not result.ok
    assert "plan_generation_rate_limits" in result.missing_tables
    assert "plans.stage2_payload" in result.missing_columns
    assert "public.check_plan_generation_short_window_limit" in result.missing_functions
    assert "plan_generation_rate_limits athlete/created index" in result.missing_indexes
    assert "generation_jobs RLS is not enabled" in result.rls_issues


def test_failure_report_lists_only_nonempty_sections():
    payload = _valid_payload()
    payload["columns"]["plans"] = [
        c for c in payload["columns"]["plans"] if c != "stage2_payload"
    ]
    report = evaluate_payload(payload).format_report()

    assert report.startswith("❌ Supabase runtime schema check failed.")
    assert "Missing columns:" in report
    assert "- plans.stage2_payload" in report
    # Sections without problems must be omitted.
    assert "Missing tables:" not in report
    assert "RLS issues:" not in report


# --- introspection payload parsing -----------------------------------------


def test_introspection_unions_indexes_and_constraints():
    snapshot = SchemaIntrospection.from_payload(
        {
            "tables": ["plans"],
            "columns": {"plans": ["id"]},
            "functions": ["is_admin"],
            "indexes": ["idx_a"],
            "constraints": ["constraint_b"],
            "rls": {"plans": True},
        }
    )
    assert snapshot.index_constraint_names == {"idx_a", "constraint_b"}
    assert snapshot.rls_by_table["plans"] is True


def test_introspection_handles_missing_keys_gracefully():
    snapshot = SchemaIntrospection.from_payload({})
    assert snapshot.tables == frozenset()
    assert snapshot.columns_by_table == {}
    assert snapshot.index_constraint_names == frozenset()


def test_introspection_rejects_non_mapping_payload():
    with pytest.raises(SchemaIntrospectionError):
        SchemaIntrospection.from_payload([])  # type: ignore[arg-type]


def test_introspection_rejects_bad_columns_shape():
    with pytest.raises(SchemaIntrospectionError):
        SchemaIntrospection.from_payload({"columns": ["not", "a", "map"]})


def test_introspection_rejects_non_list_column_values():
    # A table whose column list is a bare string must raise cleanly rather than
    # silently iterating into single characters.
    with pytest.raises(SchemaIntrospectionError):
        SchemaIntrospection.from_payload({"columns": {"plans": "id"}})


def test_introspection_allows_null_column_value():
    snapshot = SchemaIntrospection.from_payload({"columns": {"plans": None}})
    assert snapshot.columns_by_table["plans"] == frozenset()


def test_evaluate_schema_matches_evaluate_payload():
    payload = _valid_payload()
    snapshot = SchemaIntrospection.from_payload(payload)
    assert evaluate_schema(snapshot).ok == evaluate_payload(payload).ok


def test_summarize_exc_redacts_long_secrets():
    # Importing the checker module is credential-free (the Supabase client is
    # built lazily inside functions), so this stays in the normal test run.
    from tools.check_supabase_runtime_schema import _summarize_exc

    secret = "sbp_" + "a1B2c3D4" * 6  # long token-like string
    summary = _summarize_exc(RuntimeError(f"connect failed key={secret}"))
    assert secret not in summary
    assert "[redacted_secret]" in summary


def test_sparring_log_schema_is_required_by_the_deploy_gate():
    from api.schema_requirements import INDEX_REQUIREMENTS, REQUIRED_COLUMNS

    assert "sparring_logs" in REQUIRED_TABLES
    assert "sparring_logs" in RLS_REQUIRED_TABLES
    assert "public.record_sparring_log" in REQUIRED_FUNCTIONS
    for column in ("athlete_id", "training_day", "intensity", "head_contact", "rocked"):
        assert column in REQUIRED_COLUMNS["sparring_logs"]
    assert any(
        "sparring_logs_athlete_day_idx" in requirement.accepted_names
        for requirement in INDEX_REQUIREMENTS
    )


def test_rehab_deploy_gate_requires_all_persistence_and_history_columns():
    required = {"demand", "prescribed_dose", "completed_dose", "evidence_source", "recorded_at", "created_at"}
    assert required <= set(REQUIRED_COLUMNS["rehab_exposures"])
    for missing in required:
        columns = {table: list(cols) for table, cols in REQUIRED_COLUMNS.items()}
        columns["rehab_exposures"].remove(missing)
        assert f"rehab_exposures.{missing}" in find_missing_columns(columns)


def test_analytics_migration_and_privacy_objects_are_required():
    payload = _valid_payload()
    payload["tables"].remove("product_events")
    payload["columns"]["profiles"].remove("analytics_excluded")
    payload["functions"].remove("set_product_analytics_preference")
    result = evaluate_payload(payload)
    assert not result.ok
    report = result.format_report()
    assert "product_events" in report and "analytics_excluded" in report
    assert "set_product_analytics_preference" in report


def test_live_deploy_gate_rejects_inactive_retention_job(monkeypatch):
    from types import SimpleNamespace
    import tools.check_supabase_runtime_schema as checker

    class Client:
        def rpc(self, name, args=None):
            data = (
                _valid_payload()
                if name == checker.INTROSPECTION_RPC
                else {"configured": False, "healthy": False}
            )
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))

    monkeypatch.setattr(
        checker, "_require_env", lambda: ("https://local.example", "not-a-secret")
    )
    monkeypatch.setattr(checker, "_build_client", lambda *_: Client())
    with pytest.raises(checker.RuntimeSchemaCheckError, match="retention job"):
        checker.run_check()
