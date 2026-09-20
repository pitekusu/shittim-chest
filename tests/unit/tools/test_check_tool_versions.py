"""Tests for release-tool pin validation and latest-release comparison."""

import hashlib
import io
import json
from pathlib import Path
from typing import cast

import pytest
from tools import check_tool_versions as checker
from tools.check_tool_versions import ToolPin, build_report, load_source_pins, load_tool_pins


def _valid_config() -> dict[str, object]:
    version = "1.6.1"
    return {
        "schema_version": 1,
        "tools": {
            "betterleaks": {
                "archive_name": f"betterleaks_{version}_linux_x64.tar.gz",
                "archive_sha256": "a" * 64,
                "certificate_identity": (
                    "https://github.com/betterleaks/betterleaks/.github/workflows/"
                    f"release.yml@refs/tags/v{version}"
                ),
                "certificate_oidc_issuer": "https://token.actions.githubusercontent.com",
                "checksums_name": "checksums.txt",
                "checksums_sha256": "b" * 64,
                "repository": "betterleaks/betterleaks",
                "signature_bundle_name": "checksums.txt.sigstore.json",
                "signature_bundle_sha256": "c" * 64,
                "tag_prefix": "v",
                "version": version,
            }
        },
    }


def _write_config(path: Path, config: object) -> None:
    path.write_text(json.dumps(config) + "\n", encoding="utf-8")


def test_load_tool_pins_validates_betterleaks_identity(tmp_path: Path) -> None:
    config = tmp_path / "tools.json"
    _write_config(config, _valid_config())

    pins = load_tool_pins(config)

    assert pins == (
        ToolPin(
            name="betterleaks",
            repository="betterleaks/betterleaks",
            version="1.6.1",
            tag_prefix="v",
            archive_name="betterleaks_1.6.1_linux_x64.tar.gz",
            archive_sha256="a" * 64,
        ),
    )


def test_load_tool_pins_rejects_identity_for_another_tag(tmp_path: Path) -> None:
    config = _valid_config()
    tools = config["tools"]
    assert isinstance(tools, dict)
    tools = cast(dict[str, object], tools)
    betterleaks = tools["betterleaks"]
    assert isinstance(betterleaks, dict)
    betterleaks = cast(dict[str, object], betterleaks)
    betterleaks["certificate_identity"] = str(betterleaks["certificate_identity"]).replace(
        "v1.6.1", "v1.6.0"
    )
    path = tmp_path / "tools.json"
    _write_config(path, config)

    with pytest.raises(ValueError, match="certificate identity"):
        load_tool_pins(path)


def test_load_tool_pins_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "tools.json"
    path.write_text('{"schema_version":1,"schema_version":1,"tools":{}}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_tool_pins(path)


def test_report_distinguishes_current_and_outdated_versions() -> None:
    pins = (
        ToolPin("current", "owner/current", "1.2.3", "v", "current_1.2.3.tar.gz", "a" * 64),
        ToolPin("old", "owner/old", "2.0.0", "v", "old_2.0.0.tar.gz", "b" * 64),
    )
    tags = {"owner/current": "v1.2.3", "owner/old": "v2.1.0"}

    report, status = build_report(pins, lambda pin: tags[pin.repository])

    assert status == 1
    assert "| current | v1.2.3 | 最新 |" in report
    assert "| old | v2.0.0 | v2.1.0 |" in report


def test_temurin_hotfix_and_build_numbers_are_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".java-version").write_text("21.0.12.1+1\n")
    config = tmp_path / "tools.json"
    _write_config(
        config,
        {
            "sources": {
                "jdk": {
                    "repository": "adoptium/temurin21-binaries",
                    "tag_prefix": "jdk-",
                    "files": {".java-version": r"^([0-9.]+\+[0-9]+)"},
                }
            }
        },
    )
    pins = load_source_pins(config, tmp_path)
    monkeypatch.setattr(
        checker,
        "_github_json",
        lambda *_: {
            "tag_name": "jdk-21.0.12.1+2",
            "draft": False,
            "prerelease": False,
        },
    )
    checker.fetch_latest_release_tag.cache_clear()
    tag = checker.fetch_latest_release_tag(pins[0].repository, None)
    report, status = build_report(pins, lambda _: tag)
    assert status == 1
    assert "jdk-21.0.12.1+1 | jdk-21.0.12.1+2" in report
    checker.fetch_latest_release_tag.cache_clear()


def test_sdk_metadata_excludes_previews_and_compares_versions_numerically() -> None:
    packages = [
        ("platforms;android-37.2", "channel-0", "0", "false"),
        ("platforms;android-37.10", "channel-0", "0", "false"),
        ("platforms;android-38", "channel-1", "0", "false"),
        ("platforms;android-39-beta1", "channel-0", "1", "false"),
        ("build-tools;36.0.0", "channel-0", "0", "false"),
        ("build-tools;37.0.0", "channel-0", "1", "false"),
        ("build-tools;38.0.0", "channel-0", "0", "true"),
    ]
    xml = (
        "<repository>"
        + "".join(
            f'<remotePackage path="{path}" obsolete="{obsolete}"><channelRef ref="{channel}"/>'
            f"<revision><preview>{preview}</preview></revision></remotePackage>"
            for path, channel, preview, obsolete in packages
        )
        + "</repository>"
    )
    assert checker.latest_android_sdk_versions(xml.encode()) == {
        "platforms;android-": "37.10",
        "build-tools;": "36.0.0",
    }


