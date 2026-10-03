#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Small Play-internal release boundary; authentication is delegated to google-auth/GPP."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import google.auth
from google.auth.transport.requests import AuthorizedSession

from tools.verify_android_bundle import PACKAGE, digest, verify_bundle

BASE = f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{PACKAGE}"
SCOPE = "https://www.googleapis.com/auth/androidpublisher"
PRIVATE_FILES = ("upload-key.p12", "google-services.json", "publish.log", "gradle-build.log")


def request(client: AuthorizedSession, method: str, path: str) -> dict[str, Any]:
    response = client.request(
        method,
        BASE + path,
        timeout=60,
        allow_redirects=False,
    )
    if not 200 <= response.status_code < 300:
        raise ValueError(f"play_http_{response.status_code}")
    value = response.json() if response.content else {}
    if not isinstance(value, dict):
        raise ValueError("play_response_invalid")
    return value


def client() -> AuthorizedSession:
    credential_file = Path(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    if credential_file.is_symlink() or credential_file.stat().st_size > 64 * 1024:
        raise ValueError("play_credentials_invalid")
    # Never fall back to a long-lived service-account key or developer user ADC.
    config = json.loads(credential_file.read_text())
    if config.get("type") != "external_account" or not config.get(
        "service_account_impersonation_url"
    ):
        raise ValueError("play_requires_federated_service_account")
    credentials, _ = google.auth.load_credentials_from_file(str(credential_file), scopes=[SCOPE])
    # Refresh before sending, but never resend a commit after an HTTP 401.
    return AuthorizedSession(credentials, max_refresh_attempts=0)


@contextmanager
def edit(api: AuthorizedSession):
    identifier = request(api, "POST", "/edits").get("id", "")
    if not isinstance(identifier, str) or re.fullmatch(r"[A-Za-z0-9_-]+", identifier) is None:
        raise ValueError("play_edit_invalid")
    try:
        yield f"/edits/{identifier}"
    finally:
        # Inspection edits never upload or commit and can be safely discarded.
        request(api, "DELETE", f"/edits/{identifier}")


def version_code(value: object) -> int:
    if type(value) is int:
        code = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,9}", value):
        code = int(value)
    else:
        raise ValueError("play_version_code_invalid")
    if not 0 < code <= 2_100_000_000:
        raise ValueError("play_version_code_invalid")
    return code


