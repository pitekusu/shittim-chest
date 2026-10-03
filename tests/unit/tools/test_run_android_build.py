"""Owned scratch lifecycle, private output, and managed Gradle boundaries."""

from __future__ import annotations

import runpy
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from tools import run_android_build as runner


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    (source / ".git").write_text("invented worktree marker")
    project = source / "apps/records-android"
    project.mkdir(parents=True)
    (project / "gradlew").write_text("invented wrapper")
    return project, tmp_path / "artifacts", tmp_path / "cache"


@pytest.mark.parametrize("failure", [None, runner.AndroidBuildError, KeyboardInterrupt])
def test_run_scratch_removed_on_success_failure_and_cancellation(paths, monkeypatch, failure):
    project, output, cache = paths
    observed = []

    def gradle(command, *, project, env, output, lock_descriptor):
        build = Path(
            next(
                arg.split("=", 1)[1]
                for arg in command
                if arg.startswith("-PshittimAndroidBuildRoot=")
            )
        )
        observed.append(build.parent)
        (build.parent / runner.LIFETIME_STARTED).touch()
        assert command[1:3] == ["--no-daemon", "--project-cache-dir"]
        assert "-Pkotlin.compiler.execution.strategy=in-process" in command
        assert env["TMPDIR"] == env["TMP"] == env["TEMP"] == str(build.parent / "tmp")
        assert str(build.parent / "tmp") in env["JAVA_TOOL_OPTIONS"]
        assert env["SHITTIM_ANDROID_UPLOAD_STORE_PASSWORD"] == "invented"  # noqa: S105
        bundle = build / "app/outputs/bundle/release/app-release.aab"
        bundle.parent.mkdir(parents=True)
        bundle.write_bytes(b"invented signed artifact")
        lint = build / "app/reports/lint-results-release.xml"
        lint.parent.mkdir(parents=True)
        lint.write_text("invented lint report")
        if failure:
            raise failure("android_build_failed")

    monkeypatch.setenv("SHITTIM_ANDROID_UPLOAD_STORE_PASSWORD", "invented")
    monkeypatch.setattr(runner, "_gradle", gradle)
    if failure:
        with pytest.raises(failure):
            runner.run_build(
                project=project, output=output, cache=cache, arguments=[":app:bundleRelease"]
            )
    else:
        runner.run_build(
            project=project, output=output, cache=cache, arguments=[":app:bundleRelease"]
        )
    assert observed and not observed[0].exists()
    assert not list(cache.glob("run-*"))
    assert (output / "app/lint-results-release.xml").exists()
    assert (output / "app/app-release.aab").exists() == (failure is None)
    assert (project / "gradlew").read_text() == "invented wrapper"


def test_stale_cleanup_requires_exact_owner_marker_and_never_follows_links(tmp_path):
    root = tmp_path / "scratch"
    root.mkdir()
    owned, unknown, mismatch = [root / name for name in ("run-owned", "run-other", "run-wrong")]
    for directory in (owned, unknown, mismatch):
        directory.mkdir()
    (owned / runner.OWNER).write_text(runner.OWNER_VALUE)
    (owned / runner.LIFETIME_LOCK).touch()
    (mismatch / runner.OWNER).write_text("someone else's directory")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / runner.OWNER).write_text(runner.OWNER_VALUE)
    (root / "run-link").symlink_to(outside, target_is_directory=True)
    marker_link = root / "run-marker-link"
    marker_link.mkdir()
    (marker_link / runner.OWNER).symlink_to(outside / runner.OWNER)
    runner.cleanup_stale_runs(root)
    assert not owned.exists()
    assert unknown.exists() and mismatch.exists() and outside.exists() and marker_link.exists()
    assert (root / "run-link").is_symlink()


def test_live_lock_rejects_competing_run_without_cleaning_its_scratch(paths):
    project, output, cache = paths
    cache.mkdir()
    live = cache / "run-live"
    live.mkdir()
    (live / runner.OWNER).write_text(runner.OWNER_VALUE)
    with (cache / "build.lock").open("a+b") as lock:
        runner.fcntl.flock(lock, runner.fcntl.LOCK_EX | runner.fcntl.LOCK_NB)
        with pytest.raises(runner.AndroidBuildError, match="android_build_already_running"):
            runner.run_build(
                project=project, output=output, cache=cache, arguments=[":app:assembleDebug"]
            )
    assert live.exists()


