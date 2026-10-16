#!/usr/bin/env python3
"""Metadata-only bootstrap triage for inFAMOUS First Light (CUSA00575).

This tool never copies game code into the repository.  It reads a user's own
plaintext/decrypted dump and writes only hashes, metadata, import NIDs and
layout information needed to develop the native Windows compatibility layer.

Expected dump layout:
  <game>/eboot.bin
  <game>/sce_sys/param.sfo
  <game>/sce_module/libc.prx
  <game>/sce_module/libSceFios2.prx
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from prepare import inspect_libc, parse_self, sfo, span, unpack  # noqa: E402


def dynamic_info(path: Path) -> dict:
    source = path.read_bytes()
    elf, header, ph, _segments, missing = parse_self(source)
    dynamic = next(p for p in ph if p["type"] == 2)
    entries = []
    for pos in range(dynamic["offset"], dynamic["offset"] + dynamic["filesz"], 16):
        tag, value = unpack("<QQ", elf, pos)
        if not tag:
            break
        entries.append((tag, value))
    tags = dict(entries)

    lib = next(p for p in ph if p["type"] == 0x61000000)
    blob = span(elf, lib["offset"], lib["filesz"])
    strings = span(blob, tags[0x61000035], tags[0x61000037])

    def string(offset: int) -> str:
        span(strings, offset, 1)
        end = strings.index(0, offset)
        return strings[offset:end].decode("ascii", "replace")

    symtab = span(blob, tags[0x61000039], tags[0x6100003f])
    symbols = []
    for pos in range(0, len(symtab), 24):
        name, info, _other, section, value, size = unpack("<IBBHQQ", symtab, pos)
        symbols.append(
            {
                "name": string(name),
                "type": info & 15,
                "binding": info >> 4,
                "section": section,
                "value": value,
                "size": size,
            }
        )

    imports = {}
    relocation_counts = collections.Counter()
    for offset_tag, size_tag in ((0x61000029, 0x6100002D), (0x6100002F, 0x61000031)):
        if offset_tag not in tags or size_tag not in tags:
            continue
        table = span(blob, tags[offset_tag], tags[size_tag])
        for pos in range(0, len(table), 24):
            _target, info, _addend = unpack("<QQq", table, pos)
            kind, sym = info & 0xFFFFFFFF, info >> 32
            relocation_counts[kind] += 1
            if kind in (1, 6, 7) and sym < len(symbols) and not symbols[sym]["section"]:
                imports[sym] = symbols[sym]["name"]

    loads = [p for p in ph if p["type"] in (1, 0x61000010)]
    image_bytes = max(p["vaddr"] + p["memsz"] for p in loads)
    return {
        "sha256": hashlib.sha256(source).hexdigest(),
        "bytes": len(source),
        "entry": hex(header[4]),
        "image_bytes": image_bytes,
        "program_header_count": header[10],
        "unavailable_metadata_headers": missing,
        "needed": [string(value) for tag, value in entries if tag == 1],
        "imports": list(imports.values()),
        "import_count": len(imports),
        "relocation_counts": dict(relocation_counts),
    }


FS_LOAD_ANY = re.compile(
    rb'\x64([\x48\x4c])\x8b([\x04\x0c\x14\x1c\x24\x2c\x34\x3c])\x25\x00\x00\x00\x00'
)

def tls_loads(path: Path) -> dict:
    source=path.read_bytes()
    elf,_header,ph,_segments,_missing=parse_self(source)
    counts=collections.Counter()
    for p in ph:
        if p["type"] != 1 or not p["flags"] & 1 or not p["filesz"]:
            continue
        code=span(elf,p["offset"],p["filesz"])
        for match in FS_LOAD_ANY.finditer(code):
            rex=match.group(1)[0]
            modrm=match.group(2)[0]
            reg=(modrm >> 3) & 7
            if rex == 0x4c:
                reg += 8
            names=("rax","rcx","rdx","rbx","rsp","rbp","rsi","rdi",
                   "r8","r9","r10","r11","r12","r13","r14","r15")
            counts[names[reg]] += 1
    return {
        "total":sum(counts.values()),
        "rax":counts.get("rax",0),
        "non_rax":sum(counts.values())-counts.get("rax",0),
        "by_register":dict(sorted(counts.items())),
        "note":"Every entry must be rewritten from FS to guest GS/TLS; rax-only patching is insufficient.",
    }


def known_nids() -> dict[str, str]:
    path = ROOT / "src" / "import_names.inc"
    if not path.is_file():
        return {}
    result = {}
    pattern = re.compile(r'\{"([^"]+)","([^"]+)"\}')
    for scoped, symbol in pattern.findall(path.read_text(encoding="utf-8")):
        result.setdefault(scoped.split("#", 1)[0], symbol)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game", type=Path, help="decrypted CUSA00575 game directory")
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "out" / "firstlight-triage.json",
        help="JSON report path",
    )
    args = parser.parse_args()

    game = args.game.resolve()
    required = {
        "eboot": game / "eboot.bin",
        "param": game / "sce_sys" / "param.sfo",
        "libc": game / "sce_module" / "libc.prx",
        "fios": game / "sce_module" / "libSceFios2.prx",
    }
    missing = [str(path) for path in required.values() if not path.is_file()]
    if missing:
        parser.error("missing required file(s): " + ", ".join(missing))

    metadata = sfo(required["param"].read_bytes())
    if metadata.get("TITLE_ID") != "CUSA00575":
        parser.error(f"expected CUSA00575, got {metadata.get('TITLE_ID')!r}")

    eboot = dynamic_info(required["eboot"])
    libc = dynamic_info(required["libc"])
    fios = dynamic_info(required["fios"])
    libc_evidence = inspect_libc(required["libc"])

    names = known_nids()
    bases = [item.split("#", 1)[0] for item in eboot["imports"]]
    named = sorted({base: names[base] for base in bases if base in names}.items())
    unknown = sorted(set(base for base in bases if base not in names))

    module_presence = [
        {
            "name": name,
            "bundled_in_dump": (game / "sce_module" / name).is_file(),
            "provider": "native PRX available" if (game / "sce_module" / name).is_file()
                        else "host runtime/HLE required",
        }
        for name in eboot["needed"]
    ]
    def payload_present(name: str) -> bool:
        return (game / name).exists() or (game / "dvdroot_ps4" / name).exists()

    report = {
        "target": {
            "title": metadata.get("TITLE"),
            "title_id": metadata.get("TITLE_ID"),
            "app_ver": metadata.get("APP_VER"),
            "version": metadata.get("VERSION"),
            "content_id": metadata.get("CONTENT_ID"),
            "system_ver": metadata.get("SYSTEM_VER"),
        },
        "eboot": {**eboot, "thread_pointer_loads": tls_loads(required["eboot"])},
        "libc": {
            **libc,
            "init_env_is_ret": bool(libc_evidence.get("init_env_is_ret")),
            "init_env": {
                key: libc_evidence[key]
                for key in ("symbol", "address", "size", "bytes")
                if key in libc_evidence
            },
        },
        "libSceFios2": fios,
        "dependency_audit": {
            "eboot_needed_modules": module_presence,
            "native_module_count": sum(item["bundled_in_dump"] for item in module_presence),
            "host_module_count": sum(not item["bundled_in_dump"] for item in module_presence),
            "payload": {
                "art": payload_present("art"),
                "playgo.pgm": payload_present("playgo.pgm"),
            },
            "note": "A PS4 system PRX absent from the game dump is not automatically a missing game file; it must be supplied by the host runtime/HLE.",
        },
        "runtime_name_coverage": {
            "known_count": len(named),
            "unknown_count": len(unknown),
            "known": [{"nid": nid, "symbol": symbol} for nid, symbol in named],
            "unknown_nids": unknown,
        },
        "notes": [
            "No game payload is written to this report.",
            "Library/module suffixes after '#' are dump-local; bootstrap resolution must use the stable NID.",
        ],
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        f"{metadata.get('TITLE')} | {metadata.get('TITLE_ID')} | "
        f"version {metadata.get('VERSION')} | entry {eboot['entry']}"
    )
    dep = report["dependency_audit"]
    print(f"Dependency audit: {dep['native_module_count']} native PRXs present; "
          f"{dep['host_module_count']} system modules require host runtime/HLE; "
          f"art={dep['payload']['art']} playgo={dep['payload']['playgo.pgm']}")
    print(
        f"{eboot['import_count']} eboot imports; "
        f"{len(named)} named by current runtime table; {len(unknown)} still unknown"
    )
    tls=tls_loads(required["eboot"])
    print(f"Guest TLS loads: {tls['total']} total; {tls['non_rax']} use registers other than rax")
    print(f"libc _init_env is RET: {libc_evidence.get('init_env_is_ret', False)}")
    print(f"Report: {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
