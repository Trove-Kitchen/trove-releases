#!/usr/bin/env python3
"""Check an Android APK or AAB against Google Play's 16 KB memory page size rule.

For every native library in a 64-bit ABI (arm64-v8a, x86_64):
  - each ELF LOAD segment must be aligned to at least 16 KB (p_align >= 0x4000);
  - in an APK, the .so must be stored uncompressed at a 16 KB-aligned offset,
    the same thing `zipalign -c -P 16 4` checks. (An AAB's libraries are
    aligned by Play when it builds the APKs, so only the ELF check applies.)

Prints one line per library and exits 1 if any fails.

  python3 tools/check_16kb.py app-release.aab [more.apk ...]

A copy of trove-app scripts/check_android_16kb.py, kept here so the check also
runs on trove-app refs from before that script existed.
"""

import struct
import sys
import zipfile

PAGE = 16 * 1024
ABIS_64 = ("arm64-v8a", "x86_64")
PT_LOAD = 1


def load_alignments(elf: bytes) -> list[int]:
    if elf[:4] != b"\x7fELF":
        raise ValueError("not an ELF file")
    if elf[4] != 2:
        raise ValueError("not a 64-bit ELF")
    endian = "<" if elf[5] == 1 else ">"
    phoff, = struct.unpack_from(endian + "Q", elf, 0x20)
    phentsize, phnum = struct.unpack_from(endian + "HH", elf, 0x36)
    aligns = []
    for i in range(phnum):
        base = phoff + i * phentsize
        p_type, = struct.unpack_from(endian + "I", elf, base)
        if p_type == PT_LOAD:
            p_align, = struct.unpack_from(endian + "Q", elf, base + 0x30)
            aligns.append(p_align)
    return aligns


def data_offset(path: str, info: zipfile.ZipInfo) -> int:
    with open(path, "rb") as f:
        f.seek(info.header_offset)
        header = f.read(30)
    name_len, extra_len = struct.unpack_from("<HH", header, 26)
    return info.header_offset + 30 + name_len + extra_len


def check(path: str) -> int:
    is_apk = path.endswith(".apk")
    failures = checked = 0
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            parts = info.filename.split("/")
            if not info.filename.endswith(".so") or "lib" not in parts:
                continue
            abi = parts[parts.index("lib") + 1] if parts.index("lib") + 1 < len(parts) else ""
            if abi not in ABIS_64:
                continue
            checked += 1
            problems = []
            aligns = load_alignments(z.read(info))
            smallest = min(aligns) if aligns else 0
            if smallest < PAGE:
                problems.append(f"LOAD aligned to {smallest // 1024 or smallest} {'KB' if smallest >= 1024 else 'bytes'}")
            if is_apk:
                if info.compress_type != zipfile.ZIP_STORED:
                    problems.append("compressed in the APK")
                elif data_offset(path, info) % PAGE:
                    problems.append("not at a 16 KB zip offset")
            status = "FAIL " + "; ".join(problems) if problems else "ok"
            print(f"{status:<40} {info.filename}")
            failures += bool(problems)
    print(f"{path}: {checked} 64-bit native libraries, {failures} not 16 KB-ready")
    return failures


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sys.exit(1 if sum(check(p) for p in sys.argv[1:]) else 0)
