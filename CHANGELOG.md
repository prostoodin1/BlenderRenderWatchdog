# Changelog

## 2.4.2 — 2026-08-05

### Device render controls

- Open a matte-glass settings dialog by selecting a connected render worker on the main PC.
- Choose CPU, GPU or combined rendering, override Samples and assign a manual frame range per device.
- Return to automatic balancing or disconnect the selected worker without losing its active frame.

### Quieter Windows rendering

- Launch local Blender renders, distributed frames, project packing, FFmpeg and helper tools without flashing CMD windows.
- Reuse one tested Windows process helper across every background subprocess.

### Lighter Android interface

- Replaced the heavy rectangular bottom bar with a floating matte-glass cloud navigation surface.
- Added lightweight animated tab pills, softer cards and a calmer multi-tone background.
- Updated the native app and LAN protocol versions to 2.4.2.

### Engineering

- Expanded the suite to 63 tests, including render-device configuration and hidden Windows processes.
- Kept existing persistent access codes compatible with 2.4.1 installations.

## 2.4.1 — 2026-08-05

### Stable or rotating access

- Added a choice between a fresh code/link on every service start and persistent LAN credentials.
- Added an editable access key and one-click key regeneration; changing it revokes old mobile access.
- Kept network-render and mobile tokens separate even when they come from the same saved key.

### Native Android app

- Added an installable Android APK with matte Liquid Glass styling and bottom Devices, History and Settings tabs.
- Added BRWM1 sync codes, locally saved computers, background status refresh and optional device removal.
- Added remote pause/resume and stop controls plus recent render history from every saved computer.
- Added English and Russian Android resources.

### Engineering

- Added an authenticated mobile history endpoint and shared sync-code tests.
- Added a dedicated GitHub Actions APK build and expanded the suite to 59 tests.
- Kept protocol, Android app, UI layout and release work in separate commits.

## 2.4.0 — 2026-08-03

### Unfinished-render startup recovery

- Added an opt-in Windows Startup recovery file for single renders and the persistent queue.
- Keep recovery armed while work is paused or failed, and remove it after success or an explicit stop.
- Limit unattended login recovery to three attempts so a broken setup cannot loop forever.

### Main-PC device control

- Added a controller action that disconnects a selected render worker.
- Return the disconnected worker's active frame to the shared queue immediately.
- Tell remote workers to exit cleanly when the controller has disconnected them.

### Faster customizable interface

- Added Graphite, Ocean, Emerald, Amber, Rose, Violet and custom accent themes.
- Made neutral Graphite the new default instead of violet.
- Added Fast transitions, which removes card sweeps and swaps tabs immediately by default.
- Reduced the full-motion tab transition from 235 ms to 160 ms with fewer redraws.

### Engineering

- Added appearance, startup-recovery and worker-disconnect tests; the suite now contains 51 tests.
- Updated source installation, uninstallation, mobile identity and CI compilation for 2.4.

## 2.3.1 — 2026-08-01

### Final frame integrity audit

- Added a main-PC integrity pass after every distributed render finishes.
- Quarantine corrupt output files and automatically requeue their frame numbers.
- Validate PNG checksums and structure, plus signatures and truncation markers for other Blender image formats.
- Delay successful network completion until all replacement frames pass validation.

## 2.3.0 — 2026-08-01

### Network render controls

- Added Continue missing frames and Manual frame range modes to the main PC.
- Added an editable main-PC network name and a separate Stop render action.
- Added per-device Samples, automatic balancing and strict manual allocation controls.
- Existing output frames are recognized before a resumed job and included in progress.

### Scheduling fixes

- Manual workers no longer leave their assigned range when it is complete.
- Automatic workers no longer claim frames reserved for manually configured devices.
- Stopping a network render now finishes active frames while preventing new assignments.

## 2.2.2 — 2026-07-31

### Shared network visibility

- Added controller identity and a unified device list to the authenticated network status endpoint.
- Show the main computer and every connected worker on both controller and worker installations.
- Added online state, current frame, completed frames, average time, allocation and shared progress for connected devices.

## 2.2.1 — 2026-07-31

### Background hardware detection

- Prevented the PowerShell/WMIC GPU check from flashing a terminal window every 15 seconds on Windows.
- Kept hot-plug GPU detection active without changing its polling interval or network-render behavior.

## 2.2.0 — 2026-07-31

### Easier multi-device rendering

- Reworked the Network tab around two explicit roles: Connect and Become main.
- Kept the connection-code field and connected-device list visible in the role where they are needed.
- Added automatic participation of the main computer when a distributed render starts.
- Preserved automatic pull-based load balancing, hot-plug workers, retries and optional manual frame ranges.

### Languages

- Kept the complete English and Russian interface with instant switching and saved language preference.
- Added Russian translations for all new 2.2 network controls.

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
