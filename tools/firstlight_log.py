#!/usr/bin/env python3
"""Classify the latest CUSA00575 bootstrap log into the next engineering target."""
from __future__ import annotations
import argparse,json,re
from pathlib import Path

PATTERNS=[
 ("unsupported_import",re.compile(r"STOP: first unsupported PS4 import:\s*(\S+)")),
 ("guest_fault",re.compile(r"(?:Guest fault.*guest offset|Fault:.*guest offset)\s*(0x[0-9a-fA-F]+)")),
 ("driver_fault",re.compile(r"(amdvlk64\.dll|atio6axx\.dll|nvoglv64\.dll|nvwgf2umx\.dll)",re.I)),
 ("gpu_capability_block",re.compile(r"(Unsupported Vulkan image combination was blocked|Refusing unsupported image before vkCreateImage)",re.I)),
 ("gpu_assertion",re.compile(r"(STOP: GPU library assertion failed|Assertion Failed!)",re.I)),
 ("watchdog",re.compile(r"STOP: watchdog timeout")),
 ("cpu",re.compile(r"STOP: unsupported CPU")),
]
MILESTONES=[
 ("game_entry",re.compile(r"Original guest entry instructions executed|entered game",re.I)),
 ("video",re.compile(r"(sceVideoOutOpen|VideoOut|swapchain|present)",re.I)),
 ("audio",re.compile(r"(sceAudioOut|AudioOut)",re.I)),
 ("pad",re.compile(r"(scePad|controller|gamepad)",re.I)),
 ("save",re.compile(r"(sceSaveData|SaveData)",re.I)),
]

def main()->int:
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument("log",type=Path); p.add_argument("--exit-code",type=int,default=0); p.add_argument("--out",type=Path,required=True)
 a=p.parse_args()
 text=a.log.read_text(encoding="utf-8",errors="replace") if a.log.is_file() else ""
 stop={"kind":"unknown","detail":None}
 for kind,rx in PATTERNS:
  m=rx.search(text)
  if m:
   stop={"kind":kind,"detail":m.group(1) if m.groups() else None}; break
 milestones=[name for name,rx in MILESTONES if rx.search(text)]
 lines=text.splitlines()
 upload_lines=[line for line in lines if line.startswith("GPU: FL upload #")]
 last_upload=upload_lines[-1] if upload_lines else None
 resource_lines=[line for line in lines if line.startswith("GPU: FL resource ")]
 last_resource=resource_lines[-1] if resource_lines else None
 bcn_lines=[line for line in lines if "BCN CAPABILITY" in line or "BCN COPY DEBUG" in line]
 last_bcn= bcn_lines[-1] if bcn_lines else None
 submit_lines=[line for line in lines if line.startswith("GPU: FL submit #")]
 last_submit=submit_lines[-1] if submit_lines else None
 # Preserve a compact crash tail in the machine-readable diagnosis so the next iteration does
 # not depend on a second file just to identify the call immediately preceding the fault.
 crash_tail=lines[-32:]
 if a.exit_code==0 and stop["kind"]=="unknown": stop={"kind":"clean_exit","detail":None}
 next_action={
  "unsupported_import":"implement or bind the reported ABI contract; do not success-stub it blindly",
  "guest_fault":"inspect the reported guest offset and its inputs/last runtime calls",
  "driver_fault":"isolate the GPU command/resource path before changing guest semantics",
  "gpu_capability_block":"inspect the rejected Vulkan image combination; the runtime stopped before passing it to the driver",
  "gpu_assertion":"inspect the GPU errors immediately before the assertion; do not change guest CPU semantics",
  "watchdog":"inspect the emitted thread stacks for deadlock or an unserviced event",
  "cpu":"use the supported CPU path or extend the compatibility translator",
  "clean_exit":"the process exited cleanly; inspect the last milestone and why the game requested exit",
  "unknown":"inspect the tail of last_run.log; no known stop signature was found",
 }[stop["kind"]]
 report={"exit_code":a.exit_code,"stop":stop,"milestones":milestones,"next_action":next_action,
         "last_gpu_upload":last_upload,"last_gpu_resource":last_resource,
         "last_gpu_submit":last_submit,"last_bcn_event":last_bcn,
         "crash_tail":crash_tail,"log":str(a.log)}
 a.out.parent.mkdir(parents=True,exist_ok=True)
 a.out.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
 print(f"Boot stage: {', '.join(milestones) if milestones else 'pre-entry/unknown'}")
 print(f"Stop: {stop['kind']}"+(f" ({stop['detail']})" if stop["detail"] else ""))
 print(f"Next: {next_action}")
 return 0
if __name__=="__main__": raise SystemExit(main())
