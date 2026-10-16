#!/usr/bin/env python3
"""Select bundled CUSA00575 PRXs that can satisfy unresolved eboot imports.

The selector is deliberately conservative: it never copies a PRX, never links a
module just because it exists, and never treats system-module names as proof.
A module is selected only when one of its exported stable NIDs is imported by
the eboot. libc.prx and libSceFios2.prx are handled by the runner itself.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import re

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/"scripts"))
from link_modules import module

BASE={"libc.prx","libSceFios2.prx"}

def nids(m, imported: bool) -> set[str]:
    out=set()
    for s in m["symbols"]:
        if not s.get("identity"):
            continue
        is_import=not s["section"]
        if is_import != imported:
            continue
        out.add(s["identity"][0])
    return out

def host_coverage(wanted: set[str]) -> tuple[set[str], bool]:
    """Stable NIDs already supplied by the host runtime/GPU backend.

    In a portable package the source tree is intentionally absent.  In that
    case return an empty selection policy rather than guessing and linking
    extra PRXs whose module initializers may introduce new boot dependencies.
    """
    src=ROOT/"src"
    gpu=ROOT/"gpu"
    names_path=src/"import_names.inc"
    if not src.is_dir() or not gpu.is_dir() or not names_path.is_file():
        return set(), False

    mapped={}
    for scoped,symbol in re.findall(r'\{"([^"]+)","([^"]+)"\}',names_path.read_text(encoding="utf-8")):
        mapped.setdefault(scoped.split("#",1)[0],symbol)

    parts=[]
    for path in list(src.glob("runtime*.c")) + list((gpu/"shim").rglob("*.cpp")) + list((gpu/"shadps4").rglob("*.cpp")):
        try:
            parts.append(path.read_text(encoding="utf-8",errors="ignore"))
        except OSError:
            pass
    corpus="\n".join(parts)
    covered=set()
    for nid in wanted:
        symbol=mapped.get(nid)
        if nid in corpus or (symbol and f'"{symbol}"' in corpus):
            covered.add(nid)
    return covered, True

def main()->int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("game",type=Path)
    p.add_argument("--json",action="store_true")
    a=p.parse_args()
    game=a.game.resolve()
    main_mod=module(game/"eboot.bin")
    wanted=nids(main_mod,True)
    covered,source_available=host_coverage(wanted)
    need_native=wanted-covered if source_available else set()
    rows=[]
    directory=game/"sce_module"
    if directory.is_dir():
        for path in sorted(directory.glob("*.prx")):
            if path.name in BASE:
                continue
            try:
                m=module(path)
            except (OSError,ValueError,KeyError,StopIteration,IndexError) as error:
                rows.append({"file":path.name,"usable":False,"reason":str(error),"matches":[]})
                continue
            exports=nids(m,False)
            direct=sorted(wanted & exports)
            needed=sorted(need_native & exports)
            rows.append({"file":path.name,"usable":True,"matches":direct,
                         "needed_matches":needed,"match_count":len(direct),
                         "needed_match_count":len(needed)})
    # Host runtime/GPU implementations win over native module bindings in the
    # loader. Avoid starting a PRX merely to provide an export already covered
    # by HLE; its module_start could create an unnecessary earlier failure.
    selected=[r["file"] for r in rows
              if source_available and r.get("usable") and r.get("needed_match_count",0)>0]
    policy=("link only PRXs that satisfy eboot NIDs not covered by host runtime/GPU"
            if source_available else
            "portable/source-less mode: do not auto-link extra PRXs; libc/Fios2 only")
    report={"selected":selected,"modules":rows,"eboot_import_nids":len(wanted),
            "host_covered_nids":len(covered),"native_needed_nids":len(need_native),
            "source_coverage_available":source_available,
            "policy":policy}
    if a.json:
        print(json.dumps(report))
    else:
        print(json.dumps(report,indent=2))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
