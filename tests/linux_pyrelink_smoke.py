"""Smoke test for the Linux build of `pyrelink`. Needs NO game files and NO Blender.

    python tests/linux_pyrelink_smoke.py

It exercises the pieces of the Linux port that no unit test can cover without the real compiled library: DDS <-> PNG/TGA
conversion in several formats, mipmap generation, the DX10 header option, and the `texconv` replacement
(`io_soulstruct/platform_compat.py`) end to end on real files. Run it with the Python that has the freshly built
`pyrelink` wheel installed (e.g. `.build-venv/bin/python`).

Exit code 0 = everything passed. Each check prints PASS/FAIL so a failure shows exactly what broke.
"""
from __future__ import annotations

import importlib.util
import struct
import sys
import tempfile
import time
import traceback
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------------------------------------------------
# Tiny PNG encoder/decoder (8-bit RGB/RGBA, non-interlaced) so the test depends on nothing but the standard library.
# ---------------------------------------------------------------------------------------------------------------------


def make_png(width: int, height: int, rgba: bytes) -> bytes:
    assert len(rgba) == width * height * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter: None
        raw += rgba[y * width * 4:(y + 1) * width * 4]

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")


def decode_png(data: bytes) -> tuple[int, int, bytes]:
    """Return (width, height, RGBA bytes). Handles all five PNG row filters."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos = 8
    idat = bytearray()
    width = height = color_type = bit_depth = interlace = None
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", payload)
        elif tag == b"IDAT":
            idat += payload
        elif tag == b"IEND":
            break
    assert bit_depth == 8 and interlace == 0 and color_type in (2, 6), \
        f"unsupported PNG (depth={bit_depth}, color_type={color_type}, interlace={interlace})"
    bpp = 4 if color_type == 6 else 3
    stride = width * bpp
    raw = zlib.decompress(bytes(idat))
    out = bytearray()
    prev = bytearray(stride)
    for y in range(height):
        ft = raw[y * (stride + 1)]
        row = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = row[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if ft == 1:
                row[i] = (row[i] + a) & 255
            elif ft == 2:
                row[i] = (row[i] + b) & 255
            elif ft == 3:
                row[i] = (row[i] + ((a + b) >> 1)) & 255
            elif ft == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[i] = (row[i] + pred) & 255
        out += row
        prev = row
    if bpp == 3:
        rgba = bytearray()
        for i in range(0, len(out), 3):
            rgba += out[i:i + 3] + b"\xff"
        return width, height, bytes(rgba)
    return width, height, bytes(out)


def test_image(size: int = 128) -> bytes:
    """Smooth gradients plus a checker patch: representative enough for block compression, deterministic."""
    px = bytearray()
    for y in range(size):
        for x in range(size):
            r = x * 255 // (size - 1)
            g = y * 255 // (size - 1)
            b = 255 - (r + g) // 2
            if 32 <= x < 64 and 32 <= y < 64 and ((x // 4 + y // 4) % 2):
                r, g, b = 255, 255, 255
            px += bytes((r, g, b, 255))
    return bytes(px)


def mean_abs_error(a: bytes, b: bytes) -> float:
    assert len(a) == len(b)
    total = 0
    n = 0
    for i in range(0, len(a), 4):  # RGB only; alpha is constant 255 here
        total += abs(a[i] - b[i]) + abs(a[i + 1] - b[i + 1]) + abs(a[i + 2] - b[i + 2])
        n += 3
    return total / n


# ---------------------------------------------------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------------------------------------------------

RESULTS: list[tuple[str, bool, str]] = []


def check(name):
    def deco(fn):
        t0 = time.perf_counter()
        try:
            detail = fn() or ""
            RESULTS.append((name, True, f"{detail} ({time.perf_counter() - t0:.2f}s)".strip()))
        except Exception as ex:  # noqa: BLE001
            RESULTS.append((name, False, f"{type(ex).__name__}: {ex}\n{traceback.format_exc()}"))
        print(f"{'PASS' if RESULTS[-1][1] else 'FAIL'}  {name}  {RESULTS[-1][2] if RESULTS[-1][1] else ''}")
        if not RESULTS[-1][1]:
            print(RESULTS[-1][2])
        return fn
    return deco


try:
    import pyrelink.core as pyre_core
    from pyrelink.core import DDS, DXGIFormat
except Exception as ex:  # noqa: BLE001
    print(f"FATAL: cannot import pyrelink.core: {type(ex).__name__}: {ex}")
    sys.exit(2)

SIZE = 128
SRC_RGBA = test_image(SIZE)
SRC_PNG = make_png(SIZE, SIZE, SRC_RGBA)


def dds_header(dds) -> dict:
    d = dds.data
    assert d[:4] == b"DDS ", f"bad magic {d[:4]!r}"
    height, width = struct.unpack("<II", d[12:20])
    mips = struct.unpack("<I", d[28:32])[0]
    return {"w": width, "h": height, "mips": max(mips, 1), "fourcc": d[84:88]}


for fmt_name, max_err in [
    ("R8G8B8A8_UNORM", 0.01),
    ("B8G8R8A8_UNORM", 0.01),
    ("BC1_UNORM", 10.0),
    ("BC3_UNORM", 10.0),
    ("BC7_UNORM", 5.0),
]:
    @check(f"PNG -> {fmt_name} -> PNG round trip (mean error <= {max_err})")
    def _roundtrip(fmt_name=fmt_name, max_err=max_err):
        fmt = getattr(DXGIFormat, fmt_name)
        dds = DDS.from_png(SRC_PNG, fmt)
        hdr = dds_header(dds)
        assert (hdr["w"], hdr["h"]) == (SIZE, SIZE), hdr
        w, h, rgba = decode_png(dds.to_png())
        assert (w, h) == (SIZE, SIZE), (w, h)
        err = mean_abs_error(SRC_RGBA, rgba)
        assert err <= max_err, f"mean abs error {err:.3f} > {max_err}"
        return f"error={err:.3f}, {dds.size} bytes"


@check("mip_levels=0 generates the full chain (128px -> 8 mips)")
def _mips_full():
    hdr = dds_header(DDS.from_png(SRC_PNG, DXGIFormat.BC1_UNORM, mip_levels=0))
    assert hdr["mips"] == 8, hdr
    return f"mips={hdr['mips']}"


@check("mip_levels=3 generates exactly 3 mips")
def _mips_three():
    hdr = dds_header(DDS.from_png(SRC_PNG, DXGIFormat.BC3_UNORM, mip_levels=3))
    assert hdr["mips"] == 3, hdr


@check("mip_levels=1 (default) writes a single level")
def _mips_one():
    assert dds_header(DDS.from_png(SRC_PNG, DXGIFormat.BC1_UNORM))["mips"] == 1


@check("full mip chain survives decoding back to PNG (base level)")
def _mips_decode():
    w, h, rgba = decode_png(DDS.from_png(SRC_PNG, DXGIFormat.BC1_UNORM, mip_levels=0).to_png())
    assert (w, h) == (SIZE, SIZE)
    assert mean_abs_error(SRC_RGBA, rgba) <= 10.0


@check("force_dx10=True writes a DX10 extended header")
def _dx10():
    plain = dds_header(DDS.from_png(SRC_PNG, DXGIFormat.BC7_UNORM))
    forced = dds_header(DDS.from_png(SRC_PNG, DXGIFormat.BC1_UNORM, force_dx10=True))
    assert forced["fourcc"] == b"DX10", forced
    assert plain["fourcc"] == b"DX10", "BC7 has no legacy FourCC, so it must use the DX10 header"


@check("BC1 without force_dx10 uses the legacy DXT1 header")
def _legacy():
    assert dds_header(DDS.from_png(SRC_PNG, DXGIFormat.BC1_UNORM))["fourcc"] == b"DXT1"


@check("DDS -> TGA -> DDS round trip")
def _tga():
    tga = DDS.from_png(SRC_PNG, DXGIFormat.R8G8B8A8_UNORM).to_tga()
    assert len(tga) > 18 + SIZE * SIZE * 3, len(tga)
    dds = DDS.from_tga(tga, DXGIFormat.BC3_UNORM)
    w, h, rgba = decode_png(dds.to_png())
    assert (w, h) == (SIZE, SIZE)
    err = mean_abs_error(SRC_RGBA, rgba)
    assert err <= 10.0, err
    return f"error={err:.3f}"


@check("DDS(data) re-wraps raw bytes")
def _wrap():
    dds = DDS.from_png(SRC_PNG, DXGIFormat.BC1_UNORM)
    again = DDS(dds.data)
    assert again.data == dds.data and not again.is_empty


@check("invalid input raises instead of crashing the process")
def _bad_input():
    for bad in (b"", b"not an image at all", SRC_PNG[:40]):
        try:
            DDS.from_png(bad, DXGIFormat.BC1_UNORM)
        except Exception:
            continue
        raise AssertionError(f"expected an exception for {len(bad)}-byte garbage input")
    try:
        DDS(b"DDS garbage").to_png()
    except Exception:
        return
    raise AssertionError("expected an exception for a corrupt DDS")


@check("BC7 compression time for 256x256 (informational; no OpenMP, single thread)")
def _bc7_speed():
    png = make_png(256, 256, test_image(256))
    t0 = time.perf_counter()
    DDS.from_png(png, DXGIFormat.BC7_UNORM, mip_levels=0)
    dt = time.perf_counter() - t0
    return f"{dt:.2f}s"


# ---- the texconv replacement used by Soulstruct, on real files -------------------------------------------------------

def _load_platform_compat():
    spec = importlib.util.spec_from_file_location("platform_compat_under_test", REPO_ROOT / "io_soulstruct" / "platform_compat.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    compat = _load_platform_compat()
except Exception as ex:  # noqa: BLE001
    compat = None
    print(f"FAIL  cannot load platform_compat.py: {type(ex).__name__}: {ex}")
    RESULTS.append(("load platform_compat", False, str(ex)))

if compat is not None:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        src_png = tmp / "tex.png"
        src_png.write_bytes(SRC_PNG)

        @check("run_texconv: PNG -> DDS (BC3, full mips, -dx10)")
        def _tc_to_dds():
            out = tmp / "out_dds"
            out.mkdir()
            r = compat.run_texconv("-f", "BC3_UNORM", "-m", "0", "-dx10", "-o", str(out), "-nologo", "-y", str(src_png))
            assert r.returncode == 0, r.stdout.decode(errors="replace")
            dds_path = out / "tex.dds"
            assert dds_path.is_file(), list(out.iterdir())
            hdr = dds_header(DDS(dds_path.read_bytes()))
            assert hdr["fourcc"] == b"DX10" and hdr["mips"] == 8, hdr
            return f"mips={hdr['mips']}"

        @check("run_texconv: DDS -> PNG")
        def _tc_to_png():
            out = tmp / "out_png"
            out.mkdir()
            r = compat.run_texconv("-o", str(out), "-ft", "png", "-f", "RGBA", "-nologo", "-y", str(tmp / "out_dds" / "tex.dds"))
            assert r.returncode == 0, r.stdout.decode(errors="replace")
            w, h, rgba = decode_png((out / "tex.png").read_bytes())
            assert (w, h) == (SIZE, SIZE)
            err = mean_abs_error(SRC_RGBA, rgba)
            assert err <= 10.0, err
            return f"error={err:.3f}"

        @check("run_texconv: unsupported format reports failure (rc=1), does not raise")
        def _tc_bad():
            r = compat.run_texconv("-f", "NOT_A_FORMAT", "-o", str(tmp), str(src_png))
            assert r.returncode == 1, r.returncode

        @check("run_texconv: missing input reports failure (rc=1), does not raise")
        def _tc_missing():
            r = compat.run_texconv("-f", "BC1_UNORM", "-o", str(tmp), str(tmp / "nope.png"))
            assert r.returncode == 1, r.returncode

        # ---- the real soulstruct package, if installed ----
        try:
            import soulstruct.base.textures.texconv  # noqa: F401
            have_soulstruct = True
        except Exception:  # noqa: BLE001
            have_soulstruct = False

        if have_soulstruct:
            @check("soulstruct integration: shim installed over the real texconv()")
            def _ss():
                import importlib as _il
                compat.install()  # installs the shim on Linux; no-op elsewhere
                module = _il.import_module("soulstruct.base.textures.texconv")
                if compat.IS_LINUX:
                    assert getattr(module.texconv, "_soulstruct_blender_linux_shim", False), "shim not installed"
                out = tmp / "ss_out"
                out.mkdir()
                r = module.texconv("-f", "BC1_UNORM", "-o", str(out), "-nologo", "-y", str(src_png))
                assert r.returncode == 0, r.stdout
                assert (out / "tex.dds").is_file()

failed = [r for r in RESULTS if not r[1]]
print()
print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
sys.exit(1 if failed else 0)
