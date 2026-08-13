"""Windows artifacts stay GUI-subsystem builds that can speak, without a companion terminal."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest import mock


def test_windows_builds_use_the_windowed_subsystem():
    root = Path(__file__).resolve().parents[1]
    build_script = (root / "build_windows.bat").read_text(encoding="utf-8")
    spec = (root / "genericMud.spec").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/build-windows.yml").read_text(encoding="utf-8").lower()

    # build_windows.bat freezes from the spec, so its GUI-subsystem choice lives there as
    # console=False; the workflow still spells the flags out itself.
    assert "genericMud.spec" in build_script
    assert "console=False" in spec
    assert "--windowed" in workflow
    assert "--console" not in workflow


def _load_prism_hook():
    """Execute hooks/hook-prism.py the way PyInstaller would and return the module.

    ``collect_dynamic_libs`` is stubbed, so this asserts what the hook *asks for* without
    needing prism installed. The end-to-end check -- freeze, then look for _prism_cffi in the
    bundle -- needs minutes and a full GUI stack, so it belongs in CI, not here.
    """
    root = Path(__file__).resolve().parents[1]
    hooks = types.ModuleType("PyInstaller.utils.hooks")
    requested: dict[str, object] = {}

    def _collect_dynamic_libs(package, *, search_patterns=None, **_kw):
        requested["package"] = package
        requested["search_patterns"] = search_patterns
        return [("/fake/prism/_native/_prism_cffi.pyd", "prism/_native")]

    hooks.collect_dynamic_libs = _collect_dynamic_libs
    module = types.ModuleType("hook_prism")
    source = (root / "hooks/hook-prism.py").read_text(encoding="utf-8")
    fake_pyinstaller = {
        "PyInstaller": types.ModuleType("PyInstaller"),
        "PyInstaller.utils": types.ModuleType("PyInstaller.utils"),
        "PyInstaller.utils.hooks": hooks,
    }
    with mock.patch.dict(sys.modules, fake_pyinstaller):
        exec(compile(source, "hook-prism.py", "exec"), module.__dict__)  # noqa: S102
    return module, requested


def test_the_prism_hook_collects_the_cffi_bridge_and_its_backend():
    # Asserting on the hook's source text would pass a hook whose collect result was never
    # bound to `binaries`, which is the exact shape of a silent no-speech release.
    module, requested = _load_prism_hook()

    assert requested["package"] == "prism"
    # The two PyInstaller's own defaults (*.dll, *.dylib, lib*.so) leave out.
    assert "*.pyd" in requested["search_patterns"]
    assert "*.so" in requested["search_patterns"]
    assert module.binaries, "the collected libraries must reach PyInstaller as `binaries`"
    assert all(dest.startswith("prism") for _src, dest in module.binaries)
    # _cffi_backend is loaded from C, so no module graph will ever find it on its own.
    assert "_cffi_backend" in module.hiddenimports


def test_every_windows_freeze_path_registers_the_prism_hook():
    # build_windows.bat goes through the spec, the workflow spells its flags out; each has to
    # register the hook on its own, so they are the pair that can drift.
    root = Path(__file__).resolve().parents[1]
    spec = (root / "genericMud.spec").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/build-windows.yml").read_text(encoding="utf-8")

    assert "hookspath" in spec and "hooks" in spec
    assert "--additional-hooks-dir hooks" in workflow
    assert "--collect-all prism" in workflow
