"""Run the glibc TUTK library on musl (Home Assistant OS containers are Alpine).

The library is a glibc build and imports eight names musl does not export.
Six of them are pure aliases on 64-bit Linux, so a copy of the library gets
those import names rewritten in place (``.dynstr``; the new name is never
longer, the rest is NUL padded):

    fopen64 open64 lseek64 mmap64 readdir64   -> the same call without "64"
    __register_atfork(prepare, parent, child, dso) -> pthread_atfork(...)

The other two are glibc's old stat wrappers, which take an extra leading
version argument: ``__xstat64(ver, path, buf)`` / ``__fxstat64(ver, fd, buf)``.
They are renamed to ``stat`` / ``fstat`` so the library loads, and once it is
loaded ``fix_stat_slots`` points their GOT entries at small trampolines that
drop the version argument. No compiler, no extra package.

Standalone on purpose (no Home Assistant imports): the producer process loads
it from its own directory.
"""
from __future__ import annotations

import ctypes
import os
import struct

ALIASES = {
    "fopen64": "fopen",
    "open64": "open",
    "lseek64": "lseek",
    "mmap64": "mmap",
    "readdir64": "readdir",
    "__register_atfork": "pthread_atfork",
    "__xstat64": "stat",
    "__fxstat64": "fstat",
    # glibc emits __rawmemchr(s, c) for strchr-like scans; strchr gives the
    # same result for c == 0 and for a c that occurs before the terminator.
    "__rawmemchr": "strchr",
    "__strdup": "strdup",
    "__strtok_r": "strtok_r",
}
# Renamed imports whose arguments still need shifting after the load.
SHIFTED = {"stat": "stat", "fstat": "fstat"}

SHT_DYNSYM = 11
SHT_RELA = 4
R_AARCH64_GLOB_DAT = 1025
R_AARCH64_JUMP_SLOT = 1026
R_X86_64_GLOB_DAT = 6
R_X86_64_JUMP_SLOT = 7
_SLOT_TYPES = {R_AARCH64_GLOB_DAT, R_AARCH64_JUMP_SLOT, R_X86_64_GLOB_DAT, R_X86_64_JUMP_SLOT}


def is_musl() -> bool:
    """True when this process runs on musl libc."""
    return any(name.startswith("ld-musl-") for name in _listdir("/lib"))


def _listdir(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def _sections(data: bytes) -> list[tuple]:
    if data[:4] != b"\x7fELF" or data[4] != 2:
        raise ValueError("not a 64-bit ELF file")
    shoff, = struct.unpack_from("<Q", data, 0x28)
    shentsize, shnum = struct.unpack_from("<HH", data, 0x3A)
    return [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize) for i in range(shnum)]


def _cstr(data: bytes, pos: int) -> str:
    return data[pos:data.index(b"\0", pos)].decode()


def dynsyms(data: bytes) -> list[tuple[str, int, int, int]]:
    """(name, name offset in the file, st_shndx, st_value) of every dynamic symbol, by index."""
    sections = _sections(data)
    for _n, kind, _f, _a, off, size, link, _i, _al, entsize in sections:
        if kind != SHT_DYNSYM:
            continue
        stroff = sections[link][4]
        out = []
        for pos in range(off, off + size, entsize):
            st_name, _info, _other, shndx, value = struct.unpack_from("<IBBHQ", data, pos)
            out.append((_cstr(data, stroff + st_name), stroff + st_name, shndx, value))
        return out
    raise ValueError("no dynamic symbol table")


def undefined_symbols(path: str) -> list[str]:
    """Names the library imports."""
    with open(path, "rb") as fh:
        data = fh.read()
    return sorted({name for name, _pos, shndx, _v in dynsyms(data) if shndx == 0 and name})


