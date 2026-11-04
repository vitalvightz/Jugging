from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "supabase" / "schema.sql"
ENV_EXAMPLE = ROOT / ".env.example"


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_supabase_auth_redirects_use_configured_site_origin():
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
    auth_form = _read("web/components/auth-form.tsx")
    forgot_password = _read("web/app/forgot-password/page.tsx")
    reset_password = _read("web/app/reset-password/page.tsx")

    assert "NEXT_PUBLIC_SITE_URL=http://localhost:3000" in env_example
    # Every emailed link is built by the one helper that refuses untrusted
    # origins. Reintroducing a raw origin here is how reset links ended up
    # pointing at protected *.vercel.app preview deployments.
    assert 'import { buildAuthRedirectUrl } from "@/lib/site-url";' in auth_form
    assert 'import { buildAuthRedirectUrl } from "@/lib/site-url";' in forgot_password
    assert 'emailRedirectTo: buildAuthRedirectUrl(authRedirectPath())' in auth_form
    assert 'buildAuthRedirectUrl(authRedirectPath())' in auth_form
    assert 'redirectTo: buildAuthRedirectUrl("/reset-password")' in forgot_password
    assert "getSiteOrigin" not in auth_form
    assert "getSiteOrigin" not in forgot_password
    assert 'router.replace("/login")' in reset_password
    assert "client.auth.updateUser({ password })" in reset_password
    assert "client.auth.signOut()" in reset_password


def test_auth_email_origin_rejects_untrusted_hosts():
    site_url = _read("web/lib/site-url.ts")

    # Auth emails are opened later, from another device, so the current
    # deployment host is not good enough. Only an explicit configuration, a
    # non-Vercel production domain, or local development may be emailed.
    assert "function resolveAuthEmailOrigin" in site_url
    assert "isVercelDeploymentOrigin(vercelProduction)" in site_url
    assert "isLocalDevelopmentOrigin(currentOrigin)" in site_url
    # A scheme that is present must be http(s); prefixing https:// onto
    # "ftp://app.unlxck.com" silently yields the host "ftp".
    assert 'scheme !== "http" && scheme !== "https"' in site_url


def test_reset_password_expired_link_fallback_is_statically_present():
    reset_password = _read("web/app/reset-password/page.tsx")
    auth_link = _read("web/lib/auth-link.ts")

    # Supabase reports failures on the fragment (implicit flow) and on the
    # query string (PKCE and /auth/v1/verify). Reading only one half is how an
    # expired link came to render a blank form.
    assert "readAuthLinkStatus" in reset_password
    assert "location.hash" in reset_password
    assert "location.search" in reset_password
    assert "location.hash" in auth_link
    assert "location.search" in auth_link
    assert "expired" in auth_link
    assert 'href="/forgot-password"' in reset_password
    assert "Request a new reset link" in reset_password


def test_reset_password_requires_a_verified_recovery_link():
    reset_password = _read("web/app/reset-password/page.tsx")

    # A credential-shaped param is not proof. supabase-js keeps an existing
    # session when it fails to consume a URL, so "?code=arbitrary" on a
    # signed-in browser must not unlock the form. Only a PASSWORD_RECOVERY
    # event, a session whose token is the one the URL carried, or a code
    # Supabase itself accepts may open it.
    assert 'event === "PASSWORD_RECOVERY" && session' in reset_password
    assert "session.access_token === urlAccessToken" in reset_password
    assert "client.auth.exchangeCodeForSession(urlCode)" in reset_password
    # The weaker "any session plus any credential param" gate must not return.
    assert "cameFromRecoveryLink" not in reset_password
    assert 'linkStatus.kind === "credentials";' not in reset_password
    # An unrelated signed-in session gets sent to the route that asks for the
    # current password instead.
    assert "setCanChangeInSettings(true)" in reset_password
    assert 'href="/settings"' in reset_password


def test_recovery_marker_is_only_written_from_the_supabase_event():
    redirect = _read("web/components/password-recovery-redirect.tsx")
    reset_password = _read("web/app/reset-password/page.tsx")
    layout = _read("web/app/layout.tsx")
    recovery = _read("web/lib/password-recovery.ts")

    # Supabase rewrites any non-allow-listed redirect_to to the project Site
    # URL, so a recovery link routinely lands somewhere other than
    # /reset-password. Listening app-wide is what stops that from silently
    # degrading into a plain sign-in.
    assert "PasswordRecoveryRedirect" in layout
    assert 'event !== "PASSWORD_RECOVERY"' in redirect
    assert "markPasswordRecovery(session.user.id)" in redirect

    # The marker is proof, so it must never be derived from the URL — that is
    # the "?code=arbitrary unlocks the form" bypass in another shape. Only the
    # redirect component writes it, and only from the Supabase event.
    assert "markPasswordRecovery" not in reset_password
    assert "clearPasswordRecovery" in reset_password
    # Bound to one user and time limited.
    assert "readPasswordRecovery()?.userId === userId" in recovery
    assert "RECOVERY_TTL_MS" in recovery

    # Navigating inline would deadlock: supabase-js holds its auth lock across
    # this callback, and the reset page calls getSession() as it mounts.
    assert "setTimeout(() => router.replace(RESET_PASSWORD_ROUTE), 0)" in redirect

    # The redirect fires as soon as Supabase verifies the link, so the proof
    # cannot depend on sessionStorage being writable — email clients open links
    # in restrictive in-app browsers. Mirrors lib/generation-intent.ts.
    assert "memoryMarker" in recovery
    assert "memoryMarker = marker" in recovery

    # Every terminal branch spends the marker, including the early return for a
    # URL that Supabase rejected.
    error_branch = reset_password.split('if (linkStatus.kind === "error")', 1)[1].split("return;", 1)[0]
    assert "clearPasswordRecovery();" in error_branch


