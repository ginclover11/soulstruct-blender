"""Platform compatibility layer (Linux).

Soulstruct (the Python library) was written for Windows and shells out to Windows executables bundled in its wheel. The
only one the Blender extension depends on is ``texconv.exe`` (Microsoft's DirectXTex command-line tool), used to
convert textures between DDS and PNG/TGA, in both directions, when importing and exporting FLVER textures.

``texconv`` cannot be built for Linux (DirectXTex only builds its tools on Windows), but ``pyrelink`` (our C++ binding
layer) bundles the DirectXTex *library*, which can. This module installs a drop-in replacement for
``soulstruct.base.textures.texconv.texconv`` that understands the subset of the ``texconv`` command line that Soulstruct
and this extension actually use, and runs it through ``pyrelink.core.DDS`` instead.

On Windows this module does nothing. It must not import ``bpy`` so it can be unit-tested outside Blender.

Call :func:`install` as early as possible (before the rest of the extension is imported). It is idempotent.
"""
from __future__ import annotations

__all__ = [
    "IS_LINUX",
    "install",
    "steam_common_locations",
    "TexconvShimError",
    "run_texconv",
]

import importlib
import logging
import os
import subprocess
import sys
import typing as tp
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_LOGGER = logging.getLogger("soulstruct.blender.platform_compat")

IS_LINUX = sys.platform.startswith("linux")

_SHIM_MARKER = "_soulstruct_blender_linux_shim"


# ---------------------------------------------------------------------------------------------------------------------
# Steam locations
# ---------------------------------------------------------------------------------------------------------------------

def steam_common_locations() -> list[Path]:
    """Return candidate Steam 'steamapps/common' directories for the current platform, most likely first."""
    if IS_LINUX:
        home = Path.home()
        # Steam Deck SD card / typical removable-media library mounts.
        media_root = Path("/run/media")
        media_libraries = sorted(media_root.glob("*/*/steamapps/common")) if media_root.is_dir() else []
        return [
            home / ".local/share/Steam/steamapps/common",
            home / ".steam/steam/steamapps/common",
            home / ".steam/root/steamapps/common",
            # Flatpak Steam
            home / ".var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/common",
            *media_libraries,
        ]
    locations = [Path(f"{drive}:/Program Files (x86)/Steam/steamapps/common") for drive in "CDEFGH"]
    locations += [Path(f"{drive}:/Steam/steamapps/common") for drive in "CDEFGH"]
    return locations


# ---------------------------------------------------------------------------------------------------------------------
# `texconv` replacement
# ---------------------------------------------------------------------------------------------------------------------

class TexconvShimError(Exception):
    """Raised for a texconv invocation the shim cannot (or will not) perform. Reported as a failed process."""


# Legacy FourCC names and common shorthands that `texconv -f` accepts, mapped to DXGI format names.
# `soulstruct`'s `DDS.texconv_format` returns the FourCC string for legacy (non-DX10) headers.
_FORMAT_ALIASES = {
    "DXT1": "BC1_UNORM",
    "DXT2": "BC2_UNORM",
    "DXT3": "BC2_UNORM",
    "DXT4": "BC3_UNORM",
    "DXT5": "BC3_UNORM",
    "ATI1": "BC4_UNORM",
    "BC4U": "BC4_UNORM",
    "BC4S": "BC4_SNORM",
    "ATI2": "BC5_UNORM",
    "BC5U": "BC5_UNORM",
    "BC5S": "BC5_SNORM",
    "RGBA": "R8G8B8A8_UNORM",
    "BGRA": "B8G8R8A8_UNORM",
    "BGRX": "B8G8R8X8_UNORM",
    "BC1": "BC1_UNORM",
    "BC2": "BC2_UNORM",
    "BC3": "BC3_UNORM",
    "BC4": "BC4_UNORM",
    "BC5": "BC5_UNORM",
    "BC6H": "BC6H_UF16",
    "BC7": "BC7_UNORM",
}

_IMAGE_SUFFIXES = {".png": "png", ".tga": "tga", ".dds": "dds"}


class _TexconvArgs(tp.NamedTuple):
    inputs: list[Path]
    output_dir: Path | None
    file_type: str  # 'dds', 'png', or 'tga'
    dxgi_format: str | None
    mip_levels: int | None  # `None` means texconv's default (full chain when writing DDS)
    force_dx10: bool


