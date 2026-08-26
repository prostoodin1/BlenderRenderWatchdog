# Changelog

## Unreleased

## 3.0.4 — 2026-08-26

### Simpler worker connection

- Added one visible Main PC action that creates and copies a complete SSH invitation with the group, endpoint, user and dedicated key.
- Replaced the worker's manual SSH fields with two clear inputs: an SSH invitation link and a BRW/one-time LAN code.
- Kept host, port and identity controls behind Advanced while offering automatic OpenSSH client installation when required.
- Preserved group identity metadata in generated SSH invitations and removed embedded private keys from the visible field after import.

### Uninterrupted frame assignments

- Locked contiguous assignments to one Blender animation process, so a 20-frame chunk renders frames 1 through 20 without restarting Blender between frames.
- Added regression coverage for the single-process `start/end/animation` command.

## 3.0.3 — 2026-08-25

### Render group workflow

- Split the compact group controls into dedicated Main PC and Worker mini-tabs.
- Added a worker-side list that merges live LAN groups with remembered SSH routes.
- Persisted group access tokens, remote routes and per-group SSH identities so devices remain recognizable across restarts and network changes.
- Added a visible one-click SSH key action to the Main PC tab and group metadata to reusable invitations.
- Added automatic worker rejoin after a temporary controller or tunnel outage while preserving explicit controller disconnects.

### Continuous frame chunks

- Added live `completed/total` progress for each assigned frame chunk.
- Workers immediately claim the next contiguous chunk after finishing the current one.
- Hidden Blender batch launches now report completed output frames while the process is still running.

### Computer-wide render profile

- Made CPU/GPU mode, Cycles backend and chunk size persistent settings of the computer instead of the selected project.
- Prevented queue selection from replacing the saved hardware profile with stale values from another `.blend`.
- Applied the same global profile to normal queue renders and network plans.

## 3.0.1 — 2026-08-25

### Settings navigation

- Restored Settings as a first-class tab in the unified desktop interface.
- Made the header Settings button switch to the same tab instead of opening a second window.
- Kept update, power, language, colour, mobile dashboard and LAN security controls together on the restored scrollable page.

## 3.0.0 — 2026-08-25

### Unified workspace and project queue

- Combined projects, active render state and device groups into one lightweight Workspace.
- Added a single persisted active project shared by the queue, network plan and mobile state.
- Added queue format v2 migration, source revision fingerprints and duplicate-project updates.
- Added fixed or adaptive contiguous frame chunks with a default target of 10 frames.

### Persistent render groups

- Added saved render groups with open or one-time-code LAN access and `brw://join/...` invitations.
- Added one-click SSH invitations: the main PC creates a dedicated Ed25519 key, authorizes it through Windows OpenSSH and embeds the worker credential in the share link.
- Workers extract embedded SSH credentials into a restricted local file and never persist the private key in the regular application config.
- Added stable installation identities and hardware capability records so reconnects and IP changes do not duplicate devices.
- Added group discovery metadata without exposing access tokens and cached offline members for a stable device list.
- Added manual coordinator takeover for a saved group when its previous main PC is unavailable.

### Render devices and performance

- Added per-project and per-device CPU, GPU, CPU + GPU and automatic modes.
- Added explicit OptiX, CUDA, HIP, oneAPI and Metal selection with capability validation.
- Render workers now process a contiguous frame batch in one hidden Blender launch.
- Network ETA now uses the measured throughput of online workers.
- Added a code-native connection icon and disabled heavyweight effects on the unified navigation path.
- Expanded the automated suite to 102 tests covering project state, groups, stable reconnects, chunks, backends, SSH invitations and LAN metadata.

## 2.6.0 — 2026-08-22

### Local network pairing

- Made LAN the default transport with no Tailscale installation requirement.
- Added automatic discovery of visible main PCs on the local broadcast network.
- Added optional six-digit one-time pairing codes and an explicit code-free mode for trusted LANs.
- Issued a unique persistent token to every approved device so later reconnects do not ask for the code again.
- Kept discovery announcements free of access tokens and rate-limited invalid pairing attempts.

### OpenSSH transport

- Replaced the Tailscale UI and dependency with optional Windows OpenSSH Client/Server installation.
- Added BRW4 connection codes carrying the SSH endpoint without storing a password.
- Added hidden, asynchronous SSH local forwarding using a private key or `ssh-agent`.
- Kept legacy BRW2/BRW3 decoding compatibility while moving new connections to LAN and SSH.

### Interface and engineering

- Added main-PC visibility and pairing-policy controls to the Network tab.
- Added a discovered-controller picker and saved trusted reconnect flow for workers.
- Reworked the Network layout after visual QA to keep role controls visible and SSH settings scrollable.
- Expanded the automated suite to 78 tests covering discovery, one-time pairing, trusted reconnects and OpenSSH detection.

## 2.5.1 — 2026-08-16

### Tailscale Internet rendering

- Added Internet via Tailscale using the controller's stable private Tailscale IPv4 address.
- Added official Windows installer download, sign-in launch and live connection-state detection.
- Added BRW3 pairing codes that identify Tailscale transport while retaining BRW2 LAN compatibility.
- Made persistent user-key codes stable across controller restarts and kept rotating codes available.

### Network status and progress

- Added a dedicated progress bar, completed-frame counter and ETA above connected devices.
- Sent heartbeats concurrently during project download, frame rendering and result upload.
- Prevented long-running workers from appearing Offline while their Blender process is active.

### Engineering

- Added isolated Tailscale status parsing and Windows integration tests.
- Added protocol compatibility and in-render heartbeat coverage.
- Kept Tailscale authentication outside Watchdog so account credentials are never stored by the app.

## 2.5.0 — 2026-08-15

### Progress and layout

- Added approximate remaining time to the main render view and frame range, frames/minute and frames/hour to network progress.
- Made every desktop tab vertically scrollable and added two-axis scrolling to the connected-device table.
- Preserved the last valid CPU/GPU snapshot when a transient Windows hardware query fails.

### Network workflow

- Added rotating or persistent access-code controls directly to the Network tab and saved worker names and join codes.
- Send the original `.blend` with its original filename instead of creating a second packed project copy.
- Write uploaded frames directly to the output chosen on the Render tab and automatically remove worker cache after completion.

### Android client

- Made the compact matte cloud navigation auto-hide after five seconds and reappear on interaction.
- Added connection grace retries, device renaming, optional detailed frame metrics and latest-frame previews.
- Added Emerald, Ocean and Amber themes plus a dedicated adaptive launcher icon.

### Updater and engineering

- Reset the PyInstaller environment on restart to prevent missing `_MEI` Python DLL errors.
- Added SHA-256 verification, replacement retries, working-directory launch and automatic rollback.
- Expanded automated coverage for render rate/ETA, worker cache cleanup, hardware snapshots, updater restart and Android features.

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
