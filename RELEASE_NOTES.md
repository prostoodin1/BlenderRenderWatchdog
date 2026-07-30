# Blender Render Watchdog 2.0

Version 2.0 implements the complete public upgrade roadmap from issues #1, #2 and #3.

Highlights:

- redesigned interface with Advanced, Network, Insights and Sandbox workflows;
- persistent smart queue with estimates, retries and shortest-first ordering;
- robust video composition to MP4, WebM, MKV or AVI;
- render time and memory prediction, hardest-frame analytics and history;
- Auto Fix preflight for common render problems;
- distributed rendering across up to five LAN computers with dynamic load balancing;
- hot-plug workers and hardware detection;
- phone-friendly local dashboard with progress, preview and render controls;
- Render Sandbox comparison mode;
- automatic update support, tests and documented source builds.

The network and mobile features operate on the local network and use private random access tokens. FFmpeg is required only when video composition is enabled.