def _parse_texconv_args(args: tp.Sequence[tp.Any]) -> _TexconvArgs:
    """Parse the subset of the texconv command line used by Soulstruct and this extension.

    Handled: ``-o <dir>``, ``-ft <dds|png|tga>``, ``-f <format>``, ``-m <levels>``, ``-dx10``, and the no-op flags
    ``-nologo`` and ``-y``. Anything else is an error rather than being silently ignored, so unsupported use is noticed.
    """
    inputs: list[Path] = []
    output_dir: Path | None = None
    file_type = "dds"
    dxgi_format: str | None = None
    mip_levels: int | None = None
    force_dx10 = False

    items = [str(a) if not isinstance(a, os.PathLike) else os.fspath(a) for a in args]
    i = 0
    while i < len(items):
        item = items[i]
        i += 1
        if not item.startswith("-") or len(item) == 1:
            inputs.append(Path(item))
            continue

        option = item[1:].lower()
        if option in ("nologo", "y"):
            continue
        if option == "dx10":
            force_dx10 = True
            continue
        if option in ("o", "ft", "f", "m"):
            if i >= len(items):
                raise TexconvShimError(f"Option '{item}' requires a value.")
            value = items[i]
            i += 1
            if option == "o":
                output_dir = Path(value)
            elif option == "ft":
                file_type = value.lower()
                if file_type not in ("dds", "png", "tga"):
                    raise TexconvShimError(f"Unsupported output file type '{value}' (supported: dds, png, tga).")
            elif option == "f":
                dxgi_format = value
            else:  # "m"
                try:
                    mip_levels = int(value)
                except ValueError:
                    raise TexconvShimError(f"Invalid mip level count '{value}'.") from None
                if mip_levels < 0:
                    raise TexconvShimError(f"Invalid mip level count '{value}'.")
            continue
        raise TexconvShimError(f"Unsupported texconv option '{item}'.")

    if not inputs:
        raise TexconvShimError("No input files given.")
    return _TexconvArgs(inputs, output_dir, file_type, dxgi_format, mip_levels, force_dx10)


def _resolve_format(pyre_core, name: str):
    """Resolve a ``texconv -f`` name (DXGI name, FourCC alias, or shorthand) to a ``pyrelink`` ``DXGIFormat``."""
    key = name.strip().upper()
    if "TYPELESS" in key:
        raise TexconvShimError(f"DDS format '{name}' is not supported (TYPELESS formats cannot be converted).")
    key = _FORMAT_ALIASES.get(key, key)
    try:
        return getattr(pyre_core.DXGIFormat, key)
    except AttributeError:
        raise TexconvShimError(f"Texture format '{name}' is not supported on this platform.") from None


def _convert_one(pyre_core, path: Path, parsed: _TexconvArgs, log: list[str]) -> None:
    source_type = _IMAGE_SUFFIXES.get(path.suffix.lower())
    if source_type is None:
        raise TexconvShimError(f"Unsupported input file type: '{path.name}'.")
    if not path.is_file():
        raise TexconvShimError(f"Input file not found: '{path}'.")

    out_dir = parsed.output_dir if parsed.output_dir is not None else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{path.stem}.{parsed.file_type}"
    data = path.read_bytes()

    if source_type == "dds":
        if parsed.file_type == "dds":
            raise TexconvShimError("DDS-to-DDS conversion is not supported on this platform.")
        dds = pyre_core.DDS(data)
        # Both writers decompress block formats and write the base mip level as 8-bit RGBA; `-f` is irrelevant here.
        out_path.write_bytes(dds.to_png() if parsed.file_type == "png" else dds.to_tga())
    else:
        if parsed.file_type != "dds":
            raise TexconvShimError(f"Converting {source_type.upper()} to {parsed.file_type.upper()} is not supported.")
        dxgi = _resolve_format(
            pyre_core, parsed.dxgi_format or ("R8G8B8A8_UNORM" if source_type == "png" else "B8G8R8A8_UNORM")
        )
        # texconv generates a full mip chain when writing DDS unless `-m` says otherwise.
        mip_levels = 0 if parsed.mip_levels is None else parsed.mip_levels
        convert = pyre_core.DDS.from_png if source_type == "png" else pyre_core.DDS.from_tga
        dds = convert(data, dxgi, mip_levels=mip_levels, force_dx10=parsed.force_dx10)
        out_path.write_bytes(dds.data)

    log.append(f"writing {out_path}")


