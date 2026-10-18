"""ORT binaries are edition-specific and are supplied by pyinstaller_build.py.

The upstream hook always collects every provider plugin, which would put the
CUDA and TensorRT DLLs back into the CPU-only standard edition.
"""

binaries = []
