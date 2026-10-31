import { expect, test, type Page, type Route } from "@playwright/test";
import { grantSignupAccess, isolateFromNetwork } from "./support";

const SUPABASE = process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://stub.supabase.co";
const ID = "00000000-0000-4000-8000-0000000000bb";
const user = { id: ID, aud: "authenticated", role: "authenticated", email: "fighter@example.com",
  email_confirmed_at: "2026-10-01T00:00:00Z", created_at: "2026-10-01T00:00:00Z",
  app_metadata: { provider: "google", providers: ["email", "google"] }, user_metadata: { full_name: "Fighter" },
  identities: [{ id: "google-identity", provider: "google" }, { id: "email-identity", provider: "email" }] };
const json = (route: Route, body: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });

async function oauth(page: Page, baseURL: string, returning = false, dateOfBirth: string | null = null) {
  await isolateFromNetwork(page, baseURL);
  let accepted = returning;
  const requests: Record<string, unknown>[] = [];
  const profile = () => ({ profile: {
    athlete_id: ID, email: user.email, full_name: "Fighter", role: "athlete", access_status: "approved",
    starting_path: returning ? "tracking_first" : null, technical_style: [], tactical_style: [], stance: "",
    professional_status: "", record: "", athlete_timezone: "Europe/London", athlete_locale: "en",
    appearance_mode: "dark", nutrition_profile: {}, date_of_birth: accepted ? "1996-05-04" : dateOfBirth,
    meets_minimum_age: accepted || Boolean(dateOfBirth), terms_accepted: accepted, health_consent_granted: false,
  }, plan_count: 0, latest_plan: null, latest_intake: null, username_rate_limit: {} });
  await page.route("**/api/me", route => json(route, profile()));
  await page.route(`${SUPABASE}/auth/v1/user`, route => json(route, user));
  await page.route(`${SUPABASE}/auth/v1/logout**`, route => json(route, {}));
  await page.route("**/api/me/compliance", async route => {
    requests.push(route.request().postDataJSON()); accepted = true; await json(route, profile());
  });
  await page.route(`${SUPABASE}/auth/v1/authorize**`, async route => {
    const url = new URL(route.request().url());
    expect(url.searchParams.get("provider")).toBe("google");
    expect(url.searchParams.get("redirect_to")).toBe(`${baseURL}/login`);
    const fragment = `#access_token=oauth-test-token&refresh_token=oauth-refresh&expires_in=3600&token_type=bearer&type=signup`;
    await route.fulfill({ status: 302, headers: { location: `${baseURL}/login${fragment}` }, body: "" });
  });
  return { requests, user };
}

for (const route of ["/login", "/signup"]) {
  test(`${route} starts with two actions and reveals email in place`, async ({ page, baseURL }) => {
    await isolateFromNetwork(page, baseURL!);
    if (route === "/signup") await grantSignupAccess(page, baseURL!);
    await page.goto(route);
    await expect(page.getByRole("button", { name: "Continue with Google", exact: true })).toBeVisible();
    await expect(page.locator("input")).toHaveCount(0);
    await page.getByRole("button", { name: "Continue with email", exact: true }).click();
    await expect(page.getByLabel("Email", { exact: true })).toBeFocused();
    await expect(page.getByLabel("Password", { exact: true })).toHaveAttribute("autocomplete", route === "/login" ? "current-password" : "new-password");
    await expect(page).toHaveURL(new RegExp(`${route}$`));
    if (route === "/login") await expect(page.getByRole("link", { name: "Forgot password?" })).toBeVisible();
    await page.getByRole("button", { name: "All sign-in options" }).click();
    await expect(page.locator("input")).toHaveCount(0);
  });
}

