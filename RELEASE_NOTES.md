# Blender Render Watchdog 2.3.0

Version 2.3 adds practical controls for restarting and tuning distributed renders.

Highlights:

- Continue missing frames scans the output folder and skips every frame already rendered;
- Manual frame range starts a network job from an explicit first and last frame;
- every worker can use automatic balancing or a strict manually assigned frame range;
- per-device Samples overrides are sent with each assigned frame;
- the main PC has an editable network name;
- Stop render stops assigning new frames without shutting down the controller;
- manually reserved ranges are no longer taken by automatic workers.
