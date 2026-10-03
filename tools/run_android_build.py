#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build Android with bounded, on-disk scratch storage and private output files."""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path

OWNER = ".shittim-android-build-owner"
OWNER_VALUE = "shittim-android-build-v1\n"
INIT_SCRIPT = Path(__file__).resolve().with_name("android_build.init.gradle.kts")
DEFAULT_PROJECT = Path(__file__).resolve().parents[1] / "apps/records-android"
LIFETIME_LOCK = "lifetime.lock"
LIFETIME_STARTED = "lifetime.started"
LAUNCH_STARTED = "launch.started"


class AndroidBuildError(RuntimeError):
    """Only a fixed category, never Gradle arguments or signing values, is public."""


class BuildInterrupted(Exception):
    pass


def validate_arguments(arguments: list[str]) -> list[str]:
    arguments = arguments[1:] if arguments[:1] == ["--"] else arguments
    managed = (
        "--daemon",
        "--no-daemon",
        "--foreground",
        "--stop",
        "--status",
        "--project-cache-dir",
        "--project-dir",
        "--init-script",
        "--gradle-user-home",
        "--build-file",
        "--settings-file",
        "-PshittimAndroidBuildRoot",
        "-Pkotlin.compiler.execution.strategy",
        "-Djava.io.tmpdir",
        "-Dorg.gradle.daemon",
        "-Dorg.gradle.project.shittimAndroidBuildRoot",
    )
    if not arguments or any(
        arg.startswith(managed)
        or (
            arg.startswith("--")
            and any(option.startswith(arg.split("=", 1)[0]) for option in managed)
        )
        or re.match(r"^-(?:I|p|g|b|c)(?:=|[^-]|$)", arg)
        or "java.io.tmpdir" in arg
        or "org.gradle.daemon" in arg
        or "shittimAndroidBuildRoot" in arg
        or "kotlin.compiler.execution.strategy" in arg
        or re.search(r"(?i)(?:password|secret|token)=", arg)
        for arg in arguments
    ):
        raise AndroidBuildError("android_build_arguments_invalid")
    return arguments


def _remove_idle_run(directory: Path, *, wait_seconds: float = 0) -> bool:
    # A JVM that has not reached the init script may already have detached. Its
    # liveness is unknown: retain this one run and require an operator check.
    if (directory / LAUNCH_STARTED).exists() and not (directory / LIFETIME_STARTED).is_file():
        raise AndroidBuildError("android_build_cleanup_needed")
    lock_path = directory / LIFETIME_LOCK
    if lock_path.is_symlink() or not lock_path.is_file():
        raise AndroidBuildError("android_build_cleanup_needed")
    descriptor = os.open(lock_path, os.O_WRONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "wb") as lease:
        metadata = os.fstat(lease.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise AndroidBuildError("android_build_cleanup_needed")
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                # Java FileChannel.lock uses POSIX record locks, not flock.
                fcntl.lockf(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.05)
        # Retain the lease through removal, preventing a competing JVM lease.
        shutil.rmtree(directory)
    return True


def cleanup_stale_runs(root: Path) -> None:
    """Only this tool's marked scratch dirs; never arbitrary cache or worktrees."""
    for candidate in root.glob("run-*"):
        marker = candidate / OWNER
        if (
            candidate.is_symlink()
            or not candidate.is_dir()
            or candidate.stat().st_uid != os.getuid()
            or marker.is_symlink()
            or not marker.is_file()
        ):
            continue
        if (
            marker.stat().st_size == len(OWNER_VALUE)
            and marker.read_bytes() == OWNER_VALUE.encode()
            and not _remove_idle_run(candidate)
        ):
            raise AndroidBuildError("android_build_already_running")


def build_environment(scratch: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for key in (
        "JAVA_TOOL_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "_JAVA_OPTIONS",
        "GRADLE_OPTS",
        "JAVA_OPTS",
    ):
        if "java.io.tmpdir" in environment.get(key, ""):
            raise AndroidBuildError("android_build_temp_override")
    temporary = scratch / "tmp"
    temporary.mkdir(mode=0o700)
    environment.update(TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary))
    option = shlex.quote(f"-Djava.io.tmpdir={temporary}")
    environment["JAVA_TOOL_OPTIONS"] = (
        environment.get("JAVA_TOOL_OPTIONS", "") + " " + option
    ).strip()
    return environment


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    # Stop launcher descendants. A detached single-use Gradle JVM is separately
    # protected by its lifetime lease; never claim killpg stops that JVM.
    deadline = time.monotonic() + 10
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    while time.monotonic() < deadline:
        process.poll()
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=5)


