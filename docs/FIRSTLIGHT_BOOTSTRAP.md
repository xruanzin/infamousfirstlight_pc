# inFAMOUS First Light native bootstrap

Target: **inFAMOUS First Light — CUSA00575 — version 1.00**.

This branch is the first title-specific bootstrap of the Bloodborne native-port
base.  It does **not** contain game files and it is not yet a playable port.

## Confirmed from a decrypted CUSA00575 v1.00 dump

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

Before linking native modules, the CUSA00575 eboot has 468 imports.  The
Bloodborne-derived name table already recognizes 366 stable NIDs.  The
remaining 102 are title-specific gaps in the current table.

A large part of that apparent gap is not a host-runtime problem: 22 of the
unknown eboot NIDs belong to `libSceFios2`, which is linked as native guest
code by `scripts/link_modules.py`.

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
