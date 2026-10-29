"""Tests for `io_soulstruct/platform_compat.py` (the Linux `texconv.exe` replacement).

Unlike the other tests in this folder these do NOT need Blender: they run under plain Python with the real `soulstruct`
package installed, and replace `pyrelink.core` with a recording fake, so they exercise the argument handling, format
aliasing, file naming, error reporting, and the monkey-patching of Soulstruct itself without needing the compiled
`pyrelink` extension.

Run with:  python -m unittest tests/test_linux_texconv_shim.py -v
(See `test_linux_texconv_pyrelink.py` for the companion test that exercises the real compiled `pyrelink`.)
"""
from __future__ import annotations

import enum
import importlib
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path

_COMPAT_PATH = Path(__file__).parent.parent / "io_soulstruct" / "platform_compat.py"


def _load_platform_compat() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("io_soulstruct_platform_compat_under_test", _COMPAT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeDXGIFormat(enum.IntEnum):
    R8G8B8A8_UNORM = 28
    B8G8R8A8_UNORM = 87
    BC1_UNORM = 71
    BC2_UNORM = 74
    BC3_UNORM = 77
    BC4_UNORM = 80
    BC5_UNORM = 83
    BC5_SNORM = 84
    BC7_UNORM = 98
    BC6H_UF16 = 95


class _FakeDDS:
    """Records every conversion so tests can assert on what the shim asked `pyrelink` to do."""

    calls: list[tuple] = []

    def __init__(self, data: bytes):
        self._data = bytes(data)

    @property
    def data(self) -> bytes:
        return self._data

    @staticmethod
    def from_png(data, fmt, mip_levels=1, force_dx10=False):
        _FakeDDS.calls.append(("from_png", bytes(data), fmt, mip_levels, force_dx10))
        return _FakeDDS(b"DDS:png->" + fmt.name.encode() + f":m{mip_levels}:dx10={force_dx10}".encode())

    @staticmethod
    def from_tga(data, fmt, mip_levels=1, force_dx10=False):
        _FakeDDS.calls.append(("from_tga", bytes(data), fmt, mip_levels, force_dx10))
        return _FakeDDS(b"DDS:tga->" + fmt.name.encode() + f":m{mip_levels}:dx10={force_dx10}".encode())

    def to_png(self) -> bytes:
        _FakeDDS.calls.append(("to_png", self._data))
        return b"PNG:" + self._data

    def to_tga(self) -> bytes:
        _FakeDDS.calls.append(("to_tga", self._data))
        return b"TGA:" + self._data


def _install_fake_pyrelink() -> None:
    pyrelink = types.ModuleType("pyrelink")
    core = types.ModuleType("pyrelink.core")
    core.DDS = _FakeDDS
    core.DXGIFormat = _FakeDXGIFormat
    pyrelink.core = core
    sys.modules["pyrelink"] = pyrelink
    sys.modules["pyrelink.core"] = core


class TexconvShimTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # The real `soulstruct` (needed to prove we patch it correctly). Skip cleanly where it is not installed.
        try:
            cls.texconv_module = importlib.import_module("soulstruct.base.textures.texconv")
            cls.tpf_module = importlib.import_module("soulstruct.containers.tpf")
            cls.textures_package = importlib.import_module("soulstruct.base.textures")
        except ImportError as ex:  # pragma: no cover
            raise unittest.SkipTest(f"`soulstruct` is not installed: {ex}")
        # Kept in a dict: a plain function stored as a class attribute would turn into a bound method on `self`.
        cls.originals = {
            "texconv": cls.texconv_module.texconv,
            "batch": cls.texconv_module.batch_texconv_to_dds,
        }

    def setUp(self):
        _FakeDDS.calls = []
        _install_fake_pyrelink()
        self.compat = _load_platform_compat()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _write(self, name: str, data: bytes = b"input") -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    # --- patching --------------------------------------------------------------------------------------------------

    def test_install_rebinds_every_existing_reference_and_is_idempotent(self):
        compat = self.compat
        original = self.originals["texconv"]
        self.assertFalse(getattr(original, compat._SHIM_MARKER, False), "test must start from an unpatched soulstruct")
        # Names bound at import time (`from .texconv import texconv`, `from soulstruct.base.textures import *`).
        self.assertIs(self.tpf_module.texconv, original)
        self.assertIs(self.textures_package.texconv_to_dds, self.texconv_module.texconv_to_dds)

        try:
            compat.IS_LINUX = True
            compat.install()
            self.assertIs(self.texconv_module.texconv, compat.run_texconv)
            self.assertIs(self.tpf_module.texconv, compat.run_texconv, "tpf.py bound the original by name")
            self.assertIsNot(self.texconv_module.batch_texconv_to_dds, self.originals["batch"])
            self.assertTrue(getattr(self.texconv_module.batch_texconv_to_dds, compat._SHIM_MARKER))

            # Second call must neither re-wrap nor change anything.
            before = (self.texconv_module.texconv, self.texconv_module.batch_texconv_to_dds)
            compat.install()
            self.assertEqual(before, (self.texconv_module.texconv, self.texconv_module.batch_texconv_to_dds))
        finally:
            # Restore Soulstruct so other tests (and this class's other cases) see an unpatched module.
            self.texconv_module.texconv = original
            self.texconv_module.batch_texconv_to_dds = self.originals["batch"]
            self.tpf_module.texconv = original
            self.textures_package.batch_texconv_to_dds = self.originals["batch"]

    def test_install_is_a_noop_off_linux(self):
        self.compat.IS_LINUX = False
        self.compat.install()
        self.assertIs(self.texconv_module.texconv, self.originals["texconv"])

    # --- DDS -> PNG / TGA ------------------------------------------------------------------------------------------

    def test_dds_to_png_uses_input_stem_and_output_dir(self):
        src = self._write("tex_a.dds", b"ddsbytes")
        out_dir = self.dir / "out"
        result = self.compat.run_texconv("-o", out_dir, "-ft", "png", "-f", "RGBA", "-nologo", src)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((out_dir / "tex_a.png").read_bytes(), b"PNG:ddsbytes")
        self.assertEqual(_FakeDDS.calls, [("to_png", b"ddsbytes")])

    def test_dds_to_tga(self):
        src = self._write("tex_b.dds", b"x")
        result = self.compat.run_texconv("-o", str(self.dir), "-ft", "tga", "-f", "RGBA", "-nologo", src)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((self.dir / "tex_b.tga").read_bytes(), b"TGA:x")

    # --- PNG / TGA -> DDS ------------------------------------------------------------------------------------------

    def test_png_to_dds_defaults_to_full_mip_chain(self):
        src = self._write("n.png", b"pngdata")
        result = self.compat.run_texconv("-f", "BC7_UNORM", "-o", self.dir / "o", "-nologo", "-y", src)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(_FakeDDS.calls, [("from_png", b"pngdata", _FakeDXGIFormat.BC7_UNORM, 0, False)])
        self.assertTrue((self.dir / "o" / "n.dds").is_file())

    def test_explicit_mips_and_dx10_match_soulstruct_argument_layout(self):
        # `texconv_to_dds` inserts "-dx10" between "-ft dds" and "-f": make sure that exact layout is understood.
        src = self._write("t.tga", b"tgadata")
        args = ["-o", str(self.dir), "-ft", "dds", "-dx10", "-f", "BC1_UNORM", "-m", "1", "-nologo", str(src)]
        result = self.compat.run_texconv(*args)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(_FakeDDS.calls, [("from_tga", b"tgadata", _FakeDXGIFormat.BC1_UNORM, 1, True)])

    def test_legacy_fourcc_aliases(self):
        cases = {"DXT1": "BC1_UNORM", "dxt3": "BC2_UNORM", "DXT5": "BC3_UNORM", "ATI1": "BC4_UNORM",
                 "ATI2": "BC5_UNORM", "BC5S": "BC5_SNORM", "RGBA": "R8G8B8A8_UNORM", "BC6H": "BC6H_UF16"}
        for alias, expected in cases.items():
            with self.subTest(alias=alias):
                _FakeDDS.calls = []
                src = self._write("a.png")
                result = self.compat.run_texconv("-f", alias, "-o", self.dir, src)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(_FakeDDS.calls[0][2].name, expected)

    # --- failures are reported like a failing process, never raised ------------------------------------------------

    def test_failures_return_nonzero_with_message(self):
        src = self._write("f.png")
        cases = {
            "typeless": ["-f", "BC7_TYPELESS", "-o", self.dir, src],
            "unknown format": ["-f", "NOT_A_FORMAT", "-o", self.dir, src],
            "unsupported option": ["-srgb", "-o", self.dir, src],
            "missing value": ["-o"],
            "no inputs": ["-nologo"],
            "missing file": ["-o", self.dir, self.dir / "nope.png"],
            "png to tga": ["-ft", "tga", "-o", self.dir, src],
            "bad extension": ["-o", self.dir, self._write("w.jpg")],
        }
        for label, args in cases.items():
            with self.subTest(label):
                result = self.compat.run_texconv(*args)
                self.assertEqual(result.returncode, 1, label)
                self.assertIn(b"FAILED", result.stdout)

    def test_missing_pyrelink_is_reported_not_raised(self):
        sys.modules["pyrelink.core"] = None  # makes `import pyrelink.core` raise ImportError
        result = self.compat.run_texconv("-o", self.dir, self._write("p.png"))
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"pyrelink", result.stdout)

    # --- Soulstruct's own helpers work end to end through the shim -------------------------------------------------

    def test_soulstruct_texconv_to_dds_and_batch_work_with_the_shim(self):
        compat = self.compat
        original, original_batch = self.originals["texconv"], self.originals["batch"]
        try:
            compat.IS_LINUX = True
            compat.install()
            TexconvConfig = self.texconv_module.TexconvConfig
            configs = []
            for i in range(5):
                src = self._write(f"img{i}.png", f"png{i}".encode())
                configs.append(TexconvConfig(str(self.dir / "dds"), "BC3_UNORM", False, 0, src))

            single = self.texconv_module.texconv_to_dds(configs[0])
            self.assertEqual(single, b"DDS:png->BC3_UNORM:m0:dx10=False")

            batch = self.texconv_module.batch_texconv_to_dds(configs)
            self.assertEqual(len(batch), 5)
            self.assertTrue(all(item == single for item in batch), "batch results must come back in order, intact")

            bad = TexconvConfig(str(self.dir), "BC7_TYPELESS", False, 0, self.dir / "img0.png")
            with self.assertRaises(self.texconv_module.TexconvError):
                self.texconv_module.batch_texconv_to_dds([bad])
            self.assertEqual(self.texconv_module.batch_texconv_to_dds([]), [])
        finally:
            self.texconv_module.texconv = original
            self.texconv_module.batch_texconv_to_dds = original_batch
            self.tpf_module.texconv = original
            self.textures_package.batch_texconv_to_dds = original_batch


class SteamLocationTests(unittest.TestCase):

    def test_linux_locations_are_absolute_and_include_flatpak(self):
        compat = _load_platform_compat()
        compat.IS_LINUX = True
        locations = compat.steam_common_locations()
        self.assertTrue(locations)
        self.assertTrue(all(p.is_absolute() for p in locations))
        self.assertTrue(any("com.valvesoftware.Steam" in str(p) for p in locations))

    def test_windows_locations_unchanged(self):
        compat = _load_platform_compat()
        compat.IS_LINUX = False
        locations = compat.steam_common_locations()
        self.assertEqual(len(locations), 12)
        self.assertEqual(str(locations[0]).replace("\\", "/"), "C:/Program Files (x86)/Steam/steamapps/common")


if __name__ == "__main__":
    unittest.main()