test("new Google identity supplies only missing compliance and can decline health consent", async ({ page, baseURL }) => {
  const { requests } = await oauth(page, baseURL!);
  await grantSignupAccess(page, baseURL!);
  await page.goto("/signup");
  await page.getByRole("button", { name: "Continue with Google", exact: true }).click();
  await expect(page).toHaveURL(/\/consent$/);
  await page.getByLabel("Date of birth", { exact: true }).fill("1996-05-04");
  await page.locator("#complianceTerms").check();
  await expect(page.locator("#complianceHealthConsent")).not.toBeChecked();
  await page.getByRole("button", { name: "CONTINUE", exact: true }).click();
  await expect(page).toHaveURL(/\/get-started$/);
  expect(requests).toEqual([{ date_of_birth: "1996-05-04", accept_terms: true }]);
  await page.goto("/login");
  await expect(page).toHaveURL(/\/get-started$/);
  await page.goto("/consent");
  await expect(page).toHaveURL(/\/get-started$/);
});

test("returning Google/email identity restores the existing workspace without compliance repeating", async ({ page, baseURL }) => {
  const { requests } = await oauth(page, baseURL!, true);
  await page.goto("/login");
  await page.getByRole("button", { name: "Continue with Google", exact: true }).click();
  await expect(page).toHaveURL(`${baseURL}/`);
  await page.goto("/login");
  await expect(page).toHaveURL(`${baseURL}/`);
  expect(requests).toHaveLength(0);
});

test("underage Google identity stays unverified and can sign out", async ({ page, baseURL }) => {
  const { requests } = await oauth(page, baseURL!);
  await page.goto("/login");
  await page.getByRole("button", { name: "Continue with Google", exact: true }).click();
  await expect(page).toHaveURL(/\/consent$/);
  const dob = new Date(); dob.setUTCFullYear(dob.getUTCFullYear() - 10);
  await page.getByLabel("Date of birth", { exact: true }).fill(dob.toISOString().slice(0, 10));
  await expect(page.locator("#compliance-dob-error")).toContainText("13");
  await page.locator("#complianceTerms").check();
  await expect(page.getByRole("button", { name: "CONTINUE", exact: true })).toBeDisabled();
  await page.reload();
  await expect(page).toHaveURL(/\/consent$/);
  expect(requests).toHaveLength(0);
  await page.getByRole("region", { name: "Finish your account" }).getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);
});

test("Google compliance keeps an existing DOB and records health consent only when checked", async ({ page, baseURL }) => {
  const { requests } = await oauth(page, baseURL!, false, "1996-05-04");
  await page.goto("/login");
  await page.getByRole("button", { name: "Continue with Google", exact: true }).click();
  await expect(page).toHaveURL(/\/consent$/);
  await expect(page.getByLabel("Date of birth", { exact: true })).toHaveCount(0);
  await page.locator("#complianceTerms").check();
  await page.locator("#complianceHealthConsent").check();
  await page.getByRole("button", { name: "CONTINUE", exact: true }).click();
  await expect(page).toHaveURL(/\/get-started$/);
  expect(requests).toEqual([{ accept_terms: true, health_data_consent: true }]);
});

test("email password login retains the existing account and landing route", async ({ page, baseURL }) => {
  await oauth(page, baseURL!, true);
  const bodies: Record<string, unknown>[] = [];
  await page.route(`${SUPABASE}/auth/v1/token**`, async route => {
    expect(new URL(route.request().url()).searchParams.get("grant_type")).toBe("password");
    bodies.push(route.request().postDataJSON());
    await json(route, { access_token: "email-test-token", refresh_token: "email-refresh", expires_in: 3600,
      token_type: "bearer", user });
  });
  await page.goto("/login");
  await page.getByRole("button", { name: "Continue with email", exact: true }).click();
  await page.getByLabel("Email", { exact: true }).fill(user.email);
  await page.getByLabel("Password", { exact: true }).fill("Southpaw-Jab-Cross-2026");
  await page.getByRole("button", { name: "Log in", exact: true }).click();
  await expect(page).toHaveURL(`${baseURL}/`);
  expect(bodies).toHaveLength(1);
  expect(bodies[0]).toMatchObject({ email: user.email, password: "Southpaw-Jab-Cross-2026" });
});