def run_texconv(*args: tp.Any) -> subprocess.CompletedProcess:
    """Drop-in for ``soulstruct.base.textures.texconv.texconv`` that does not need ``texconv.exe``.

    Returns a ``subprocess.CompletedProcess`` (``stdout`` as bytes, ``returncode`` 0 on success, 1 on failure) so that
    existing callers, which only inspect ``stdout``, work unchanged.
    """
    log: list[str] = []
    try:
        try:
            import pyrelink.core as pyre_core
        except ImportError as ex:
            raise TexconvShimError(f"`pyrelink` is not available, so textures cannot be converted: {ex}") from None
        parsed = _parse_texconv_args(args)
        for path in parsed.inputs:
            _convert_one(pyre_core, path, parsed, log)
    except TexconvShimError as ex:
        log.append(f"FAILED: {ex}")
        return subprocess.CompletedProcess(["texconv"] + [str(a) for a in args], 1, "\n".join(log).encode(), b"")
    except Exception as ex:  # noqa: BLE001 - mirror a failing external process: report, never raise into Blender's UI
        log.append(f"FAILED: {type(ex).__name__}: {ex}")
        return subprocess.CompletedProcess(["texconv"] + [str(a) for a in args], 1, "\n".join(log).encode(), b"")
    return subprocess.CompletedProcess(["texconv"] + [str(a) for a in args], 0, "\n".join(log).encode(), b"")


def _make_batch_texconv_to_dds(textures_module):
    """Replace `batch_texconv_to_dds`, which forks a `multiprocessing.Pool`.

    Forking the whole Blender process (threads, OpenGL context, ...) is hazardous, and `pyrelink` releases the GIL while
    it converts, so a thread pool gives the same parallelism safely.
    """

    def batch_texconv_to_dds(configs):
        for config in configs:
            if "TYPELESS" in config.dds_format:
                raise textures_module.TexconvError(
                    f"DDS format '{config.dds_format}' is not supported by `texconv`. "
                    f"(Try UNORM instead of TYPELESS.)"
                )
        if not configs:
            return []
        workers = max(1, min(len(configs), os.cpu_count() or 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(textures_module.texconv_to_dds, configs))

    batch_texconv_to_dds.__doc__ = "Thread-pool replacement for the multiprocessing-based original (Linux)."
    return batch_texconv_to_dds


def _install_texconv_shim() -> bool:
    """Replace `texconv`/`batch_texconv_to_dds` in `soulstruct` and in every module that already imported them."""
    try:
        # NOTE: do NOT use `import soulstruct.base.textures.texconv as texconv_module`. The `soulstruct.base.textures`
        # package does `from .texconv import *`, which rebinds its `texconv` attribute to the *function*, so that
        # statement would hand us the function instead of the module. `import_module` returns the real module.
        texconv_module = importlib.import_module("soulstruct.base.textures.texconv")
    except ImportError:
        _LOGGER.warning("Could not import `soulstruct.base.textures.texconv`; texture conversion shim not installed.")
        return False

    original_texconv = texconv_module.texconv
    if getattr(original_texconv, _SHIM_MARKER, False):
        replacements: dict[int, tp.Any] = {}
    else:
        original_batch = texconv_module.batch_texconv_to_dds
        shim_batch = _make_batch_texconv_to_dds(texconv_module)
        setattr(run_texconv, _SHIM_MARKER, True)
        setattr(shim_batch, _SHIM_MARKER, True)
        # Keep references so the scan below can recognise the originals wherever they were imported by name.
        replacements = {id(original_texconv): run_texconv, id(original_batch): shim_batch}
        texconv_module.texconv = run_texconv
        texconv_module.batch_texconv_to_dds = shim_batch

    # Patch modules that did `from soulstruct.base.textures.texconv import texconv` (or `import *`) before we ran.
    shim_names = ("texconv", "batch_texconv_to_dds")
    shim_by_name = {"texconv": run_texconv, "batch_texconv_to_dds": texconv_module.batch_texconv_to_dds}
    patched = 0
    for module in list(sys.modules.values()):
        if module is None or module is texconv_module:
            continue
        module_dict = getattr(module, "__dict__", None)
        if not isinstance(module_dict, dict):
            continue
        for name in shim_names:
            current = module_dict.get(name)
            if current is not None and not getattr(current, _SHIM_MARKER, False) and (
                id(current) in replacements
            ):
                module_dict[name] = shim_by_name[name]
                patched += 1
    _LOGGER.info(f"Installed Linux texture conversion shim (re-bound {patched} existing reference(s)).")
    return True


def install() -> None:
    """Install all platform shims. Safe to call repeatedly; does nothing on Windows."""
    if not IS_LINUX:
        return
    _install_texconv_shim()