@contextmanager
def held_lifetime_lease(path):
    # POSIX locks are process-owned: use a real child instead of a same-process
    # mock so this exercises the Java FileChannel/Python lockf interoperability.
    with subprocess.Popen(  # noqa: S603 - fixed test program and pytest-owned path.
        [
            sys.executable,
            "-c",
            "import fcntl,sys; f=open(sys.argv[1],'r+'); fcntl.lockf(f,fcntl.LOCK_EX); "
            "print('ready',flush=True); sys.stdin.readline()",
            str(path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdout and process.stdout.readline() == "ready\n"
        try:
            yield
        finally:
            process.communicate(input="\n", timeout=5)


def test_detached_jvm_lease_prevents_cleanup_and_next_build_until_exit(paths):
    project, output, cache = paths
    cache.mkdir()
    live = cache / "run-detached"
    live.mkdir()
    (live / runner.OWNER).write_text(runner.OWNER_VALUE)
    (live / runner.LIFETIME_LOCK).touch()
    (live / runner.LAUNCH_STARTED).touch()
    (live / runner.LIFETIME_STARTED).touch()
    with held_lifetime_lease(live / runner.LIFETIME_LOCK):
        assert not runner._remove_idle_run(live)
        with pytest.raises(runner.AndroidBuildError, match="android_build_already_running"):
            runner.run_build(
                project=project, output=output, cache=cache, arguments=[":app:assembleDebug"]
            )
        assert live.exists() and list(cache.glob("run-*")) == [live]
    runner.cleanup_stale_runs(cache)
    assert not live.exists()


def test_failure_before_init_retains_uncertain_run_and_refuses_another(paths, monkeypatch):
    project, output, cache = paths

    def gradle(*args, **kwargs):
        raise runner.AndroidBuildError("android_build_interrupted")

    monkeypatch.setattr(runner, "_gradle", gradle)
    for _attempt in range(2):
        with pytest.raises(runner.AndroidBuildError, match="android_build_cleanup_needed"):
            runner.run_build(
                project=project, output=output, cache=cache, arguments=[":app:assembleDebug"]
            )
        assert len(list(cache.glob("run-*"))) == 1


def test_prelaunch_environment_failure_is_cleaned_without_uncertain_state(paths, monkeypatch):
    project, output, cache = paths
    monkeypatch.setenv("JAVA_TOOL_OPTIONS", "-Djava.io.tmpdir=/tmp/invented")
    with pytest.raises(runner.AndroidBuildError, match="android_build_temp_override"):
        runner.run_build(
            project=project, output=output, cache=cache, arguments=[":app:assembleDebug"]
        )
    assert not list(cache.glob("run-*"))


@pytest.mark.parametrize(
    "argument",
    [
        "--daemon",
        "--project-cache-dir=/tmp/other",
        "--project-cache-d=/tmp/other",
        "-p/tmp/other",
        "--init-script=other",
        "-PshittimAndroidBuildRoot=/tmp/other",
        "-Pkotlin.compiler.execution.strategy=daemon",
        "-Dorg.gradle.project.kotlin.compiler.execution.strategy=out-of-process",
        "shittimAndroidBuildRoot=/tmp/other",
        "-Djava.io.tmpdir=/tmp/other",
        "-Dorg.gradle.jvmargs=-Djava.io.tmpdir=/tmp/other",
        "-Ppassword=invented",
    ],
)
def test_managed_overrides_and_secret_arguments_rejected(argument):
    with pytest.raises(runner.AndroidBuildError, match="android_build_arguments_invalid"):
        runner.validate_arguments([argument, ":app:assembleDebug"])


def test_output_and_scratch_cannot_be_inside_source_repository(paths):
    project, output, cache = paths
    for unsafe_output, unsafe_cache in (
        (project / "output", cache),
        (output, project / "cache"),
        (cache / "run-output", cache),
    ):
        with pytest.raises(runner.AndroidBuildError, match="android_build_paths_invalid"):
            runner.run_build(
                project=project,
                output=unsafe_output,
                cache=unsafe_cache,
                arguments=[":app:assembleDebug"],
            )


def test_cancellation_stops_child_before_return_and_does_not_leak_arguments(tmp_path, monkeypatch):
    actions = []

    class Process:
        pid = 123456

        def wait(self, *, timeout):
            actions.append("wait")
            raise runner.BuildInterrupted

    def start(*args, **kwargs):
        assert kwargs["start_new_session"] is True and kwargs["pass_fds"] == (987654,)
        return Process()

    monkeypatch.setattr(runner.subprocess, "Popen", start)
    monkeypatch.setattr(runner, "_stop_process", lambda process: actions.append("stopped"))
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    with pytest.raises(runner.AndroidBuildError, match=r"^android_build_interrupted$"):
        runner._gradle(
            ["wrapper", "invented-private-value"],
            project=tmp_path,
            env={},
            output=tmp_path,
            lock_descriptor=987654,
        )
    assert actions == ["wait", "stopped"]
    assert before == {sig: signal.getsignal(sig) for sig in before}
    assert (tmp_path / "build.log").stat().st_mode & 0o077 == 0


def test_cancellation_during_process_start_keeps_handle_to_stop_child(tmp_path, monkeypatch):
    stopped = []
    child = object()

    def start(*args, **kwargs):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        return child

    monkeypatch.setattr(runner.subprocess, "Popen", start)
    monkeypatch.setattr(runner, "_stop_process", stopped.append)
    with pytest.raises(runner.AndroidBuildError, match="android_build_interrupted"):
        runner._gradle(
            ["wrapper"], project=tmp_path, env={}, output=tmp_path, lock_descriptor=987654
        )
    assert stopped == [child]


@pytest.mark.parametrize("status", [0, 1])
def test_launcher_exit_stops_remaining_group_before_success_or_failure(
    tmp_path, monkeypatch, status
):
    actions = []

    class Process:
        def wait(self, *, timeout):
            actions.append("launcher_exited")
            return status

    child = Process()
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(runner, "_stop_process", lambda process: actions.append("group_stopped"))
    if status:
        with pytest.raises(runner.AndroidBuildError, match=r"^android_build_failed$"):
            runner._gradle(
                ["wrapper"], project=tmp_path, env={}, output=tmp_path, lock_descriptor=987654
            )
    else:
        runner._gradle(
            ["wrapper"], project=tmp_path, env={}, output=tmp_path, lock_descriptor=987654
        )
    assert actions == ["launcher_exited", "group_stopped"]


def test_finished_process_group_is_immediate_without_sleep(monkeypatch):
    actions = []

    class Process:
        pid = 123456

        def poll(self):
            return 0

        def wait(self, *, timeout):
            actions.append("reaped")
            return 0

    def gone(*args):
        raise ProcessLookupError

    monkeypatch.setattr(runner.os, "killpg", gone)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: pytest.fail("unexpected waiting"))
    runner._stop_process(cast(Any, Process()))
    assert actions == ["reaped"]


def test_fixed_artifact_names_overwrite_and_do_not_copy_other_build_data(tmp_path):
    build, output = tmp_path / "build", tmp_path / "output"
    output.mkdir()
    binary = build / "app/outputs/apk/debug/app-debug.apk"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"first")
    (build / "app/private.bin").write_bytes(b"not an output")
    assert runner.copy_outputs(build, output, successful=True) == 1
    binary.write_bytes(b"second")
    assert runner.copy_outputs(build, output, successful=True) == 1
    copied = output / "app/app-debug.apk"
    assert copied.read_bytes() == b"second" and copied.stat().st_mode & 0o077 == 0
    assert len(list(output.rglob("*.*"))) == 1


@pytest.mark.parametrize(
    "environment_key",
    ["JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "GRADLE_OPTS", "JAVA_OPTS"],
)
def test_java_override_environment_rejected_before_gradle(tmp_path, monkeypatch, environment_key):
    monkeypatch.setenv(environment_key, "-Djava.io.tmpdir=/tmp/invented")
    with pytest.raises(runner.AndroidBuildError, match="android_build_temp_override"):
        runner.build_environment(tmp_path)


def test_artifact_subdirectory_symlink_cannot_escape_output(tmp_path):
    build, output, outside = tmp_path / "build", tmp_path / "output", tmp_path / "outside"
    output.mkdir()
    outside.mkdir()
    (output / "app").symlink_to(outside, target_is_directory=True)
    binary = build / "app/outputs/apk/debug/app-debug.apk"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"invented artifact")
    with pytest.raises(runner.AndroidBuildError, match="android_build_output_symlink"):
        runner.copy_outputs(build, output, successful=True)
    assert not list(outside.iterdir())


@pytest.mark.parametrize("in_checkout", [True, False])
def test_cli_default_prefers_current_checkout_and_otherwise_source_default(
    paths, monkeypatch, in_checkout
):
    project, output, _cache = paths
    monkeypatch.chdir(project.parents[1] if in_checkout else output.parent)
    monkeypatch.setattr(runner.sys, "argv", ["runner", "--", ":app:assembleDebug"])
    monkeypatch.setattr(runner.os, "umask", lambda _mask: None)
    observed = []
    monkeypatch.setattr(runner, "run_build", lambda **kwargs: observed.append(kwargs))
    assert runner.main() == 0
    assert observed[0]["project"] == (project if in_checkout else runner.DEFAULT_PROJECT)
    assert observed[0]["arguments"] == ["--", ":app:assembleDebug"]


def test_cli_symlink_resolves_init_script_next_to_installed_runner(tmp_path):
    entry = tmp_path / "shittim-android-build"
    entry.symlink_to(Path(runner.__file__))
    namespace = runpy.run_path(str(entry), run_name="symlinked_android_build")
    assert namespace["INIT_SCRIPT"] == Path(runner.__file__).resolve().with_name(
        "android_build.init.gradle.kts"
    )
    assert namespace["INIT_SCRIPT"].is_file()