test("email magic link requires no password and cannot create a login account", async ({ page, baseURL }) => {
  await isolateFromNetwork(page, baseURL!);
  const bodies: Record<string, unknown>[] = [];
  await page.route(`${SUPABASE}/auth/v1/otp**`, async route => {
    bodies.push(route.request().postDataJSON()); await json(route, {});
  });
  await page.goto("/login");
  await page.getByRole("button", { name: "Continue with email", exact: true }).click();
  await page.getByLabel("Email", { exact: true }).fill(user.email);
  await page.getByRole("button", { name: "Email sign-in link", exact: true }).click();
  await expect(page.locator(".success-banner")).toBeVisible();
  expect(bodies).toHaveLength(1);
  expect(bodies[0]).toMatchObject({ email: user.email, create_user: false });
});

test("OAuth cancellation is scrubbed from URL and leaves both methods usable", async ({ page, baseURL }) => {
  await isolateFromNetwork(page, baseURL!);
  await page.goto("/login#error=access_denied&error_description=untrusted-provider-detail");
  await expect(page.locator(".error-banner")).toContainText("Google sign-in could not be completed");
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByText("untrusted-provider-detail")).toHaveCount(0);
  await page.getByRole("button", { name: "Continue with email", exact: true }).click();
  await expect(page.getByLabel("Email", { exact: true })).toBeVisible();
});

test("an account explicitly marked invite-pending is turned away before a profile exists", async ({ page, baseURL }) => {
  await isolateFromNetwork(page, baseURL!);
  const newUser = { ...user, created_at: new Date().toISOString(), user_metadata: { full_name: "Gatecrasher", invite_pending: true } };
  const meCalls: string[] = [];
  const metadataUpdates: unknown[] = [];
  let signedOut = false;
  await page.route("**/api/me", route => { meCalls.push(route.request().url()); return json(route, {}); });
  await page.route(`${SUPABASE}/auth/v1/user`, async route => {
    if (route.request().method() === "PUT") metadataUpdates.push(route.request().postDataJSON());
    await json(route, newUser);
  });
  await page.route(`${SUPABASE}/auth/v1/logout**`, route => { signedOut = true; return json(route, {}); });
  await page.route(`${SUPABASE}/auth/v1/authorize**`, route => route.fulfill({
    status: 302,
    headers: { location: `${baseURL}/login#access_token=new-google-token&refresh_token=r&expires_in=3600&token_type=bearer&type=signup` },
    body: "",
  }));

  // Previously rejected account, no invite cookie in this browser.
  await page.goto("/login");
  await page.getByRole("button", { name: "Continue with Google", exact: true }).click();

  await expect(page).toHaveURL(`${baseURL}/signup?invite=required`);
  await expect(page.getByText("You need an invite code to create an account.")).toBeVisible();
  await expect(page.getByLabel("Access code", { exact: true })).toBeVisible();
  expect(meCalls, "no profile is requested (and so none is created)").toHaveLength(0);
  expect(metadataUpdates).toContainEqual(expect.objectContaining({ data: { invite_pending: true } }));
  expect(signedOut).toBe(true);
});

test("a brand-new Google account from a browser that entered the code goes straight through", async ({ page, baseURL }) => {
  await oauth(page, baseURL!);
  await grantSignupAccess(page, baseURL!);
  // Same flow as a new Google identity, but the account was created just now,
  // so the invite guard runs (routes added later take precedence).
  const newUser = { ...user, created_at: new Date().toISOString() };
  let signedOut = false;
  await page.route(`${SUPABASE}/auth/v1/user`, route => json(route, newUser));
  await page.route(`${SUPABASE}/auth/v1/logout**`, route => { signedOut = true; return json(route, {}); });

  await page.goto("/signup");
  await page.getByRole("button", { name: "Continue with Google", exact: true }).click();

  // Into the app: the new account lands on the compliance step, not the gate.
  await expect(page).toHaveURL(/\/consent$/);
  expect(signedOut).toBe(false);
});
