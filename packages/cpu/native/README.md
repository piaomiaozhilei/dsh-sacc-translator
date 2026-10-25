# Rust screen translation

Windows x64 screen capture, region selection, OCR and translation overlay run in a native Rust helper. The DSH host sends recognized text to the currently selected model. Screenshots remain in memory and are not sent to the model or saved as translation audits.

Enable screen translation in the translator panel, then press Ctrl+Shift+S to select an area. Release the mouse to keep the selection, drag its edges to resize or its interior to move, then press Enter or click 翻译选区. Arrow keys move the selection; Shift+Arrow adjusts its bottom/right edges. The overlay supports original/translation switching, reselecting, retranslating, copying translated text and closing with Esc. Disabling the feature releases the global shortcut.

Version 0.2.1 uses a cached Gaussian-blurred screen backdrop and double-buffered painting. Selection borders remain visible through recognition and translation. Translations use one continuous frosted region with a dominant-color tint and adaptive text color, following SACC's original effect, without per-line background blocks. Selection buffers are released on close. Geometry and backdrop tests pass; final interactive visual verification was interrupted by the user.

The Win32 UI uses an event loop without a repaint timer. ONNX inference uses two intra-op threads, one inter-op thread, and disabled spinning. A bounded queue prevents accumulating captures. The OCR subprocess loads lazily and exits after 45 seconds of inactivity; a Windows job object terminates it if the parent exits. No Python process is used for screen OCR. Document translation retains its existing runtime.

The helper reuses the installed runtime's ONNX Runtime DLL and PP-OCRv6 small models. CPU inference is used for screen OCR in both editions. End users do not need Rust installed.

Validation on the development machine: four horizontal English/Chinese lines were correctly recognized; measured OCR runs were 254–414 ms after a 730 ms model load. After model release the helper used approximately 32 MiB working set, with 0 ms process CPU time measured over 10 seconds. These are sample measurements, not a comparison against the Python implementation or a guarantee for other machines.

Current limitations: horizontal, axis-aligned text is the primary target; skewed/rotated layouts need further validation. Clipboard output is text, not a comparison image. Fixture overlay and model bridge tests are covered; live desktop capture plus an actual model response must also be checked in DSH.

Build with `tools/build-screen.ps1`; run Rust tests using `-Test`, and host tests using `npm test`. The script uses the workspace-local Rust GNU toolchain. `tools/package-screen.py` creates CPU/CUDA archives under `.build/screen-release-0.2.0` without publishing them.