def inventory(
    api: AuthorizedSession, path: str
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    tracks = request(api, "GET", path + "/tracks").get("tracks", [])
    bundles = request(api, "GET", path + "/bundles").get("bundles", [])
    apks = request(api, "GET", path + "/apks").get("apks", [])
    if not all(isinstance(items, list) for items in (tracks, bundles, apks)):
        raise ValueError("play_inventory_invalid")
    codes = [
        version_code(c)
        for t in tracks
        for r in t.get("releases", [])
        for c in r.get("versionCodes", [])
    ]
    codes += [version_code(item["versionCode"]) for item in bundles + apks]
    if not any(track.get("track") == "internal" for track in tracks):
        raise ValueError("play_internal_track_missing")
    return max(codes, default=0), tracks, bundles


def published(
    tracks: list[dict[str, Any]], bundles: list[dict[str, Any]], code: int, checksum: str
) -> bool:
    matching = [b for b in bundles if version_code(b["versionCode"]) == code]
    if not matching or any(b.get("sha256") != checksum for b in matching):
        return False
    return any(
        r.get("status") == "completed" and str(code) in r.get("versionCodes", [])
        for t in tracks
        if t.get("track") == "internal"
        for r in t.get("releases", [])
    )


def write_json(path: Path, value: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream)


def preflight(state: Path) -> None:
    if os.environ.get("GITHUB_RUN_ATTEMPT") != "1":
        raise ValueError("play_rerun_requires_track_inspection")
    sha = os.environ["GITHUB_SHA"]
    if (
        re.fullmatch(r"[0-9a-f]{40}", sha) is None
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
    ):
        raise ValueError("play_requires_main_sha")
    with client() as api, edit(api) as path:
        maximum, _, _ = inventory(api, path)
    code = version_code(maximum + 1)
    write_json(state / "plan.json", {"sha": sha, "versionCode": code})
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
        stream.write(f"version_code={code}\n")
    print(f"play_internal_preflight_ready versionCode={code}")


def materialize(state: Path) -> None:
    state.mkdir(mode=0o700)
    inputs = {
        "upload-key.p12": base64.b64decode(os.environ["UPLOAD_KEY_BASE64"], validate=True),
        "google-services.json": os.environ["FIREBASE_CLIENT_CONFIG"].encode(),
    }
    if (
        not all(inputs.values())
        or not os.environ.get("UPLOAD_STORE_PASSWORD")
        or not os.environ.get("UPLOAD_KEY_ALIAS")
    ):
        raise ValueError("android_release_inputs_missing")
    firebase = json.loads(inputs["google-services.json"])
    if not any(
        c.get("client_info", {}).get("android_client_info", {}).get("package_name") == PACKAGE
        for c in firebase.get("client", [])
    ):
        raise ValueError("android_firebase_package_invalid")
    for name, content in inputs.items():
        with (state / name).open("xb") as stream:
            stream.write(content)


def verify(state: Path, bundletool: Path) -> None:
    plan = json.loads((state / "plan.json").read_text())
    bundle = Path("apps/records-android/app/build/outputs/bundle/release/app-release.aab")
    result = verify_bundle(
        bundle,
        bundletool,
        code=version_code(plan["versionCode"]),
        sha=plan["sha"],
        certificate=os.environ["ANDROID_UPLOAD_CERT_SHA256"],
    )
    artifact = state / "verified"
    artifact.mkdir(mode=0o700)
    shutil.copyfile(bundle, artifact / "app-release.aab")
    if digest(artifact / "app-release.aab") != result["bundleSha256"]:
        raise ValueError("android_bundle_copy_changed")
    write_json(state / "verification.json", result)
    print("android_bundle_verified")


def publish(state: Path) -> None:
    result = json.loads((state / "verification.json").read_text())
    bundle = state / "verified/app-release.aab"
    code, checksum = version_code(result["versionCode"]), result["bundleSha256"]
    if (
        result["sha"] != os.environ["GITHUB_SHA"]
        or digest(bundle) != checksum
        or result["track"] != "internal"
    ):
        raise ValueError("android_verified_artifact_changed")
    with client() as api:
        with edit(api) as path:
            maximum, tracks, bundles = inventory(api, path)
        if not published(tracks, bundles, code, checksum):
            if maximum >= code:
                raise ValueError("play_version_code_conflict")
            gpp = Path("apps/records-android/app/build/gpp")
            if any(
                (gpp / f"{PACKAGE}.{suffix}").exists() for suffix in ("txt", "skipped", "commit")
            ):
                raise ValueError("play_stale_edit_do_not_resend")
            # GPP owns upload/staging, with commit=false. Never retry an unknown
            # upload response; only this boundary commits the verified edit once.
            with (state / "publish.log").open("xb") as log:
                staged = subprocess.run(  # noqa: S603 - fixed Wrapper and internal-only GPP task.
                    [
                        "./gradlew",
                        "--no-daemon",
                        ":app:publishReleaseBundle",
                        f"-PshittimAndroidPublishArtifactDir={state / 'verified'}",
                        f"-PshittimAndroidPublishReleaseName=0.0.{code}",
                    ],
                    cwd="apps/records-android",
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=600,
                    check=False,
                )
            if staged.returncode != 0:
                raise ValueError("play_stage_failed_do_not_resend")
            # GPP 4.1.1's documented --no-commit state is private, never an artifact.
            edit_file = gpp / f"{PACKAGE}.txt"
            if not (gpp / f"{PACKAGE}.skipped").is_file() or edit_file.is_symlink():
                raise ValueError("play_staged_edit_missing")
            identifier = edit_file.read_text().strip()
            if re.fullmatch(r"[A-Za-z0-9_-]+", identifier) is None:
                raise ValueError("play_staged_edit_invalid")
            path = f"/edits/{identifier}"
            _, staged_tracks, staged_bundles = inventory(api, path)
            if not published(staged_tracks, staged_bundles, code, checksum) or (
                [t for t in staged_tracks if t.get("track") != "internal"]
                != [t for t in tracks if t.get("track") != "internal"]
            ):
                raise ValueError("play_staged_content_invalid")
            write_json(state / "commit-attempt.json", {**result, "phase": "commit_started"})
            # Commit may succeed even if its response is lost. Suppress only to
            # perform authoritative reads; success still requires the same hash.
            with suppress(Exception):
                # Default commit behavior cancels existing reviews. Neither cancel
                # those nor send pending Console changes for review from this job.
                request(
                    api,
                    "POST",
                    path + ":commit?changesInReviewBehavior=ERROR_IF_IN_REVIEW"
                    "&changesNotSentForReview=true",
                )
            for attempt in range(4):
                with edit(api) as path:
                    _, tracks, bundles = inventory(api, path)
                if published(tracks, bundles, code, checksum):
                    break
                if attempt < 3:
                    time.sleep(5)
            else:
                raise ValueError("play_publish_unconfirmed_do_not_resend")
    write_json(state / "receipt.json", {**result, "status": "completed", "verified": True})
    print(f"play_internal_published_and_verified versionCode={code}")


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("materialize", "preflight", "verify", "publish", "cleanup")
    )
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--bundletool", type=Path)
    args = parser.parse_args()
    try:
        # The directory is a fixed private child of the ephemeral runner temp.
        state = args.state.resolve()
        if state != Path(os.environ["RUNNER_TEMP"]).resolve() / "android-release":
            raise ValueError("android_release_state_path_invalid")
        if args.action == "cleanup":
            for name in PRIVATE_FILES:
                (state / name).unlink(missing_ok=True)
        elif args.action == "verify":
            if args.bundletool is None:
                raise ValueError("android_bundletool_missing")
            verify(state, args.bundletool)
        else:
            {"materialize": materialize, "preflight": preflight, "publish": publish}[args.action](
                state
            )
    except Exception as error:
        # No response/credential/Gradle output or arbitrary exception string in CI.
        category = (
            str(error)
            if isinstance(error, ValueError) and re.fullmatch(r"[a-z_0-9]+", str(error))
            else "android_release_failed"
        )
        print(category, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
