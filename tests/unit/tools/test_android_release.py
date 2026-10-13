"""Test only project-specific release inputs and verification, not the publishing Action."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tools import android_release as release

SHA = "a" * 40
NOTES = "キーボード表示中の入力操作を改善しました。\r\nアプリの安定性を改善しました。"
NOTES_FIELDS = {
    "releaseNotesLanguage": "ja-JP",
    "releaseNotesSha256": hashlib.sha256(NOTES.encode("utf-8")).hexdigest(),
}


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    api = MagicMock()
    api.__enter__.return_value = api
    api.edits().insert().execute.return_value = {"id": "synthetic-inspection"}
    api.edits().tracks().list().execute.return_value = {"tracks": [{"track": "alpha"}]}
    api.edits().bundles().list().execute.return_value = {"bundles": []}
    api.edits().apks().list().execute.return_value = {"apks": []}
    api.reset_mock()
    monkeypatch.setattr(release, "client", lambda: api)
    return api


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GITHUB_SHA", SHA)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    state = tmp_path / "android-release"
    state.mkdir()
    notes_path = state / release.RELEASE_NOTES_FILE
    notes_path.parent.mkdir()
    notes_path.write_text(NOTES, encoding="utf-8")
    return state


@pytest.fixture
def materialize_inputs(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    inputs = {
        "RELEASE_NOTES_JA": NOTES,
        "UPLOAD_KEY_BASE64": base64.b64encode(b"synthetic keystore").decode(),
        "UPLOAD_STORE_PASSWORD": "synthetic",
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
        monkeypatch.setenv(name, value)
    return inputs


@pytest.mark.parametrize("value", (1, "1", 2_100_000_000, "2100000000"))
def test_version_codes_accept_only_play_integers(value: object) -> None:
    assert release.version_code(value) == int(str(value))


@pytest.mark.parametrize("value", (True, False, 0, -1, 1.0, None, "01", "1e3", 2_100_000_001))
def test_version_codes_reject_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match="play_version_code_invalid"):
        release.version_code(value)


@pytest.mark.parametrize("track,bundle,apk", ((41, 28, 17), (13, 51, 17), (13, 28, 62)))
def test_preflight_uses_all_tracks_bundles_and_apks(
    api: MagicMock, state: Path, track: int, bundle: int, apk: int
) -> None:
    api.edits().tracks().list().execute.return_value = {
        "tracks": [
            {"track": "alpha", "releases": [{"versionCodes": ["1"]}]},
            {"track": "internal", "releases": [{"versionCodes": ["2"]}]},
            {"track": "production", "releases": [{"versionCodes": [str(track)]}]},
        ]
    }
    api.edits().bundles().list().execute.return_value = {"bundles": [{"versionCode": bundle}]}
    api.edits().apks().list().execute.return_value = {"apks": [{"versionCode": apk}]}

    release.preflight(state)

    code = max(track, bundle, apk) + 1
    assert json.loads((state / "plan.json").read_text()) == {"sha": SHA, "versionCode": code}
    assert (state.parent / "output").read_text() == f"version_code={code}\n"
    for resource in (api.edits().tracks(), api.edits().bundles(), api.edits().apks()):
        resource.list.return_value.execute.assert_called_once_with(num_retries=3)
    api.edits().insert.return_value.execute.assert_called_once_with()
    api.edits().delete.assert_called_once_with(
        packageName=release.PACKAGE, editId="synthetic-inspection"
    )
    api.edits().delete.return_value.execute.assert_called_once_with()
    api.edits().commit.assert_not_called()
    api.edits().bundles().upload.assert_not_called()


def test_failed_upload_floor_does_not_reuse_unlisted_version(
    api: MagicMock, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINIMUM_VERSION_CODE", "29")
    release.preflight(state)
    assert json.loads((state / "plan.json").read_text())["versionCode"] == 29


@pytest.mark.parametrize("floor,observed", (("2100000000", 27), ("1", 2_099_999_999)))
def test_terminal_upload_code_is_rejected_before_build_or_publication(
    api: MagicMock, state: Path, monkeypatch: pytest.MonkeyPatch, floor: str, observed: int
) -> None:
    monkeypatch.setenv("MINIMUM_VERSION_CODE", floor)
    api.edits().bundles().list().execute.return_value = {"bundles": [{"versionCode": observed}]}

    with pytest.raises(ValueError, match="play_version_code_exhausted"):
        release.preflight(state)

    assert not (state / "plan.json").exists()
    assert not (state.parent / "output").exists()
    api.edits().commit.assert_not_called()
    api.edits().bundles().upload.assert_not_called()


@pytest.mark.parametrize(
    "name,value",
    (("GITHUB_RUN_ATTEMPT", "2"), ("GITHUB_REF", "refs/heads/feature"), ("GITHUB_SHA", "bad")),
)
def test_untrusted_run_stops_before_creating_inspection(
    api: MagicMock, state: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        release.preflight(state)
    api.edits().insert.assert_not_called()


def test_failed_inventory_discards_only_its_read_edit(api: MagicMock, state: Path) -> None:
    api.edits().bundles().list().execute.side_effect = TimeoutError("synthetic")
    with pytest.raises(TimeoutError):
        release.preflight(state)
    api.edits().delete.assert_called_once_with(
        packageName=release.PACKAGE, editId="synthetic-inspection"
    )
    assert not (state / "plan.json").exists()


@pytest.mark.parametrize("other_track", ("production", "internal", "beta"))
def test_missing_closed_track_does_not_fall_back(
    api: MagicMock, state: Path, other_track: str
) -> None:
    api.edits().tracks().list().execute.return_value = {"tracks": [{"track": other_track}]}
    with pytest.raises(ValueError, match="play_closed_track_missing"):
        release.preflight(state)
    assert not (state / "plan.json").exists()
    api.edits().commit.assert_not_called()
    api.edits().bundles().upload.assert_not_called()


@pytest.mark.parametrize(
    "credential_type", ("service_account", "authorized_user", "external_account")
)
def test_credentials_require_federated_service_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, credential_type: str
) -> None:
    credentials = tmp_path / "synthetic-credentials.json"
    credentials.write_text(json.dumps({"type": credential_type}))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(credentials))
    load = MagicMock()
    monkeypatch.setattr(release.google.auth, "load_credentials_from_file", load)
    with pytest.raises(ValueError, match="play_requires_federated_service_account"):
        release.client()
    load.assert_not_called()


def test_client_delegates_play_connection_to_official_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = tmp_path / "synthetic-wif.json"
    credentials.write_text(
        json.dumps(
            {
                "type": "external_account",
                "service_account_impersonation_url": "https://iamcredentials.googleapis.com/fixture",
            }
        )
    )
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(credentials))
    loaded = MagicMock()
    load = MagicMock(return_value=(loaded, None))
    build = MagicMock()
    monkeypatch.setattr(release.google.auth, "load_credentials_from_file", load)
    monkeypatch.setattr(release, "build", build)
    assert release.client() is build.return_value
    load.assert_called_once_with(str(credentials), scopes=[release.SCOPE])
    build.assert_called_once_with(
        "androidpublisher", "v3", credentials=loaded, cache_discovery=False
    )


@pytest.mark.parametrize(
    "missing", ("UPLOAD_STORE_PASSWORD", "UPLOAD_KEY_ALIAS", "FIREBASE_CLIENT_CONFIG")
)
def test_missing_inputs_do_not_materialize_private_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    materialize_inputs: dict[str, str],
    missing: str,
) -> None:
    monkeypatch.setenv(missing, "")
    private = tmp_path / "private"
    with pytest.raises(ValueError, match="android_release_inputs_missing"):
        release.materialize(private)
    assert not list(private.iterdir())


@pytest.mark.parametrize("notes", ("あ" * 500, "😀" * 500))
def test_materialize_accepts_500_unicode_characters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    materialize_inputs: dict[str, str],
    notes: str,
) -> None:
    monkeypatch.setenv("RELEASE_NOTES_JA", notes)
    private = tmp_path / "private"

    release.materialize(private)

    assert (private / release.RELEASE_NOTES_FILE).read_text(encoding="utf-8") == notes


@pytest.mark.parametrize("notes", ("", " \n　", "あ" * 501, "\udcff"))
def test_invalid_release_notes_stop_before_materializing_private_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    materialize_inputs: dict[str, str],
    notes: str,
) -> None:
    monkeypatch.setenv("RELEASE_NOTES_JA", notes)
    private = tmp_path / "private"

    with pytest.raises(ValueError, match=r"play_release_notes_.*_invalid"):
        release.materialize(private)

    assert not private.exists()


def test_release_notes_keep_shell_characters_as_text_without_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    materialize_inputs: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = tmp_path / "must-not-be-created"
    notes = f'変更の説明\n$(touch "{marker}") `touch "{marker}"` $HOME ${{{{ inputs.other }}}}'
    monkeypatch.setenv("RELEASE_NOTES_JA", f"\n{notes}\n")
    private = tmp_path / "private"

    release.materialize(private)

    assert (private / release.RELEASE_NOTES_FILE).read_text(encoding="utf-8") == notes
    assert not marker.exists()
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "remote_code,remote_hash,status,remote_track,expected",
    (
        (7, "ab" * 32, "completed", "alpha", True),
        (8, "ab" * 32, "completed", "alpha", False),
        (7, "cd" * 32, "completed", "alpha", False),
        (7, "ab" * 32, "draft", "alpha", False),
        (7, "ab" * 32, "completed", "internal", False),
        (7, "ab" * 32, "completed", "production", False),
    ),
)
def test_readback_verifies_closed_artifact_without_publishing(
    api: MagicMock,
    state: Path,
    remote_code: int,
    remote_hash: str,
    status: str,
    remote_track: str,
    expected: bool,
) -> None:
    verified = {
        "sha": SHA,
        "versionCode": 7,
        "bundleSha256": "ab" * 32,
        "track": "alpha",
        **NOTES_FIELDS,
    }
    (state / "verification.json").write_text(json.dumps(verified))
    api.edits().tracks().get.return_value.execute.return_value = {
        "track": remote_track,
        "releases": [
            {
                "status": status,
                "versionCodes": [str(remote_code)],
                "releaseNotes": [{"language": "ja-JP", "text": NOTES}],
            }
        ],
    }
    api.edits().bundles().list().execute.return_value = {
        "bundles": [{"versionCode": remote_code, "sha256": remote_hash}]
    }
    if expected:
        # Only the digest must survive always-cleanup to confirm a prior submission.
        (state / release.RELEASE_NOTES_FILE).unlink()
        (state / "whatsnew").rmdir()
        release.readback(state)
        assert json.loads((state / "receipt.json").read_text()) == {
            **verified,
            "status": "submitted",
            "verified": True,
        }
    else:
        with pytest.raises(ValueError, match="play_submission_unconfirmed"):
            release.readback(state)
        assert not (state / "receipt.json").exists()
    api.edits().commit.assert_not_called()
    api.edits().bundles().upload.assert_not_called()
    api.edits().tracks().update.assert_not_called()
    api.edits().tracks().get.assert_called_once_with(
        packageName=release.PACKAGE, editId="synthetic-inspection", track="alpha"
    )
    api.edits().tracks().get.return_value.execute.assert_called_once_with(num_retries=3)
    api.edits().bundles().list.return_value.execute.assert_called_once_with(num_retries=3)
    api.edits().insert.return_value.execute.assert_called_once_with()
    api.edits().delete.return_value.execute.assert_called_once_with()


@pytest.mark.parametrize("failed_read", ("track", "bundle"))
def test_exhausted_readback_preserves_verification_without_resubmitting(
    api: MagicMock, state: Path, failed_read: str
) -> None:
    verified = {
        "sha": SHA,
        "versionCode": 7,
        "bundleSha256": "ab" * 32,
        "track": "alpha",
        **NOTES_FIELDS,
    }
    verification = state / "verification.json"
    verification.write_text(json.dumps(verified))
    api.edits().tracks().get.return_value.execute.return_value = {
        "track": "alpha",
        "releases": [
            {
                "status": "completed",
                "versionCodes": ["7"],
                "releaseNotes": [{"language": "ja-JP", "text": NOTES}],
            }
        ],
    }
    api.edits().bundles().list.return_value.execute.return_value = {
        "bundles": [{"versionCode": 7, "sha256": "ab" * 32}]
    }
    request = (
        api.edits().tracks().get.return_value
        if failed_read == "track"
        else api.edits().bundles().list.return_value
    )
    request.execute.side_effect = release.HttpError(
        MagicMock(status=503), b'{"error":{"message":"synthetic temporary outage"}}'
    )

    with pytest.raises(release.HttpError):
        release.readback(state)

    request.execute.assert_called_once_with(num_retries=3)
    api.edits().insert.return_value.execute.assert_called_once_with()
    api.edits().delete.return_value.execute.assert_called_once_with()
    assert json.loads(verification.read_text()) == verified
    assert not (state / "receipt.json").exists()
    api.edits().commit.assert_not_called()
    api.edits().bundles().upload.assert_not_called()
    api.edits().tracks().update.assert_not_called()


def test_verification_records_the_fixed_closed_track(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(state.parent)
    monkeypatch.setenv("ANDROID_UPLOAD_CERT_SHA256", "ab" * 32)
    (state / "plan.json").write_text(json.dumps({"sha": SHA, "versionCode": 7}))
    bundle = Path("apps/records-android/app/build/outputs/bundle/release/app-release.aab")
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(b"synthetic signed bundle")
    verified = {"sha": SHA, "versionCode": 7, "bundleSha256": release.digest(bundle)}
    verifier = MagicMock(return_value=verified)
    monkeypatch.setattr(release, "verify_bundle", verifier)
    bundletool = state.parent / "bundletool.jar"

    release.verify(state, bundletool)

    assert json.loads((state / "verification.json").read_text()) == {
        **verified,
        "track": "alpha",
        **NOTES_FIELDS,
    }
    assert (state / "verified/app-release.aab").read_bytes() == bundle.read_bytes()
    verifier.assert_called_once_with(bundle, bundletool, code=7, sha=SHA, certificate="ab" * 32)


@pytest.mark.parametrize("recorded_track", ("internal", "production", "beta", None))
def test_wrong_verification_track_stops_before_play_inspection(
    api: MagicMock, state: Path, recorded_track: str | None
) -> None:
    (state / "verification.json").write_text(
        json.dumps(
            {"sha": SHA, "versionCode": 7, "bundleSha256": "ab" * 32, "track": recorded_track}
        )
    )

    with pytest.raises(ValueError, match="play_verification_track_invalid"):
        release.readback(state)

    api.edits().insert.assert_not_called()
    assert not (state / "receipt.json").exists()


@pytest.mark.parametrize(
    "remote_notes",
    (
        [],
        [{"language": "ja-JP", "text": "前の更新内容"}],
        [{"language": "en-US", "text": NOTES}],
        [{"language": "ja-JP", "text": NOTES}, {"language": "ja-JP", "text": NOTES}],
    ),
)
def test_readback_rejects_missing_changed_or_duplicate_japanese_notes(
    api: MagicMock, state: Path, remote_notes: list[dict[str, str]]
) -> None:
    (state / "verification.json").write_text(
        json.dumps(
            {
                "sha": SHA,
                "versionCode": 7,
                "bundleSha256": "ab" * 32,
                "track": "alpha",
                **NOTES_FIELDS,
            }
        )
    )
    api.edits().tracks().get.return_value.execute.return_value = {
        "track": "alpha",
        "releases": [
            {"status": "completed", "versionCodes": ["7"], "releaseNotes": remote_notes},
            {
                "status": "completed",
                "versionCodes": ["6"],
                "releaseNotes": [{"language": "ja-JP", "text": NOTES}],
            },
        ],
    }
    api.edits().bundles().list().execute.return_value = {
        "bundles": [{"versionCode": 7, "sha256": "ab" * 32}]
    }

    with pytest.raises(ValueError, match="play_submission_unconfirmed"):
        release.readback(state)

    assert not (state / "receipt.json").exists()
    api.edits().commit.assert_not_called()
    api.edits().bundles().upload.assert_not_called()
    api.edits().tracks().update.assert_not_called()


@pytest.mark.parametrize(
    "notes_fields",
    (
        {},
        {"releaseNotesLanguage": "en-US", "releaseNotesSha256": "ab" * 32},
        {"releaseNotesLanguage": "ja-JP", "releaseNotesSha256": "invalid"},
    ),
)
def test_invalid_notes_verification_stops_before_readback_inspection(
    api: MagicMock, state: Path, notes_fields: dict[str, str]
) -> None:
    (state / "verification.json").write_text(json.dumps({"track": "alpha", **notes_fields}))

    with pytest.raises(ValueError, match="play_release_notes_verification_invalid"):
        release.readback(state)

    api.edits().insert.assert_not_called()
    assert not (state / "receipt.json").exists()


def test_cleanup_removes_notes_but_preserves_submission_evidence(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("upload-key.p12", "google-services.json", "gradle-build.log"):
        (state / name).write_bytes(b"synthetic private file")
    for name in ("plan.json", "verification.json", "receipt.json"):
        (state / name).write_text("{}")
    monkeypatch.setenv("RUNNER_TEMP", str(state.parent))
    monkeypatch.setattr("sys.argv", ["release", "cleanup", "--state", str(state)])

    assert release.main() == 0

    assert not (state / "whatsnew").exists()
    assert all(not (state / name).exists() for name in release.PRIVATE_FILES)
    assert all(
        (state / name).read_text() == "{}"
        for name in ("plan.json", "verification.json", "receipt.json")
    )


def test_invalid_state_and_private_errors_are_not_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["release", "cleanup", "--state", str(tmp_path)])
    assert release.main() == 1
    assert capsys.readouterr().err == "android_release_state_path_invalid\n"
    monkeypatch.setattr(
        "sys.argv", ["release", "readback", "--state", str(tmp_path / "android-release")]
    )
    monkeypatch.setattr(
        release, "readback", MagicMock(side_effect=RuntimeError("synthetic secret"))
    )
    assert release.main() == 1
    assert capsys.readouterr().err == "android_release_failed\n"


@pytest.mark.parametrize("status", (403, 503))
def test_official_sdk_http_error_logs_only_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr(
        "sys.argv", ["release", "readback", "--state", str(tmp_path / "android-release")]
    )
    response = MagicMock(status=status)
    error = release.HttpError(response, b'{"error":{"message":"synthetic private detail"}}')
    logger = release.logging.getLogger("googleapiclient.http")
    monkeypatch.setattr(logger, "disabled", False)

    def failed_readback(state: Path) -> None:
        logger.warning("Retrying synthetic private edit URI after temporary error")
        raise error

    monkeypatch.setattr(release, "readback", failed_readback)
    assert release.main() == 1
    assert capsys.readouterr().err == f"play_http_{status}\n"
    assert not caplog.records
