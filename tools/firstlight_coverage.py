#!/usr/bin/env python3
"""Audit First Light's post-link imports against the native host runtime sources.

Consumes out/link.json (written by link_modules.py).  Native module bindings are
already absent from its unresolved_imports list, so this checks only imports
that still need the C runtime or the embedded shadPS4/Vulkan backend.

The result is conservative source evidence, not a substitute for actually
calling the ABI: an import is "covered" when its stable NID or mapped symbol is
registered/implemented in the source tree.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent


def stable_nid(name: str) -> str:
    return name.split("#", 1)[0]


def names_table() -> dict[str, str]:
    text = (ROOT / "src" / "import_names.inc").read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for scoped, symbol in re.findall(r'\{"([^"]+)","([^"]+)"\}', text):
        out.setdefault(stable_nid(scoped), symbol)
    return out


def source_corpus(paths: list[Path]) -> str:
    parts: list[str] = []
    for path in paths:
        try:
            parts.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            pass
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--link", type=Path, default=ROOT / "out" / "link.json")
    parser.add_argument("--out", type=Path, default=ROOT / "out" / "firstlight-coverage.json")
    args = parser.parse_args()

    link = json.loads(args.link.read_text(encoding="utf-8"))
    imports = list(dict.fromkeys(link.get("unresolved_imports", [])))
    mapped = names_table()

    runtime_files = sorted((ROOT / "src").glob("runtime*.c"))
    gpu_files = sorted((ROOT / "gpu" / "shim").rglob("*.cpp"))
    gpu_files += sorted((ROOT / "gpu" / "shadps4" / "core" / "libraries").rglob("*.cpp"))
    gpu_files += sorted((ROOT / "gpu" / "bbnet").rglob("*.cpp"))
    runtime = source_corpus(runtime_files)
    gpu = source_corpus(gpu_files)

    rows = []
    unsupported = []
    counts = {"runtime": 0, "gpu": 0, "both": 0, "unsupported": 0}
    for original in imports:
        nid = stable_nid(original)
        symbol = mapped.get(nid)
        # Stable NIDs occur in exact resolver tables; exported symbol names occur
        # in RuntimeExport tables.  Check both because this port uses both forms.
        runtime_hit = (nid in runtime) or bool(symbol and f'"{symbol}"' in runtime)
        gpu_hit = (nid in gpu) or bool(symbol and f'"{symbol}"' in gpu)
        if runtime_hit and gpu_hit:
            provider = "both"
        elif runtime_hit:
            provider = "runtime"
        elif gpu_hit:
            provider = "gpu"
        else:
            provider = "unsupported"
            unsupported.append(original)
        counts[provider] += 1
        rows.append(
            {
                "import": original,
                "nid": nid,
                "symbol": symbol,
                "provider": provider,
            }
        )

    report = {
        "linked_native_bindings": link.get("bindings", 0),
        "post_link_imports": len(imports),
        "counts": counts,
        "unsupported": unsupported,
        "imports": rows,
        "note": (
            "Source coverage is conservative. Runtime execution remains authoritative: "
            "bb-probe stops at the first actually-called unsupported import."
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        "First Light import coverage: "
        f"{report['linked_native_bindings']} native bindings; "
        f"{counts['runtime']} runtime, {counts['gpu']} GPU, {counts['both']} both; "
        f"{counts['unsupported']} still unsupported"
    )
    if unsupported:
        print("Still unsupported after native linking:")
        for name in unsupported:
            print("  ", name, "->", mapped.get(stable_nid(name), "unknown NID"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
