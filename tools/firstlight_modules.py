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

def main()->int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("game",type=Path)
    p.add_argument("--json",action="store_true")
    a=p.parse_args()
    game=a.game.resolve()
    main_mod=module(game/"eboot.bin")
    wanted=nids(main_mod,True)
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
            matches=sorted(wanted & exports)
            rows.append({"file":path.name,"usable":True,"matches":matches,"match_count":len(matches)})
    # Linking every matching PRX can pull in a much larger dependency graph.
    # Prefer modules with direct eboot value and leave zero-match modules alone.
    selected=[r["file"] for r in rows if r.get("usable") and r.get("match_count",0)>0]
    report={"selected":selected,"modules":rows,"eboot_import_nids":len(wanted),
            "policy":"direct stable-NID matches only; libc/Fios2 are always linked separately"}
    if a.json:
        print(json.dumps(report))
    else:
        print(json.dumps(report,indent=2))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
