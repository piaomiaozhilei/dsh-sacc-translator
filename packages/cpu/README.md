# SACC Translator CPU

Windows x64, DeepSeek Harness 0.2.1-rc.2.

This small npm package automatically installs its pinned document runtime from GitHub Releases. The CUDA edition also installs its pinned GPU component. No postinstall scripts are required. CPU/CUDA share settings and translation history; enable only one edition.

CPU downloads about 364 MB in total; CUDA about 2.04 GB including CUDA/cuDNN. A short versioned runtime directory under LOCALAPPDATA avoids Windows DLL path limits, using hard links where possible.

Source, installation details and validation: https://github.com/piaomiaozhilei/dsh-sacc-translator

Version 0.1.19 defaults to 5 concurrent translation requests; saved preferences take precedence. Bundle overrides support PDF/general file reads up to 1 GiB, Excel up to 256 MiB/1,000,000 cells/120 seconds, and Office inputs up to 512 MiB with PDF output up to 1 GiB and a 300-second conversion timeout. Large previews still load full files and need sufficient memory. Profile overrides take precedence.