def patch_for_musl(src: str, dst: str) -> None:
    """Write a copy of ``src`` with the glibc-only imports renamed.

    The linker shares string tails (``fork`` is the end of
    ``__register_atfork``, ``open64`` the end of ``fopen64``), so each new
    name is written right-aligned on the old one's terminating NUL and the
    symbol's ``st_name`` moves forward by the difference. Every replacement
    ends with the tail it may share (pthread_atfork / fork, fopen / open), and
    the result is verified: every symbol must read its intended name.
    """
    with open(src, "rb") as fh:
        data = bytearray(fh.read())
    original = dynsyms(bytes(data))
    sections = _sections(bytes(data))
    symtab = next(s for s in sections if s[1] == SHT_DYNSYM)
    sym_off, entsize = symtab[4], symtab[9]
    stroff = sections[symtab[6]][4]
    strend = stroff + sections[symtab[6]][5]
    renamed = [(i, name, pos) for i, (name, pos, shndx, _v) in enumerate(original) if shndx == 0 and name in ALIASES]
    placement: dict[int, int] = {}
    # A target name already in the table (as a whole string or a NUL-ended
    # tail of one) is reused as is; only the others are written in place.
    for index, name, pos in renamed:
        found = _find_cstr(data, ALIASES[name].encode(), stroff, strend, [(p, p + len(n)) for _i, n, p in renamed])
        if found is not None:
            placement[index] = found
    expected = []
    for index, (name, pos, shndx, _value) in enumerate(original):
        new = ALIASES.get(name) if shndx == 0 else None
        if not new:
            expected.append(name)
            continue
        if index in placement:
            at = placement[index]
        else:
            shift = len(name) - len(new)
            data[pos:pos + len(name)] = b"\0" * shift + new.encode()
            at = pos + shift
        struct.pack_into("<I", data, sym_off + index * entsize, at - stroff)
        expected.append(new)
    got = [name for name, _p, _s, _v in dynsyms(bytes(data))]
    wrong = [(want, have) for want, have in zip(expected, got) if want != have]
    if wrong:
        raise ValueError(f"string table rewrite broke symbol names: {wrong[:5]}")
    tmp = dst + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, dst)


def _find_cstr(data: bytes, target: bytes, start: int, end: int, avoid: list[tuple[int, int]]) -> int | None:
    """Offset of ``target`` + NUL inside [start, end) that no rewrite touches."""
    needle = target + b"\0"
    pos = data.find(needle, start, end)
    while pos != -1:
        if not any(a < pos + len(needle) and pos < b for a, b in avoid):
            return pos
        pos = data.find(needle, pos + 1, end)
    return None


def musl_copy(src: str, dest_dir: str) -> str:
    """Path of the musl-adapted copy of ``src``, (re)written when stale."""
    dst = os.path.join(dest_dir, os.path.basename(src).replace(".so", ".musl.so"))
    try:
        stale = os.path.getmtime(dst) < max(os.path.getmtime(src), os.path.getmtime(__file__))
    except OSError:
        stale = True
    if stale:
        os.makedirs(dest_dir, exist_ok=True)
        patch_for_musl(src, dst)
    return dst


def musl_libc() -> ctypes.CDLL | None:
    """Handle of musl's libc itself (its loader), for symbol checks."""
    for name in sorted(_listdir("/lib")):
        if name.startswith("ld-musl-"):
            try:
                return ctypes.CDLL(os.path.join("/lib", name))
            except OSError:
                return None
    return None


_STAT = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
_KEEP: list = []


def fix_stat_slots(lib: ctypes.CDLL, path: str, anchor: str = "IOTC_Get_Version") -> int:
    """Point the renamed stat/fstat imports at version-dropping trampolines.

    Returns how many GOT slots were rewritten.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    syms = dynsyms(data)
    anchor_value = next(v for name, _p, shndx, v in syms if name == anchor and shndx)
    base = ctypes.cast(getattr(lib, anchor), ctypes.c_void_p).value - anchor_value

    libc = ctypes.CDLL(None, use_errno=True)
    real = {"stat": libc.stat, "fstat": libc.fstat}
    real["stat"].argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    real["fstat"].argtypes = [ctypes.c_int, ctypes.c_void_p]
    tramp = {
        "stat": _STAT(lambda _ver, p, buf: real["stat"](p, buf)),
        "fstat": _STAT(lambda _ver, fd, buf: real["fstat"](fd, buf)),
    }
    _KEEP.append(tramp)

    slots = []
    for _n, kind, _f, _a, off, size, _l, _i, _al, entsize in _sections(data):
        if kind != SHT_RELA:
            continue
        for pos in range(off, off + size, entsize):
            r_offset, r_info, _addend = struct.unpack_from("<QQq", data, pos)
            sym, rtype = r_info >> 32, r_info & 0xFFFFFFFF
            if rtype in _SLOT_TYPES and sym < len(syms) and syms[sym][0] in SHIFTED and syms[sym][2] == 0:
                slots.append((base + r_offset, syms[sym][0]))
    if not slots:
        return 0

    page = os.sysconf("SC_PAGE_SIZE")
    libc.mprotect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    for addr, name in slots:
        # RELRO made the GOT read-only after relocation. It stays writable
        # afterwards: the page may also hold .data the library writes to.
        if libc.mprotect(addr & ~(page - 1), page, 3) != 0:  # PROT_READ | PROT_WRITE
            raise OSError(ctypes.get_errno(), "mprotect failed on the GOT")
        ctypes.c_void_p.from_address(addr).value = ctypes.cast(tramp[name], ctypes.c_void_p).value
    return len(slots)
