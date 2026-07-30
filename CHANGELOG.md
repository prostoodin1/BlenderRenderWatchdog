# Changelog

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
