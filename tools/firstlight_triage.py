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

    report = {
        "target": {
            "title": metadata.get("TITLE"),
            "title_id": metadata.get("TITLE_ID"),
            "app_ver": metadata.get("APP_VER"),
            "version": metadata.get("VERSION"),
            "content_id": metadata.get("CONTENT_ID"),
            "system_ver": metadata.get("SYSTEM_VER"),
        },
        "eboot": eboot,
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
    print(
        f"{eboot['import_count']} eboot imports; "
        f"{len(named)} named by current runtime table; {len(unknown)} still unknown"
    )
    print(f"libc _init_env is RET: {libc_evidence.get('init_env_is_ret', False)}")
    print(f"Report: {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
