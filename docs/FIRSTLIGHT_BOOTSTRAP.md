# inFAMOUS First Light native bootstrap

Target: **inFAMOUS First Light — CUSA00575 — versions 1.00 and 1.04**.

This branch is the first title-specific bootstrap of the Bloodborne native-port
base.  It does **not** contain game files and it is not yet a playable port.

## Confirmed game revisions

### CUSA00575 v1.00

- PS4 executable format: plaintext x86-64 SELF/ELF
- guest entry point: `0xA19C30`
- loaded eboot image: about 132 MiB
- eboot imports: 468
- eboot relocations observed:
  - type 8: 97,778
  - type 7: 456
  - type 1: 445
  - type 6: 7
- `libc.prx` is linkable by the existing native-module linker.
- `libc.prx::_init_env` is exactly one `RET` instruction, satisfying the
  bootstrap safety check already used by the loader.
- `libSceFios2.prx` is also a plaintext/linkable x86-64 module.

The eboot declares 32 required PRX modules, including GNM, VideoOut,
Videodec, AudioOut, AJM, Pad, SaveData, Fios2, PlayGo, system services and NP
libraries.


### CUSA00575 v1.04

The update is a real executable/runtime revision rather than metadata-only:

- eboot SHA-256: `693dc18504f30ae106bcd33cbb0c1d29a3dbbdb330754ee9dc2194ca6b56e240`
- eboot bytes: 18,185,397
- guest entry point: `0x6FFD70`
- loaded eboot image: 136,535,375 bytes
- program headers: 11
- eboot imports: 510
- eboot relocations observed:
  - type 8: 97,810
  - type 7: 504
  - type 1: 446
- `libSceGnmDriver_padebug.prx` changes to `libSceGnmDriver.prx`.
- `libSceJson2.prx` is newly required by the updated eboot.
- updated `libc.prx`: 1,057,666 bytes, 84 imports, and now declares
  `libSceLibcInternal.prx` in addition to libkernel/Fios2.
- updated `libSceFios2.prx`: 451,057 bytes, 81 imports, and now also declares
  `libSceSysmodule.prx`.
- the updated libc `_init_env` remains exactly one `RET`, so the native
  loader safety assumption still holds.

The triage tool fingerprints the known v1.00 and v1.04 eboot/libc/Fios2 sets
and reports a mixed-revision warning if a base executable is accidentally
combined with update PRXs (or the reverse). Unknown hashes are reported rather
than rejected.

## Important compatibility change

Orbis symbol strings have the form `NID#library-id#module-id`.  The IDs after
the NID are local to each executable and differ between Bloodborne and First
Light.  The stable part is the NID before the first `#`.

The runtime on this branch therefore canonicalizes imports by stable NID before
sending them to the existing host resolvers.  This allows implementations that
already exist in the Bloodborne-derived runtime to be reused by CUSA00575
instead of being rejected only because the suffix is different.

## Triage

Run:

```text
python tools/firstlight_triage.py "C:\path\to\CUSA00575"
```

Expected layout:

```text
CUSA00575/
  eboot.bin
  sce_sys/
    param.sfo
  sce_module/
    libc.prx
    libSceFios2.prx
```

The command writes `out/firstlight-triage.json`.  The report contains hashes,
metadata, module dependencies, NIDs and relocation counts only.  It never
copies game code into the repository.

## Current import picture

v1.00 has 468 eboot imports. v1.04 has 510 and is an ABI revision in its own
right: compared with v1.00 it adds 91 stable NIDs and removes 49.

Only 7 of those 91 newly introduced NIDs are already named by the current
host table (`sceGnmMapComputeQueueWithPriority`, `sceGnmUpdateHsShader`,
`sceGnmUpdateVsShader`, `sceGnmUpdateGsShader`,
`sceVideoOutGetFlipStatus`, `scePthreadYield`, and `sceNpGetOnlineId`).
The current table therefore names about 325 of the 510 v1.04 imports before
native PRX linkage and host/GPU runtime resolution; roughly 185 remain unnamed
at this metadata stage.

That count is deliberately *not* treated as a list of functions to success-stub.
Imports provided by linked native modules or by GPU/runtime HLE can disappear
from the effective gap, and the first actually-called unresolved ABI remains
the authoritative bring-up failure. v1.04 enables one-call import tracing by
default for that reason.

The update also introduces `libSceJson2.prx`, while its updated libc/Fios
dependencies reference `libSceLibcInternal.prx` and Sysmodule. These are
reported as system-module dependencies unless a user-owned native module is
present; the bootstrap does not pretend that a missing firmware PRX is a
missing game payload.

The next bootstrap milestone is therefore:

1. run the triage against the complete dump;
2. prepare and link eboot + libc + Fios2;
3. start the native probe;
4. record the first actually-called unresolved import or guest fault;
5. implement/port that contract and repeat.

This keeps development evidence-driven instead of adding broad success stubs
that can hide ABI mistakes.

## Repository policy

Do not commit `eboot.bin`, PRX files, PKG contents or other copyrighted game
assets.  Only source code, metadata reports without payloads, patches authored
for this project and build/runtime tooling belong in this repository.
