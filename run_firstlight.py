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

PORT = Path(__file__).resolve().parent
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


def run_tool(script: str, *args: object, env: dict[str, str]) -> None:
    command = [sys.executable, str(SCRIPTS / script), *map(str, args)]
    print("+", subprocess.list2cmdline(command))
    result = subprocess.run(command, env=env)
    if result.returncode:
        fail(f"{script} failed with exit code {result.returncode}")


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
    if version not in (None, "01.00") and app_ver not in (None, "01.00"):
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

    out = Path(env.get("FL_OUT_DIR", PORT / "out")).resolve()
    user = Path(env.get("FL_USER_DIR", PORT / "user")).resolve()
    out.mkdir(parents=True, exist_ok=True)
    user.mkdir(parents=True, exist_ok=True)

    # Keep the bootstrap deterministic and title-neutral.  These Bloodborne
    # features must never silently modify the CUSA00575 image.
    env.update(
        BB_GAME_DIR=str(game),
        BB_USER_DIR=str(user),
        BB_GPU_USER_DIR=str(user),
        BB_ONLINE="0",
        BB_SOUND_HACK="0",
        BB_RED_ZONE="0",
        BB_FORCE_PATCHES="",
        BB_PATCHES="",
    )
    env.setdefault("BB_VBLANK_HZ", "60")
    env.setdefault("BB_FPS", "30")
    env.setdefault("BB_CONTENT_SKU", "full")

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
    subprocess.run(
        [sys.executable, str(PORT / "tools" / "firstlight_triage.py"), str(game), "--out", str(out / "firstlight-triage.json")],
        env=env,
        check=True,
    )

    module_args = ["libc.prx", "libSceFios2.prx"]
    extra = [x.strip() for x in env.get("FL_LINK_MODULES", "").replace(",", ";").split(";") if x.strip()]
    for name in extra:
        if name not in module_args:
            module_args.append(name)
    for name in module_args:
        if not (game / "sce_module" / name).is_file():
            fail(f"requested linked module is missing: sce_module/{name}")
    run_tool("link_modules.py", game, "--out", out, "--modules", *module_args, env=env)
    coverage_cmd = [
        sys.executable,
        str(PORT / "tools" / "firstlight_coverage.py"),
        "--link",
        str(out / "link.json"),
        "--out",
        str(out / "firstlight-coverage.json"),
    ]
    print("+", subprocess.list2cmdline(coverage_cmd))
    subprocess.run(coverage_cmd, env=env, check=True)
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
        log_path.write_text(header, encoding="utf-8")
        result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL)
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

    print(f"First Light exited with code {result.returncode}; log: {log_path}")
    return result.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
