"""PyInstaller hook for prism (PyPI ``prismatoid``).

prism keeps its cffi bridge and native library in ``prism/_native/``, a plain directory that
``prism/_native.py`` splices onto ``prism.__path__`` at import time. Nothing PyInstaller does
by default reaches the bridge: the module graph is static, so it never sees a module under a
path added at runtime; ``collect_data_files`` filters extensions out; and
``collect_dynamic_libs`` defaults to ``*.dll``/``*.dylib``/``lib*.so``, which
``_prism_cffi.pyd`` and ``_prism_cffi.abi3.so`` both miss.

Without this the frozen app raises ``ModuleNotFoundError: prism._prism_cffi`` on
``import prism``, voice/factory.py swallows it, and self-voice drops to the system TTS
instead of the user's own screen reader with nothing in the logs. Registered by
genericMud.spec and by tools/build_app.py, so every freeze path gets it.
"""

from PyInstaller.utils.hooks import collect_dynamic_libs

# _cffi_backend is loaded from C when the bridge initialises, so no Python source imports it
# and the module graph misses it the same way.
hiddenimports = ["_cffi_backend"]

binaries = collect_dynamic_libs(
    "prism", search_patterns=["*.dll", "*.dylib", "*.so", "*.pyd"]
)
