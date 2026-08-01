# Blender Render Watchdog 2.3.1

Version 2.3.1 verifies every final network-rendered frame on the main computer.

Highlights:

- after all workers finish, the main PC validates every output frame;
- PNG files receive full chunk and checksum validation, with structural checks for JPEG, EXR, TIFF, WebP, BMP, HDR and TGA;
- corrupt frames are quarantined with a `.corrupt-*` suffix instead of being destroyed;
- failed integrity checks return the frame to the shared queue for another worker;
- completion is reported only after the repeated frame passes the final audit;
- a frame that fails three render attempts remains visibly failed instead of looping forever.
