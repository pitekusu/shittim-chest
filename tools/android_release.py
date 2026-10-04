#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Project-specific inputs and checks; upload/commit belong to upload-google-play."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from tools.verify_android_bundle import PACKAGE, digest, verify_bundle

SCOPE = "https://www.googleapis.com/auth/androidpublisher"
PLAY_MAX_VERSION_CODE = 2_100_000_000
PRIVATE_FILES = ("upload-key.p12", "google-services.json", "gradle-build.log")


def client():
    credential_file = Path(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    config = json.loads(credential_file.read_text())
    # Workflow authentication must remain WIF; no service-account key/user ADC fallback.
    if config.get("type") != "external_account" or not config.get(
        "service_account_impersonation_url"
    ):
        raise ValueError("play_requires_federated_service_account")
    credentials, _ = google.auth.load_credentials_from_file(str(credential_file), scopes=[SCOPE])
    return build("androidpublisher", "v3", credentials=credentials, cache_discovery=False)


@contextmanager
def inspection(api):
    # These edits only read inventory and are discarded. Never use this after
    # an unknown upload result: creating an edit can invalidate an unfinished edit.
    identifier = api.edits().insert(packageName=PACKAGE, body={}).execute()["id"]
    try:
        yield {"packageName": PACKAGE, "editId": identifier}
    finally:
        api.edits().delete(packageName=PACKAGE, editId=identifier).execute()


def version_code(value: object) -> int:
    if type(value) is int:
        code = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,9}", value):
        code = int(value)
    else:
        raise ValueError("play_version_code_invalid")
    if not 0 < code <= PLAY_MAX_VERSION_CODE:
        raise ValueError("play_version_code_invalid")
    return code


def write_json(path: Path, value: dict[str, object]) -> None:
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
    with client() as api, inspection(api) as params:
        tracks = api.edits().tracks().list(**params).execute().get("tracks", [])
        bundles = api.edits().bundles().list(**params).execute().get("bundles", [])
        apks = api.edits().apks().list(**params).execute().get("apks", [])
    if not any(t["track"] == "internal" for t in tracks):
        raise ValueError("play_internal_track_missing")
    codes = [
        version_code(c)
        for t in tracks
        for r in t.get("releases", [])
        for c in r.get("versionCodes", [])
    ]
    codes += [version_code(a["versionCode"]) for a in bundles + apks]
    # A failed, uncommitted upload may not be listed; the operator supplies a floor
    # after checking its side effects, instead of retrying/reusing that version.
    floor = version_code(os.environ.get("MINIMUM_VERSION_CODE", "1"))
    code = version_code(max(max(codes, default=0) + 1, floor))
    # Keep the terminal value readable in inventory, but never publish it: Play
    # would require a larger value for every future update and none could exist.
    if code == PLAY_MAX_VERSION_CODE:
        raise ValueError("play_version_code_exhausted")
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


def readback(state: Path) -> None:
    # Run only after the Action succeeds. This verifies the submitted artifact,
    # not review completion or when a particular device can install the update.
    result = json.loads((state / "verification.json").read_text())
    with client() as api, inspection(api) as params:
        track = api.edits().tracks().get(**params, track="internal").execute()
        bundles = api.edits().bundles().list(**params).execute().get("bundles", [])
    if not (
        any(
            r.get("status") == "completed"
            and str(result["versionCode"]) in r.get("versionCodes", [])
            for r in track.get("releases", [])
        )
        and any(
            b["versionCode"] == result["versionCode"] and b.get("sha256") == result["bundleSha256"]
            for b in bundles
        )
    ):
        raise ValueError("play_submission_unconfirmed_check_console")
    write_json(state / "receipt.json", {**result, "status": "submitted", "verified": True})
    print(f"play_internal_submission_verified versionCode={result['versionCode']}")


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("materialize", "preflight", "verify", "readback", "cleanup")
    )
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--bundletool", type=Path)
    args = parser.parse_args()
    try:
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
            {"materialize": materialize, "preflight": preflight, "readback": readback}[args.action](
                state
            )
    except HttpError as error:
        print(f"play_http_{int(error.resp.status)}", file=sys.stderr)
        return 1
    except Exception as error:
        # API response bodies/credential details must not escape into CI logs.
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
