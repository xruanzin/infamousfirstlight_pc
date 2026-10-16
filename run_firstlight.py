#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Native Windows bootstrap runner for inFAMOUS First Light (CUSA00575 v1.00).

This is intentionally separate from run.py: that file still contains Bloodborne
1.09 patches, mods and online setup.  First Light starts from the generic native
loader/runtime/GPU path only, so failures point at real CUSA00575 compatibility
gaps instead of title-specific Bloodborne features.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import runpy

PORT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
SCRIPTS = PORT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from prepare import sfo  # noqa: E402


def fail(message: str) -> "NoReturn":
    print(f"First Light bootstrap: {message}", file=sys.stderr)
    raise SystemExit(1)


def find_probe() -> Path | None:
    requested = os.environ.get("FL_PROBE") or os.environ.get("BB_PROBE")
    if requested:
        return Path(requested)
    for candidate in (PORT / "bin" / "bb-probe.exe", PORT / "out" / "bb-probe.exe"):
        if candidate.is_file():
            return candidate
    return None


def creation_flags(console: bool) -> int:
    if console or os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def python_command(path: Path, *args: object) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--script", str(path), *map(str, args)]
    return [sys.executable, str(path), *map(str, args)]


def run_python(path: Path, *args: object, env: dict[str, str], capture: bool = False) -> subprocess.CompletedProcess[str]:
    command = python_command(path, *args)
    print("+", subprocess.list2cmdline(command))
    result = subprocess.run(
        command,
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    if result.returncode:
        if capture and result.stdout:
            print(result.stdout, end="")
        fail(f"{path.name} failed with exit code {result.returncode}")
    return result


def run_tool(script: str, *args: object, env: dict[str, str]) -> None:
    run_python(SCRIPTS / script, *args, env=env)


def metadata(game: Path) -> dict:
    path = game / "sce_sys" / "param.sfo"
    if not path.is_file():
        fail(f"missing {path}")
    values = sfo(path.read_bytes())
    title_id = values.get("TITLE_ID")
    if title_id != "CUSA00575":
        fail(f"expected CUSA00575, found {title_id!r}")
    version = values.get("VERSION")
    app_ver = values.get("APP_VER")
    reported = [value for value in (version, app_ver) if value is not None]
    if any(value != "01.00" for value in reported):
        fail(f"bootstrap currently targets v1.00; param.sfo reports VERSION={version!r}, APP_VER={app_ver!r}")
    return values


def bootstrap_summary(out: Path, values: dict) -> None:
    link_path = out / "link.json"
    triage_path = out / "firstlight-triage.json"
    link = json.loads(link_path.read_text(encoding="utf-8")) if link_path.is_file() else {}
    triage = json.loads(triage_path.read_text(encoding="utf-8")) if triage_path.is_file() else {}
    unresolved = link.get("unresolved_imports", [])
    report = {
        "title": values.get("TITLE"),
        "title_id": values.get("TITLE_ID"),
        "version": values.get("VERSION"),
        "app_ver": values.get("APP_VER"),
        "entry": triage.get("eboot", {}).get("entry"),
        "linked_modules": link.get("modules", []),
        "native_bindings": link.get("bindings", 0),
        "link_imports": link.get("imports", 0),
        "unresolved_before_host_runtime": len(unresolved),
        "unresolved_before_host_runtime_names": unresolved,
        "note": (
            "The unresolved list is before runtime/GPU HLE resolution. "
            "bb-probe resolves implemented host APIs by stable NID at launch."
        ),
    }
    (out / "firstlight-bootstrap-summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Native link: {report['native_bindings']} bindings; "
        f"{report['unresolved_before_host_runtime']} imports continue to host runtime/GPU"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game", nargs="?", type=Path, help="decrypted CUSA00575 game directory")
    parser.add_argument("--prepare-only", action="store_true", help="prepare/link reports without launching")
    parser.add_argument("--console", action="store_true", help="show bb-probe output live instead of only in the log")
    parser.add_argument("--strict-imports", action="store_true", help="ask bb-probe for strict import diagnostics")
    parser.add_argument("--cpu-only", action="store_true", help="run guest CPU/runtime without initializing Vulkan")
    parser.add_argument("--import-trace", type=int, default=None,
                        help="log the first N calls of every host-resolved import (bring-up diagnostics)")
    parser.add_argument("--timeout", type=int, default=None, help="watchdog timeout in seconds; 0 disables")
    args = parser.parse_args()

    os.chdir(PORT)
    env = os.environ.copy()
    game = args.game or Path(env.get("FL_GAME_DIR") or env.get("BB_GAME_DIR") or (PORT.parent / "CUSA00575"))
    game = game.expanduser().resolve()
    values = metadata(game)

    required = [
        game / "eboot.bin",
        game / "sce_module" / "libc.prx",
        game / "sce_module" / "libSceFios2.prx",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        fail("missing required plaintext game file(s): " + ", ".join(missing))

    # First Light streams most world data from art/cache/psarc and uses
    # playgo.pgm during install/content setup.  Accept both common dump layouts:
    # directly under app0 or nested under dvdroot_ps4 (runtime_file bridges it).
    def payload(name: str) -> Path | None:
        direct = game / name
        nested = game / "dvdroot_ps4" / name
        return direct if direct.exists() else nested if nested.exists() else None

    art_root = payload("art")
    playgo = payload("playgo.pgm")
    if not art_root:
        print("WARNING: no art/ payload found (also checked dvdroot_ps4/art); boot may reach the engine but cannot load world data.")
    if not playgo:
        print("WARNING: no playgo.pgm found (also checked dvdroot_ps4/playgo.pgm); PlayGo will use the fallback installed profile.")

    out = Path(env.get("FL_OUT_DIR", PORT / "out")).resolve()
    user = Path(env.get("FL_USER_DIR", PORT / "user")).resolve()
    out.mkdir(parents=True, exist_ok=True)
    user.mkdir(parents=True, exist_ok=True)

    # Keep the bootstrap deterministic and title-neutral.  These Bloodborne
    # features must never silently modify the CUSA00575 image.
    # First boot must not inherit Bloodborne-specific launch state from a shell
    # or an old bbport.ini.  Generic GPU/runtime toggles get FL_* opt-ins later.
    for key in (
        "BB_RENDER_RES", "BB_DLC_DIR", "BB_MODS_DIR", "BB_MODS_DISABLE",
        "BB_THD_TOKEN", "BB_THD_ONLINE_ID", "BB_COMMUNITY_SERVER",
        "BB_SHADNET_SERVER", "BB_SHADNET_WEBAPI", "BB_SHADNET_NPID",
        "BB_SHADNET_PASSWORD", "BB_SHADNET_TOKEN",
    ):
        env.pop(key, None)
    env.update(
        BB_GAME_DIR=str(game),
        BB_USER_DIR=str(user),
        BB_GPU_USER_DIR=str(user),
        BB_CONFIG=env.get("FL_CONFIG", str(user / "firstlight.ini")),
        BB_UPSCALER=env.get("FL_UPSCALER", "off"),
        BB_ONLINE="0",
        BB_SOUND_HACK="0",
        BB_RED_ZONE="0",
        BB_FORCE_PATCHES="",
        BB_PATCHES="",
    )
    env.setdefault("BB_VBLANK_HZ", "60")
    env.setdefault("BB_FPS", "30")
    env.setdefault("BB_CONTENT_SKU", "full")
    if args.import_trace is not None:
        if args.import_trace < 0:
            fail("--import-trace must be zero or greater")
        if args.import_trace:
            env["FL_IMPORT_TRACE"]=str(args.import_trace)
        else:
            env.pop("FL_IMPORT_TRACE",None)

    # Development builds use MSYS2's runtime DLLs if they are not copied beside
    # bb-probe.exe yet.
    probe = find_probe()
    if probe and not (probe.parent / "SDL3.dll").is_file() and os.name == "nt":
        clang64 = Path(env.get("MSYS2_ROOT", r"C:\msys64")) / "clang64" / "bin"
        if clang64.is_dir():
            env["PATH"] = f"{clang64}{os.pathsep}{env.get('PATH', '')}"

    print(f"{values.get('TITLE')} | CUSA00575 | v1.00")
    print(f"Game: {game}")
    print(f"Output: {out}")
    print(f"User/logs: {user}")

    run_tool("prepare.py", game, "--out", out, env=env)
    run_python(
        PORT / "tools" / "firstlight_triage.py",
        game,
        "--out",
        out / "firstlight-triage.json",
        env=env,
    )

    module_args = ["libc.prx", "libSceFios2.prx"]
    # Optional title modules can be linked as native guest code without baking
    # them into the port.  "auto" asks the selector to add only modules whose
    # exports satisfy imports that remain after libc/Fios2.
    requested_modules = env.get("FL_LINK_MODULES", "auto")
    if requested_modules.strip().lower() == "auto":
        selection = run_python(
            PORT / "tools" / "firstlight_modules.py",
            game,
            "--json",
            env=env,
            capture=True,
        )
        try:
            selected = json.loads(selection.stdout or "{}").get("selected", [])
        except json.JSONDecodeError as error:
            fail(f"firstlight_modules.py returned invalid JSON: {error}")
        for name in selected:
            if name not in module_args:
                module_args.append(name)
    else:
        extra = [x.strip() for x in requested_modules.replace(",", ";").split(";") if x.strip()]
        for name in extra:
            if name not in module_args:
                module_args.append(name)
    for name in module_args:
        if not (game / "sce_module" / name).is_file():
            fail(f"requested linked module is missing: sce_module/{name}")
    run_tool("link_modules.py", game, "--out", out, "--modules", *module_args, env=env)
    if getattr(sys, "frozen", False):
        print("Import source-coverage audit skipped in portable build (runtime resolution remains authoritative).")
    else:
        run_python(
            PORT / "tools" / "firstlight_coverage.py",
            "--link",
            out / "link.json",
            "--out",
            out / "firstlight-coverage.json",
            env=env,
        )
    run_tool("content_profile.py", game, "--out", out, "--sku", env["BB_CONTENT_SKU"], env=env)
    bootstrap_summary(out, values)

    if args.prepare_only:
        print("Preparation complete; launch skipped (--prepare-only).")
        return 0

    if not probe or not probe.is_file():
        fail("bb-probe.exe not found. Build the firstlight-bootstrap branch with build.sh first.")

    timeout = args.timeout
    if timeout is None:
        timeout = int(env.get("FL_TIMEOUT", env.get("BB_TIMEOUT", "0")) or 0)

    command = [
        str(probe),
        str(out / "boot-linked.bin"),
        "--content-profile",
        str(out / "content.bin"),
        "--app0",
        str(game),
        "--user",
        str(user),
        "--timeout",
        str(timeout),
    ]
    if args.strict_imports:
        command.append("--strict-imports")
    if args.cpu_only:
        command.append("--cpu-only")

    log_path = user / "last_run.log"
    header = (
        f"inFAMOUS First Light native bootstrap\n"
        f"title_id=CUSA00575 version=01.00\n"
        f"game={game}\n"
        f"probe={probe}\n"
        f"started={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"command={subprocess.list2cmdline(command)}\n\n"
    )
    print(f"Launching native guest code. Log: {log_path}")

    if args.console:
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            log.write(header)
            log.flush()
            process = subprocess.Popen(
                command,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="")
                log.write(line)
                log.flush()
            code = process.wait()
        result_code = code
    else:
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            log.write(header)
            log.flush()
            result = subprocess.run(
                command,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags(False),
            )
        result_code = result.returncode

    diagnosis = user / "last_run_diagnosis.json"
    diagnose = run_python(
        PORT / "tools" / "firstlight_log.py",
        log_path,
        "--exit-code",
        result_code,
        "--out",
        diagnosis,
        env=env,
        capture=True,
    )
    if diagnose.stdout:
        print(diagnose.stdout, end="")
    print(f"First Light exited with code {result_code}; log: {log_path}")
    print(f"Boot diagnosis: {diagnosis}")
    return result_code


def frozen_script_dispatch() -> None:
    """Allow the frozen runner to execute the external preparation scripts.

    The packaged build keeps scripts/ and tools/ beside the EXE.  Child runs of
    this same EXE use --script <path> so players do not need a Python install.
    """
    if not getattr(sys, "frozen", False) or len(sys.argv) < 3 or sys.argv[1] != "--script":
        return
    path = Path(sys.argv[2]).resolve()
    try:
        path.relative_to(PORT)
    except ValueError:
        fail(f"refusing to execute script outside the port directory: {path}")
    if not path.is_file() or path.suffix.lower() != ".py":
        fail(f"bootstrap script not found: {path}")
    sys.argv = [str(path), *sys.argv[3:]]
    runpy.run_path(str(path), run_name="__main__")
    raise SystemExit(0)


if __name__ == "__main__":
    try:
        frozen_script_dispatch()
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
