# Section-sized loading transitions

Today uses two separate section-sized loading boxes. The top box includes the title placeholders and three mini status placeholders, with no visible words. It reserves the full command section through the guidance feedback row. A shared 30rem minimum on pending and loaded command panels keeps the next section stationary; longer safety guidance remains free to grow. This intentionally gives short guidance extra space to maintain the minimum section size.

The lower box extends a viewport below its starting position, with a fading background and no bottom border or rounded bottom corners. It reads as continuing below the screen, and the footer no longer appears in the loading viewport.

Both large boxes and their small placeholders shimmer immediately. The section shimmer is a non-interactive overlay and does not affect layout. Local keyframes bind correctly under CSS-module scoping, and reduced motion disables the animation.

Home’s checking message remains available to screen readers but is visually hidden. Freshness, safety gating, retries, cinematic black fades, and the local opacity reveal remain unchanged. No artificial waits or dependencies added.

## Evidence

Synthetic authenticated athlete, production Next server, system Chromium at 390×900 and 1280×900. Browser plugin unavailable; Playwright used. API delay is 1800ms in tests only. The original supplied recording remains below for before comparison; its data differs from the test fixture.

![Original recording](loading-transitions/before-recording.jpg)

| Surface | Loading | Loaded |
| --- | --- | --- |
| Today mobile | ![](loading-transitions/today-loading-390.png) | ![](loading-transitions/today-loaded-390.png) |
| Today desktop | ![](loading-transitions/today-loading-1280.png) | ![](loading-transitions/today-loaded-1280.png) |
| Home mobile | ![](loading-transitions/home-loading-390.png) | ![](loading-transitions/home-loaded-390.png) |
| Home desktop | ![](loading-transitions/home-loading-1280.png) | ![](loading-transitions/home-loaded-1280.png) |

## Verification

- Browser checks verify the shimmer background position advances on both boxes at 390px and 1280px, with a 1.4s loop and no animation under reduced motion.
- Production build and typecheck pass; all nine Today component tests pass.
- Four production browser cases pass: mobile/desktop layout and loading completion, Home fresh-read gating, failed-read/retry.
- Loading Today has exactly two section boxes, no visible text, a top height of at least 480px, and a lower box ending below the viewport.
- Top command-panel height and starting position change by less than 2px between pending and loaded in the fixture. Home companion movement remains less than 2px. No horizontal overflow.
- Measurements exclude the existing page entrance. Reduced motion disables the local fade; screenshots show the loaded state after the opacity reveal.
- Full-screen transition components are untouched.

Limits: mobile Chromium emulation, not real iPhone Safari. Long safety evidence, optional check-in sections and larger accessibility text can still grow the page naturally. Measurements concern element geometry, not field CLS.
