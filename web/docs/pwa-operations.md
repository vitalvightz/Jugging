# UNLXCK PWA operations

UNLXCK remains one Next.js web application. The PWA layer adds installation, standalone launch, a static offline fallback, and controlled update prompts; it does not create a native iOS or Android project.

## Files that control the PWA

- `app/manifest.ts` — install name, colours, icons, start URL, and shortcuts.
- `app/layout.tsx` — PWA metadata, media-aware theme colours, and runtime registration.
- `components/pwa-register.tsx` — production-only service-worker registration, install-prompt capture, and update UX.
- `components/install-unlxck.tsx` — Settings install action plus iPhone/iPad instructions.
- `public/sw.js` — conservative cache and update policy.
- `public/offline.html` — static, non-personal offline fallback.
- `public/icons/` — home-screen, maskable, Apple touch, and favicon assets.
- `next.config.ts` — no-cache service-worker headers and same-origin worker CSP.

## Updating icons

The navigation and notification prompt use the current one-angle mark in `public/brand/unlxck-one-angle-48.png`. The installed app uses the black-background icon files in `public/icons/`; keep the supplied silver/white monogram and red diagonal light intact. Do not reintroduce the retired stacked monogram.

Keep `app/icon.png` identical to `public/icons/icon-512x512.png` and
`app/apple-icon.png` identical to `public/icons/apple-touch-icon.png`; Next.js
automatically publishes those metadata routes, so stale copies can override
the correct icons configured in the layout.

Export these exact PNGs:

- `icon-192x192.png`
- `icon-512x512.png`
- `icon-maskable-512x512.png` (important artwork inside the central safe zone)
- `apple-touch-icon.png` (180 × 180)
- `favicon-32x32.png`
- `favicon-16x16.png`

The registration URL is fingerprinted from Vercel's deployment commit. A new frontend deployment therefore installs a new worker and automatically retires older `unlxck-pwa-*` caches; no manual cache-number edit is required.

## Installing

### iPhone and iPad

1. Open UNLXCK in Safari.
2. Tap **Share**.
3. Select **Add to Home Screen**.
4. Tap **Add**.

iOS does not expose Chromium’s `beforeinstallprompt` event, so the Settings action shows these steps rather than claiming a one-tap installation.

### Android and Chromium desktop

Open **Settings → Account → Install UNLXCK** and use the native install prompt. The panel appears only after the browser provides a real install prompt.

Browsers without either a native install prompt or the Safari iOS/iPadOS manual route do not see a misleading install panel. Manifest shortcuts remain browser-dependent.

## Testing standalone mode

Use a production build; registration is deliberately disabled under `next dev`.

```powershell
$env:NEXT_PUBLIC_API_BASE_URL='http://127.0.0.1:8000'
$env:NEXT_PUBLIC_SUPABASE_URL='https://example.supabase.co'
$env:NEXT_PUBLIC_SUPABASE_ANON_KEY='test-anon-key'
npm.cmd run build
npm.cmd run start -- --hostname 127.0.0.1 --port 3100
```

Then verify in Chromium DevTools **Application**:

- `/manifest.webmanifest` loads and references all icons.
- `/sw.js` controls `/` and has no registration error.
- Display mode is `standalone` after installation.
- The start URL is `/dashboard?source=pwa`.
- Login persists after closing and reopening the installed app.
- Settings hides the install action when already standalone.

Run the automated checks with:

```powershell
npm.cmd run typecheck
npm.cmd run lint
npm.cmd run test:unit
npm.cmd run build
npm.cmd run test:e2e
```

Chrome no longer provides the old Lighthouse PWA category. Use manifest/service-worker response checks, DevTools Application inspection, and real-device installation for installability; Lighthouse remains useful for performance, accessibility, best practices, and SEO.

## Cache policy

The service worker caches only:

- the static offline page;
- PWA icon and favicon files;
- same-origin, versioned `/_next/static/` assets encountered at runtime.

It never caches authenticated HTML, `/api` responses, Supabase or OpenAI traffic, plans, profiles, admin data, check-ins, nutrition data, generation jobs, or mutation requests. Navigation remains network-first, and the offline page appears only when the network request genuinely fails.

## Update flow

The registration URL and cache names use the current Vercel deployment fingerprint, so old static chunks cannot accumulate across deployments.

The worker does not call `skipWaiting()` during installation. Updated workers activate through the browser’s normal service worker lifecycle on a later visit.

## Final real-device readiness checklist

### iPhone and iPad

- Open in Safari and use **Share → Add to Home Screen**.
- Confirm the UNLXCK icon and app name are correct.
- Launch from the Home Screen and confirm standalone display.
- Check notch, status-bar, fixed-navigation, and home-indicator safe areas.
- Sign in, close the app, reopen it, and confirm login persistence.
- Confirm the Settings install panel is hidden after installation.

### Android

- Confirm Settings exposes the native install prompt only when the browser provides it.
- Confirm the maskable icon is centred and unclipped on the launcher.
- Launch standalone and verify navigation and login persistence.
- Deploy a test update and verify the explicit update prompt on a safe route.
- Confirm the update action defers during generation or edited forms, then returns later.
- Test the offline fallback and recovery after reconnecting.

### Desktop

- Install with Chrome and Edge and verify standalone launch.
- Confirm unsupported browsers hide the install panel instead of showing generic instructions.
- Uninstall, clear the site worker/cache in DevTools, and reinstall.

## Resetting during debugging

In Chromium DevTools:

1. Open **Application → Service Workers**.
2. Select **Unregister**.
3. Open **Application → Storage** and clear site data for the test origin.
4. Reload the page twice and confirm a fresh worker controls the page.

Do this only on a test device or local origin; clearing production site data signs the current browser out.

## Disabling the PWA safely

1. Remove `<PwaRegister>` from `app/layout.tsx` to stop future registration.
2. Deploy a final `public/sw.js` that deletes every `unlxck-pwa-*` cache and unregisters itself during activation.
3. After clients receive that cleanup worker, remove the manifest/install metadata and PWA assets in a later deployment.

Do not simply delete `sw.js`: already-installed workers can continue controlling returning clients until explicitly replaced or unregistered.
