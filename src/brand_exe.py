"""Build a branded launcher exe so tray icons are attributed to us, not Python.

Windows Settings > Taskbar > Other system tray icons shows the OWNING
EXECUTABLE's icon and file description - any tray app run through pythonw.exe
is listed as "Python" with the Python logo. This script creates
.venv/Scripts/voice-of-claude.exe:

  1. a copy of the BASE pythonw.exe (the venv's own pythonw.exe is only a
     launcher shim that re-spawns the base interpreter, which would own the
     tray icon - so the shim must be bypassed, not copied);
  2. the Python runtime DLLs copied next to it (a base interpreter outside
     its install dir cannot find them otherwise);
  3. its icon resources replaced with the speaker icon from tray.py;
  4. its version-info resource removed, so the display name falls back to
     the file name ("voice-of-claude") instead of "Python".

Sitting in Scripts/, it picks up pyvenv.cfg and runs inside the venv exactly
like pythonw.exe would. Run once: .venv/Scripts/python.exe src/brand_exe.py
(setup.ps1 does this). launch_server.py prefers it for the tray when present.
"""
from __future__ import annotations

import ctypes
import shutil
import struct
import sys
from ctypes import wintypes
from pathlib import Path

EXE_NAME = "voice-of-claude.exe"
RT_ICON, RT_GROUP_ICON, RT_VERSION = 3, 14, 16
LANG_NEUTRAL = 0

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.BeginUpdateResourceW.restype = wintypes.HANDLE
_k32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
_k32.UpdateResourceW.restype = wintypes.BOOL
_k32.UpdateResourceW.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, wintypes.LPVOID,
    wintypes.WORD, wintypes.LPVOID, wintypes.DWORD,
]
_k32.EndUpdateResourceW.restype = wintypes.BOOL
_k32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
_k32.LoadLibraryExW.restype = wintypes.HMODULE
_k32.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
_k32.FreeLibrary.argtypes = [wintypes.HMODULE]

_ENUM_NAMES = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_ssize_t,
)
_ENUM_LANGS = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p,
    wintypes.WORD, ctypes.c_ssize_t,
)
_k32.EnumResourceNamesW.restype = wintypes.BOOL
_k32.EnumResourceNamesW.argtypes = [
    wintypes.HMODULE, ctypes.c_void_p, _ENUM_NAMES, ctypes.c_ssize_t,
]
_k32.EnumResourceLanguagesW.restype = wintypes.BOOL
_k32.EnumResourceLanguagesW.argtypes = [
    wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p, _ENUM_LANGS,
    ctypes.c_ssize_t,
]


def _as_res(value):
    """int id or str name -> LPVOID usable as a resource type/name."""
    if isinstance(value, int):
        return ctypes.c_void_p(value)
    return ctypes.cast(ctypes.c_wchar_p(value), ctypes.c_void_p)


def _from_ptr(ptr) -> int | str:
    """Resource name pointer -> int id (IS_INTRESOURCE) or str."""
    val = ptr or 0
    if val >> 16 == 0:
        return val
    return ctypes.wstring_at(val)


def _enum(path: Path, res_type: int) -> list:
    """[(name, lang), ...] for every resource of `res_type` in the PE file."""
    LOAD_AS_DATA = 0x2 | 0x20  # DATAFILE | IMAGE_RESOURCE
    hmod = _k32.LoadLibraryExW(str(path), None, LOAD_AS_DATA)
    if not hmod:
        raise OSError(f"LoadLibraryExW failed for {path}")
    out = []
    try:
        def on_name(h, _type, name_ptr, _lp):
            name = _from_ptr(name_ptr)

            def on_lang(h2, _t2, _n2, lang, _lp2):
                out.append((name, lang))
                return True

            _k32.EnumResourceLanguagesW(
                h, _as_res(res_type), _as_res(name), _ENUM_LANGS(on_lang), 0,
            )
            return True

        _k32.EnumResourceNamesW(hmod, _as_res(res_type), _ENUM_NAMES(on_name), 0)
    finally:
        _k32.FreeLibrary(hmod)
    return out


def _ico_to_resources(ico_bytes: bytes):
    """Parse an .ico: -> (group_icon_dir_bytes, [icon_image_bytes, ...])."""
    count = struct.unpack_from("<H", ico_bytes, 4)[0]
    images, group = [], ico_bytes[:6]
    for i in range(count):
        w, h, colors, _res, planes, bpp, size, offset = struct.unpack_from(
            "<BBBBHHII", ico_bytes, 6 + i * 16,
        )
        images.append(ico_bytes[offset:offset + size])
        # GRPICONDIRENTRY: like ICONDIRENTRY but ends in a WORD resource id.
        group += struct.pack("<BBBBHHIH", w, h, colors, 0, planes, bpp, size, i + 1)
    return group, images


def _build_ico(tmp: Path) -> bytes:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import tray

    img = tray._make_image(muted=False)
    ico = tmp / "voice-of-claude.ico"
    img.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])
    return ico.read_bytes()


def main() -> int:
    scripts = Path(sys.executable).resolve().parent
    base = Path(sys.base_prefix)
    src = base / "pythonw.exe"
    dst = scripts / EXE_NAME
    if not src.exists():
        print(f"base pythonw.exe not found: {src}")
        return 1

    # 1+2. Copy the base interpreter and the runtime DLLs it needs beside it.
    shutil.copy2(src, dst)
    copied = [dst.name]
    for dll in list(base.glob("python*.dll")) + list(base.glob("vcruntime*.dll")):
        target = scripts / dll.name
        if not target.exists():
            shutil.copy2(dll, target)
            copied.append(dll.name)

    # 3+4. Swap the icon, drop the version info.
    group, images = _ico_to_resources(_build_ico(scripts))
    old_groups = _enum(dst, RT_GROUP_ICON)
    old_icons = _enum(dst, RT_ICON)
    old_versions = _enum(dst, RT_VERSION)

    h = _k32.BeginUpdateResourceW(str(dst), False)
    if not h:
        print("BeginUpdateResource failed")
        return 1
    def upd(res_type, name, lang, data) -> None:
        if data is None:
            r = _k32.UpdateResourceW(h, _as_res(res_type), _as_res(name), lang, None, 0)
        else:
            buf = ctypes.create_string_buffer(data, len(data))
            r = _k32.UpdateResourceW(
                h, _as_res(res_type), _as_res(name), lang, buf, len(data),
            )
        if not r:
            print(f"UpdateResource failed: type={res_type} name={name!r} "
                  f"lang={lang} err={ctypes.get_last_error()}")

    for name, lang in old_groups:
        upd(RT_GROUP_ICON, name, lang, None)
    for name, lang in old_versions:
        upd(RT_VERSION, name, lang, None)
    for name, lang in old_icons:
        upd(RT_ICON, name, lang, None)
    lang = old_groups[0][1] if old_groups else LANG_NEUTRAL
    for i, image in enumerate(images):
        upd(RT_ICON, i + 1, lang, image)
    upd(RT_GROUP_ICON, 1, lang, group)
    if not _k32.EndUpdateResourceW(h, False):
        print(f"EndUpdateResource failed, err={ctypes.get_last_error()}")
        return 1

    print(f"built {dst}")
    print("copied:", ", ".join(copied))
    return 0


if __name__ == "__main__":
    sys.exit(main())