def test_reset_password_leads_with_a_new_link_not_settings():
    reset_password = _read("web/app/reset-password/page.tsx")

    # /settings requires the current password, which is the one thing an
    # athlete in the reset flow does not have, so it can only ever be the
    # secondary suggestion.
    primary = reset_password.index("Request a new reset link")
    secondary = reset_password.index("Change it in Settings")
    assert primary < secondary
    assert "Change your password in Settings" not in reset_password


def test_login_surfaces_failed_email_link_outcomes():
    auth_form = _read("web/components/auth-form.tsx")

    # Magic links and signup confirmations land on /login; a rejected token
    # must say so instead of silently rendering an empty form.
    assert "readAuthLinkStatus" in auth_form
    assert "clearAuthLinkParams" in auth_form


def test_settings_page_keeps_username_and_password_sections():
    settings = _read("web/app/settings/page.tsx")

    assert "settingsUsername" in settings
    assert "username_rate_limit" in settings
    assert "Update username" in settings
    assert "currentPassword" in settings
    assert "newPassword" in settings
    assert "Update password" in settings


def test_store_change_username_uses_shared_validator():
    store = _read("api/store.py")
    supabase_store = store.split("class SupabaseAppStore(", 1)[1]

    change_username = re.search(
        r"def change_username\(self, athlete_id: str, username: str\).*?def get_latest_intake",
        supabase_store,
        re.DOTALL,
    )
    assert change_username is not None
    assert "normalized = validate_username(username)" in change_username.group(0)
    assert '"change_profile_username"' in change_username.group(0)


def test_schema_contains_required_role_and_username_security_triggers():
    schema = SCHEMA.read_text(encoding="utf-8")

    assert "create or replace function public.prevent_self_role_escalation()" in schema
    assert re.search(
        r"create trigger profiles_prevent_self_role_escalation\s+before insert or update on public\.profiles",
        schema,
        re.IGNORECASE,
    )
    role_guard = re.search(
        r"create or replace function public\.prevent_self_role_escalation\(\).*?\$\$;",
        schema,
        re.IGNORECASE | re.DOTALL,
    )
    assert role_guard is not None
    role_guard_text = role_guard.group(0)
    assert "auth.role() <> 'service_role'" in role_guard_text
    # No is_admin() bypass: role changes are service-role-only so a stale DB-role
    # admin (email removed from UNLXCK_ADMIN_EMAILS) cannot self-escalate.
    assert "not public.is_admin()" not in role_guard_text
    assert "Only the backend service role can change profile roles." in role_guard_text

    assert "username text unique" in schema
    assert "username_change_history jsonb not null default '[]'::jsonb" in schema
    assert "active_plan_id uuid references public.plans(id) on delete set null" in schema
    assert "profiles_active_plan_id_idx" in schema
    assert "profiles_username_key" in schema
    assert "profiles_username_length" in schema
    assert "profiles_username_lowercase" in schema
    assert "profiles_username_format" in schema
    assert "create or replace function public.prevent_username_policy_bypass()" in schema
    assert "create trigger profiles_prevent_username_policy_bypass" in schema


def test_schema_blocks_direct_critical_table_mutation_rls_policies():
    schema = SCHEMA.read_text(encoding="utf-8")

    for policy in (
        "plans_self_or_admin_insert",
        "plans_self_or_admin_update",
        "plans_self_or_admin_delete",
        "intakes_self_or_admin_insert",
        "intakes_self_or_admin_update",
        "intakes_self_or_admin_delete",
        "generation_jobs_self_or_admin_insert",
        "generation_jobs_self_or_admin_update",
        "generation_jobs_self_or_admin_delete",
    ):
        assert f'drop policy if exists "{policy}"' in schema
        assert f'create policy "{policy}"' not in schema

    # SELECT is own-rows-only now (renamed from the misleading "*_self_or_admin");
    # the admin-inclusive names are gone and no is_admin() cross-athlete grant
    # survives in a live policy clause.
    for policy in (
        "plans_self_select",
        "intakes_self_select",
        "generation_jobs_self_select",
    ):
        assert f'create policy "{policy}"' in schema
    for policy in (
        "plans_self_or_admin_select",
        "intakes_self_or_admin_select",
        "generation_jobs_self_or_admin_select",
    ):
        assert f'create policy "{policy}"' not in schema
    assert "or public.is_admin()" not in schema