def _gradle(
    command: list[str], *, project: Path, env: dict[str, str], output: Path, lock_descriptor: int
) -> None:
    descriptor = os.open(
        output / "build.log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    process = None
    cancelled = False

    def cancel(_signum: int, _frame: object) -> None:
        # Do not raise during Popen construction and lose a just-started child's PID.
        nonlocal cancelled
        cancelled = True

    try:
        with os.fdopen(descriptor, "wb") as log:
            os.fchmod(log.fileno(), 0o600)
            for sig in previous:
                signal.signal(sig, cancel)
            process = subprocess.Popen(  # noqa: S603 - fixed Wrapper, validated Gradle options.
                command,
                cwd=project,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(lock_descriptor,),
            )
            while True:
                if cancelled:
                    raise BuildInterrupted
                try:
                    result = process.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
            # Stop any residual launcher group. The separate lifetime lease also
            # prevents scratch removal while a detached Gradle JVM shuts down.
            _stop_process(process)
            if result != 0:
                raise AndroidBuildError("android_build_failed")
    except BuildInterrupted, KeyboardInterrupt:
        # Ignore repeated cancellation until the child has finished terminating.
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        if process is not None:
            _stop_process(process)
        raise AndroidBuildError("android_build_interrupted") from None
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def copy_outputs(build: Path, output: Path, *, successful: bool) -> int:
    copied = 0
    for source in build.rglob("*"):
        if source.is_symlink() or not source.is_file():
            continue
        artifact = source.suffix in {".apk", ".aab"} and "outputs" in source.parts
        lint = source.name.startswith("lint-results-") and source.suffix in {
            ".html",
            ".xml",
            ".txt",
        }
        if not lint and not (successful and artifact):
            continue
        relative = source.relative_to(build)
        destination = output / relative.parts[0] / source.name
        if not destination.resolve().is_relative_to(output):
            raise AndroidBuildError("android_build_output_symlink")
        destination.parent.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as target, source.open("rb") as original:
            os.fchmod(target.fileno(), 0o600)
            shutil.copyfileobj(original, target)
        copied += artifact
    return copied


def run_build(*, project: Path, output: Path, arguments: list[str], cache: Path) -> None:
    project, output, cache = project.resolve(), output.resolve(), cache.resolve()
    repository = next(
        (parent for parent in (project, *project.parents) if (parent / ".git").exists()), project
    )
    if (
        not (project / "gradlew").is_file()
        or not INIT_SCRIPT.is_file()
        or output.is_relative_to(repository)
        or cache.is_relative_to(repository)
        or output.is_relative_to(cache)
    ):
        raise AndroidBuildError("android_build_paths_invalid")
    arguments = validate_arguments(arguments)
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (cache / "build.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AndroidBuildError("android_build_already_running") from None
        cleanup_stale_runs(cache)
        with tempfile.TemporaryDirectory(prefix="run-", dir=cache, delete=False) as directory:
            scratch = Path(directory)
            (scratch / OWNER).write_text(OWNER_VALUE)
            (scratch / LIFETIME_LOCK).touch(mode=0o600)
            build = scratch / "build"
            command = [
                str(project / "gradlew"),
                "--no-daemon",
                "--project-cache-dir",
                str(scratch / "project-cache"),
                "--init-script",
                str(INIT_SCRIPT),
                f"-PshittimAndroidBuildRoot={build}",
                "-Pkotlin.compiler.execution.strategy=in-process",
                *arguments,
            ]
            try:
                environment = build_environment(scratch)
                (scratch / LAUNCH_STARTED).touch(mode=0o600)
                # The inherited global lock protects launcher startup after
                # Python SIGKILL; the init-script lease protects detached JVM use.
                _gradle(
                    command,
                    project=project,
                    env=environment,
                    output=output,
                    lock_descriptor=lock.fileno(),
                )
            except BaseException:
                copy_outputs(build, output, successful=False)
                raise
            else:
                copy_outputs(build, output, successful=True)
            finally:
                if not _remove_idle_run(scratch, wait_seconds=3):
                    raise AndroidBuildError("android_build_cleanup_needed")


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    local_project = Path.cwd() / "apps/records-android"
    default_project = local_project if (local_project / "gradlew").is_file() else DEFAULT_PROJECT
    parser.add_argument("--project", type=Path, default=default_project)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("gradle_arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    try:
        run_build(
            project=args.project,
            output=args.output_dir or cache_home / "shittim-chest/android-artifacts",
            arguments=args.gradle_arguments,
            cache=cache_home / "shittim-chest/android-build",
        )
    except AndroidBuildError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("android_build_storage_failed", file=sys.stderr)
        return 1
    print("android_build_complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