@pytest.mark.parametrize(
    "xml",
    [
        b"invalid",
        b"<repository/>",
        b"x" * (checker.MAX_RESPONSE_BYTES + 1),
        b'<!DOCTYPE repository [<!ENTITY x "expanded">]><repository>&x;</repository>',
        '<!DOCTYPE repository [<!ENTITY x "expanded">]><repository>&x;</repository>'.encode(
            "utf-16"
        ),
    ],
)
def test_sdk_metadata_failure_is_not_an_up_to_date_result(xml: bytes) -> None:
    with pytest.raises(ValueError):
        checker.latest_android_sdk_versions(xml)


@pytest.mark.parametrize("other", ["0.37.0", "0.36.0", "latest"])
def test_source_pins_read_both_workflows_and_reject_drift(tmp_path: Path, other: str) -> None:
    (tmp_path / "ci.yml").write_text("version: v0.37.0\n", encoding="utf-8")
    (tmp_path / "release.yml").write_text(f"version: v{other}\n", encoding="utf-8")
    config = tmp_path / "tools.json"
    _write_config(
        config,
        {
            "sources": {
                "buildx": {
                    "repository": "docker/buildx",
                    "tag_prefix": "v",
                    "files": {"ci.yml": r"version: v(\S+)", "release.yml": r"version: v(\S+)"},
                }
            }
        },
    )
    if other != "0.37.0":
        with pytest.raises(ValueError, match=r"inconsistent|invalid"):
            load_source_pins(config, tmp_path)
    else:
        (pin,) = load_source_pins(config, tmp_path)
        assert pin.expected_tag == "v0.37.0"


def test_release_line_uses_numeric_stable_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        checker,
        "_github_json",
        lambda *_: [
            {"ref": "refs/tags/v24.9.0"},
            {"ref": "refs/tags/v24.20.0"},
            {"ref": "refs/tags/v24.21.0-rc.1"},
            {"ref": "refs/tags/v26.0.0"},
        ],
    )
    checker.fetch_latest_release_tag.cache_clear()
    assert checker.fetch_latest_release_tag("nodejs/node", None, "v24.") == "v24.20.0"
    checker.fetch_latest_release_tag.cache_clear()


@pytest.mark.parametrize(
    "payload",
    [
        {"tag_name": "v1.0.0", "draft": False, "prerelease": True},
        {"tag_name": "@someone", "draft": False, "prerelease": False},
    ],
)
def test_latest_rejects_nonstable_upstream_data(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    monkeypatch.setattr(checker, "_github_json", lambda *_: payload)
    checker.fetch_latest_release_tag.cache_clear()
    with pytest.raises(RuntimeError, match="stable"):
        checker.fetch_latest_release_tag("owner/tool", None)


def test_partial_lookup_failure_preserves_other_results_without_error_content() -> None:
    pins = [ToolPin(name, f"owner/{name}", "1.0.0", "v", "", "") for name in ("bad", "good")]

    def fetch(pin: ToolPin) -> str:
        if pin.name == "bad":
            raise RuntimeError("upstream-private-detail")
        return "v1.1.0"

    report, status = build_report(pins, fetch)
    assert status == 2
    assert "取得失敗(未確認)" in report
    assert "| good | v1.0.0 | v1.1.0 |" in report
    assert "upstream-private-detail" not in report


@pytest.mark.parametrize("payload", [b"same", b"replaced", b""])
def test_mutable_signer_download_is_checked_against_all_pins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> None:
    workflow = tmp_path / ".github/workflows/release.yml"
    workflow.parent.mkdir(parents=True)
    checksum = hashlib.sha256(b"same").hexdigest()
    workflow.write_text(
        "\n".join(
            f"AWS_SIGNER_NOTATION_{field}_SHA256: {checksum}"
            for field in ("ARCHIVE", "SIGNATURE", "KEY")
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        checker.urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(payload)
    )
    if not payload:
        with pytest.raises(RuntimeError, match="empty"):
            checker.check_signer_bundle(tmp_path)
    else:
        changes = checker.check_signer_bundle(tmp_path)
        assert len(changes) == (0 if payload == b"same" else 3)


@pytest.mark.parametrize("failure", [False, True])
def test_cli_report_exit_code_preserves_bundle_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: bool,
) -> None:
    config = tmp_path / "tools.json"
    _write_config(config, _valid_config())
    monkeypatch.setattr(
        checker,
        "load_source_pins",
        lambda *_: (ToolPin("buildx", "docker/buildx", "0.37.0", "v", "", ""),),
    )
    monkeypatch.setattr(
        checker,
        "fetch_latest_release_tag",
        lambda repo, *_: "v1.6.1" if repo == "betterleaks/betterleaks" else "v0.37.0",
    )

    def bundle(_root: Path) -> tuple[str, ...]:
        if failure:
            raise OSError("private-error-detail")
        return ()

    monkeypatch.setattr(checker, "check_signer_bundle", bundle)
    report = tmp_path / "report.md"
    assert checker.main(["latest", str(config), "--report", str(report)]) == (2 if failure else 0)
    assert "private-error-detail" not in report.read_text()
