# Changelog

## 2.1.1 — 2026-07-31

### Interface languages

- Added an instant language selector to Settings with English and Russian.
- Localized navigation, cards, actions, table headings, dialogs and live render states.
- Saved the selected language in the existing user configuration and detected Russian on first launch when Windows uses a Russian locale.
- Filled the unused Settings space with a balanced Interface section instead of adding another crowded card.

### Engineering

- Added a dependency-free localization module with formatted status messages and English fallbacks.
- Added localization tests and included the new module in source installs and CI compilation.

## 2.1.0 — 2026-07-30

### Matte glass interface

- Replaced classic ttk cards, tabs, buttons, entries and checkboxes with rounded Canvas-backed controls.
- Added layered matte surfaces, soft shadows, focus rings, top highlights and a calmer navy/violet palette.
- Rebalanced the Advanced layout for the wider animated controls.

### Motion and interaction

- Added eased page slides, button hover and press states, click ripples and animated toggle thumbs.
- Added staggered card-light sweeps, hover glow, status pulses and a shimmer progress indicator.
- Reworked progress interpolation so repeated updates share one animation loop.
- Updated the phone dashboard with glass surfaces, responsive motion and reduced-motion support.

### Engineering

- Added reusable `glass_ui.py` primitives and unit tests for colour/path animation math.
- Kept 2.1 work split into reviewable design, motion and release commits.

## 2.0.0 — 2026-07-30

### Interface and workflow

- Redesigned the desktop UI and split advanced workflows into dedicated tabs.
- Hide manual output, range and video fields when they are not relevant.
- Added subtle window, tab and progress transitions.
- Added explicit resolution and crash-retry controls.

### Queue and video

- Added a persistent, reorderable queue with per-project estimates and output details.
- Added shortest-project-first smart ordering and bounded retries.
- Added robust frames-first video output through FFmpeg in MP4, WebM, MKV and AVI.

### Prediction, history and recovery

- Added time and memory prediction from scene settings and project history.
- Added per-frame timing, hardest-frame views and persistent render history.
- Added Auto Fix preflight with safe one-click fixes.
- Improved pause-after-frame detection and active-range resume filtering.

### Network, mobile and devices

- Added token-authenticated LAN rendering with up to five workers.
- Added packed project transfer, frame upload, retry, manual allocation and automatic pull-based load balancing.
- Added hot-plug network workers and local GPU change detection.
- Added a token-protected responsive mobile dashboard with preview and controls.

### Sandbox and engineering

- Added sequential or parallel Draft/Balanced/Quality sandbox comparisons.
- Added standalone source modules, unit/integration tests and GitHub Actions.
