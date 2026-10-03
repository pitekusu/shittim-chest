"""Exercise Play publishing boundaries with only synthetic files and API stubs."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.auth.transport.requests import AuthorizedSession
from tools import android_release as release

SHA = "a" * 40


@pytest.mark.parametrize("value", (1, "1", 2_100_000_000, "2100000000"))
def test_version_codes_accept_only_play_integers(value: object) -> None:
    assert release.version_code(value) == int(str(value))


@pytest.mark.parametrize("value", (True, False, 0, -1, 1.0, None, "01", "1e3", 2_100_000_001))
def test_version_codes_reject_boolean_and_out_of_range_values(value: object) -> None:
    with pytest.raises(ValueError, match="play_version_code_invalid"):
        release.version_code(value)


@pytest.mark.parametrize(
    "track_code,bundle_code,apk_code", ((41, 28, 17), (13, 51, 17), (13, 28, 62))
)
def test_inventory_uses_every_track_bundle_and_apk(
    monkeypatch: pytest.MonkeyPatch, track_code: int, bundle_code: int, apk_code: int
) -> None:
    responses = {
        "/edits/read/tracks": {
            "tracks": [
                {"track": "internal", "releases": [{"versionCodes": ["1"]}]},
                {"track": "production", "releases": [{"versionCodes": [str(track_code)]}]},
            ]
        },
        "/edits/read/bundles": {"bundles": [{"versionCode": bundle_code}]},
        "/edits/read/apks": {"apks": [{"versionCode": apk_code}]},
    }
    request = MagicMock(side_effect=lambda _api, _method, path: responses[path])
    monkeypatch.setattr(release, "request", request)

    maximum, _, _ = release.inventory(MagicMock(), "/edits/read")

    assert maximum == max(track_code, bundle_code, apk_code)
    assert [call.args[2] for call in request.call_args_list] == list(responses)


@pytest.mark.parametrize(
    "credential_type", ("service_account", "authorized_user", "external_account")
)
def test_credentials_cannot_fall_back_to_long_lived_key_or_user_adc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, credential_type: str
) -> None:
    credential_file = tmp_path / "synthetic-credentials.json"
    credential_file.write_text(json.dumps({"type": credential_type}))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(credential_file))
    load = MagicMock()
    monkeypatch.setattr(release.google.auth, "load_credentials_from_file", load)

    with pytest.raises(ValueError, match="play_requires_federated_service_account"):
        release.client()

    load.assert_not_called()


def test_federated_client_scopes_play_and_disables_response_driven_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential_file = tmp_path / "synthetic-federation.json"
    credential_file.write_text(
        json.dumps(
            {
                "type": "external_account",
                "service_account_impersonation_url": "https://iamcredentials.googleapis.com/fixture",
            }
        )
    )
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(credential_file))
    credentials = MagicMock()
    load = MagicMock(return_value=(credentials, None))
    session = MagicMock()
    monkeypatch.setattr(release.google.auth, "load_credentials_from_file", load)
    monkeypatch.setattr(release, "AuthorizedSession", session)

    assert release.client() is session.return_value

    load.assert_called_once_with(str(credential_file), scopes=[release.SCOPE])
    session.assert_called_once_with(credentials, max_refresh_attempts=0)


def test_request_has_fixed_api_origin_and_no_redirects() -> None:
    api = MagicMock(spec=AuthorizedSession)
    api.request.return_value.status_code = 200
    api.request.return_value.content = b"{}"
    api.request.return_value.json.return_value = {"tracks": []}

    assert release.request(api, "GET", "/edits/read/tracks") == {"tracks": []}
    api.request.assert_called_once_with(
        "GET", release.BASE + "/edits/read/tracks", timeout=60, allow_redirects=False
    )


@pytest.mark.parametrize(
    "status,body,category", ((302, {}, "play_http_302"), (200, [], "play_response_invalid"))
)
def test_request_rejects_redirects_and_non_object_responses(
    status: int, body: object, category: str
) -> None:
    api = MagicMock(spec=AuthorizedSession)
    api.request.return_value.status_code = status
    api.request.return_value.content = b"synthetic response"
    api.request.return_value.json.return_value = body

    with pytest.raises(ValueError, match=category):
        release.request(api, "GET", "/edits/read/tracks")


def test_rerun_stops_before_remote_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    client = MagicMock()
    monkeypatch.setattr(release, "client", client)

    with pytest.raises(ValueError, match="play_rerun_requires_track_inspection"):
        release.preflight(tmp_path)

    client.assert_not_called()


@pytest.mark.parametrize(
    "missing", ("UPLOAD_STORE_PASSWORD", "UPLOAD_KEY_ALIAS", "FIREBASE_CLIENT_CONFIG")
)
def test_missing_release_inputs_cannot_materialize_private_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    inputs = {
        "UPLOAD_KEY_BASE64": base64.b64encode(b"synthetic keystore fixture").decode(),
        "UPLOAD_STORE_PASSWORD": str(12345678),
        "UPLOAD_KEY_ALIAS": "synthetic-upload",
        "FIREBASE_CLIENT_CONFIG": json.dumps(
            {
                "client": [
                    {"client_info": {"android_client_info": {"package_name": release.PACKAGE}}}
                ]
            }
        ),
    }
    for name, value in inputs.items():
        monkeypatch.setenv(name, "" if name == missing else value)
    state = tmp_path / "private-state"

    with pytest.raises(ValueError, match="android_release_inputs_missing"):
        release.materialize(state)

    assert not list(state.iterdir())


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_SHA", SHA)
    state = tmp_path / "android-release"
    (state / "verified").mkdir(parents=True)
    bundle = state / "verified/app-release.aab"
    bundle.write_bytes(b"synthetic already-verified AAB fixture")
    (state / "verification.json").write_text(
        json.dumps(
            {
                "sha": SHA,
                "versionCode": 7,
                "bundleSha256": release.digest(bundle),
                "track": "internal",
            }
        )
    )
    (tmp_path / "apps/records-android/app/build/gpp").mkdir(parents=True)
    return state


class PlayStub:
    """Expose staged and committed snapshots without contacting a real service."""

    def __init__(self, state: Path) -> None:
        self.checksum = release.digest(state / "verified/app-release.aab")
        self.calls: list[tuple[str, str]] = []
        self.committed = False
        self.commit_error = False
        self.confirm_commit = True
        self.conflict = False
        self.change_other_track = False
        self.reads = 0

    def request(self, _api: AuthorizedSession, method: str, path: str) -> dict[str, Any]:
        self.calls.append((method, path))
        if method == "POST" and path == "/edits":
            self.reads += 1
            return {"id": f"read-{self.reads}"}
        if method == "POST" and ":commit?" in path:
            self.committed = self.confirm_commit
            if self.commit_error:
                raise TimeoutError("synthetic transport failure")
            return {}
        if method == "DELETE":
            return {}
        staged = path.startswith("/edits/gpp-new-edit/")
        ready = staged or self.committed
        code = 7 if ready or self.conflict else 6
        if path.endswith("/tracks"):
            return {
                "tracks": [
                    {
                        "track": "internal",
                        "releases": [{"status": "completed", "versionCodes": [str(code)]}],
                    },
                    {
                        "track": "production",
                        "releases": [
                            {"versionCodes": ["3" if staged and self.change_other_track else "2"]}
                        ],
                    },
                ]
            }
        if path.endswith("/bundles"):
            return {
                "bundles": [{"versionCode": code, "sha256": self.checksum if ready else "0" * 64}]
            }
        if path.endswith("/apks"):
            return {"apks": []}
        raise AssertionError(f"unexpected synthetic API operation: {method} {path}")


@pytest.fixture
def publishing(state: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[PlayStub, MagicMock]:
    stub = PlayStub(state)
    api = MagicMock(spec=AuthorizedSession)
    api.__enter__.return_value = api
    monkeypatch.setattr(release, "client", lambda: api)
    monkeypatch.setattr(release, "request", stub.request)
    monkeypatch.setattr(release.time, "sleep", lambda _: None)

    def stage(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        gpp = Path("apps/records-android/app/build/gpp")
        (gpp / f"{release.PACKAGE}.txt").write_text("gpp-new-edit")
        (gpp / f"{release.PACKAGE}.skipped").touch()
        return subprocess.CompletedProcess(args, 0)

    run = MagicMock(side_effect=stage)
    monkeypatch.setattr(release.subprocess, "run", run)
    return stub, run


@pytest.mark.parametrize("lost_response", (False, True))
def test_stage_then_safe_single_commit_is_confirmed_by_a_fresh_edit(
    state: Path, publishing: tuple[PlayStub, MagicMock], lost_response: bool
) -> None:
    stub, run = publishing
    stub.commit_error = lost_response

    release.publish(state)

    commits = [(method, path) for method, path in stub.calls if ":commit?" in path]
    assert commits == [
        (
            "POST",
            "/edits/gpp-new-edit:commit?changesInReviewBehavior=ERROR_IF_IN_REVIEW&changesNotSentForReview=true",
        )
    ]
    assert stub.reads == 2
    assert run.call_count == 1
    assert run.call_args.args[0][2] == ":app:publishReleaseBundle"
    assert run.call_args.kwargs["cwd"] == "apps/records-android"
    assert json.loads((state / "receipt.json").read_text())["verified"] is True
    assert not any(method == "DELETE" and "gpp-new-edit" in path for method, path in stub.calls)


def test_unknown_commit_never_resends_when_fresh_reads_cannot_confirm(
    state: Path, publishing: tuple[PlayStub, MagicMock]
) -> None:
    stub, run = publishing
    stub.commit_error = True
    stub.confirm_commit = False

    with pytest.raises(ValueError, match="play_publish_unconfirmed_do_not_resend"):
        release.publish(state)

    assert sum(":commit?" in path for _, path in stub.calls) == 1
    assert stub.reads == 5
    assert run.call_count == 1
    assert not (state / "receipt.json").exists()


def test_already_published_digest_skips_gpp_and_commit(
    state: Path, publishing: tuple[PlayStub, MagicMock]
) -> None:
    stub, run = publishing
    stub.committed = True

    release.publish(state)

    run.assert_not_called()
    assert not any(":commit?" in path for _, path in stub.calls)
    assert (state / "receipt.json").is_file()


@pytest.mark.parametrize("conflict", (False, True))
def test_concurrent_version_or_other_track_change_cannot_commit(
    state: Path, publishing: tuple[PlayStub, MagicMock], conflict: bool
) -> None:
    stub, run = publishing
    stub.conflict = conflict
    stub.change_other_track = not conflict
    category = "play_version_code_conflict" if conflict else "play_staged_content_invalid"

    with pytest.raises(ValueError, match=category):
        release.publish(state)

    assert run.call_count == (0 if conflict else 1)
    assert not any(":commit?" in path for _, path in stub.calls)


def test_changed_verified_bundle_stops_before_any_remote_operation(
    state: Path, publishing: tuple[PlayStub, MagicMock]
) -> None:
    stub, run = publishing
    (state / "verified/app-release.aab").write_bytes(b"changed synthetic bundle")

    with pytest.raises(ValueError, match="android_verified_artifact_changed"):
        release.publish(state)

    assert not stub.calls
    run.assert_not_called()


@pytest.mark.parametrize("suffix", ("txt", "skipped", "commit"))
def test_stale_gpp_edit_cannot_trigger_another_upload(
    state: Path, publishing: tuple[PlayStub, MagicMock], suffix: str
) -> None:
    stub, run = publishing
    (Path("apps/records-android/app/build/gpp") / f"{release.PACKAGE}.{suffix}").touch()

    with pytest.raises(ValueError, match="play_stale_edit_do_not_resend"):
        release.publish(state)

    run.assert_not_called()
    assert not any(":commit?" in path for _, path in stub.calls)
