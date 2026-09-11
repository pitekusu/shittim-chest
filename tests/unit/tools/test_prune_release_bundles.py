"""Release ZIP retention without accessing AWS or deleting real objects."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from unittest.mock import Mock
from zipfile import ZipFile

import pytest
from tools import prune_release_bundles as cleanup

RECORDS_KEYS = {f"{index:064x}.zip" for index in range(1, 6)}


def version(number: int, family: str = "records", *, latest: bool = True) -> cleanup.Version:
    key = f"{number:064x}.zip"
    if family == "core":
        key = f"lambda/shittim-chest/{number:064x}/shittim-chest-lambda-arm64.zip"
    return cleanup.Version(
        key,
        f"version-{number}-{latest}",
        100,
        datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=number),
        latest,
    )


def sample() -> tuple[list[cleanup.Version], dict[str, set[tuple[str, str | None]]]]:
    versions = [version(index, family) for family in ("records", "core") for index in range(1, 6)]
    auxiliary = version(99)
    versions.append(auxiliary)
    references: dict[str, set[tuple[str, str | None]]] = {
        "records": {(version(2).key, version(2).version_id)},
        "core": {(version(2, "core").key, None), (auxiliary.key, None)},
    }
    return versions, references


@pytest.mark.parametrize("family", ["core", "records"])
def test_keeps_current_and_two_newest_distinct_keys_and_foreign_assets(family: str) -> None:
    versions, references = sample()
    deleted = cleanup.plan_deletions(versions, references, family, RECORDS_KEYS)
    assert {item.key for item in deleted} == {version(1, family).key, version(3, family).key}


def test_pinned_version_survives_and_versions_do_not_count_as_generations() -> None:
    versions, references = sample()
    old = replace(version(2), latest=False)
    versions.remove(version(2))
    versions.extend([old, replace(version(2), version_id="new-current"), version(4, latest=False)])
    deleted = cleanup.plan_deletions(versions, references, "records", RECORDS_KEYS)
    assert old not in deleted
    assert version(4, latest=False) in deleted
    assert version(5) not in deleted


def test_fewer_than_three_keeps_all_and_missing_live_asset_stops() -> None:
    versions, references = sample()
    versions = [
        item
        for item in versions
        if item.key in {key for refs in references.values() for key, _ in refs}
    ]
    assert cleanup.plan_deletions(versions, references, "records", RECORDS_KEYS) == []
    versions.remove(version(2))
    with pytest.raises(cleanup.CleanupError, match="deployed_asset_missing"):
        cleanup.plan_deletions(versions, references, "records", RECORDS_KEYS)


def test_cloudformation_resolves_exact_versions_and_rejects_pending_change_sets() -> None:
    cloudformation = Mock()
    cloudformation.describe_stacks.return_value = {
        "Stacks": [
            {
                "StackStatus": "UPDATE_COMPLETE",
                "Parameters": [
                    {"ParameterKey": "BundleKey", "ParameterValue": "bundle.zip"},
                ],
            }
        ]
    }
    cloudformation.get_paginator.return_value.paginate.return_value = [{"Summaries": []}]
    cloudformation.get_template.return_value = {
        "TemplateBody": {
            "Resources": {
                "Function": {
                    "Type": "AWS::Lambda::Function",
                    "Properties": {
                        "Code": {
                            "S3Bucket": "bucket",
                            "S3Key": {"Ref": "BundleKey"},
                            "S3ObjectVersion": "pinned",
                        }
                    },
                },
            }
        }
    }
    assert cleanup.deployed_references(cloudformation, "bucket") == {
        family: {("bundle.zip", "pinned")} for family in ("core", "records")
    }
    cloudformation.get_paginator.return_value.paginate.return_value = [
        {
            "Summaries": [{"ExecutionStatus": "AVAILABLE"}],
        }
    ]
    with pytest.raises(cleanup.CleanupError, match="pending_release_change_set"):
        cleanup.deployed_references(cloudformation, "bucket")


def test_inventory_ignores_templates_but_rejects_zip_delete_markers() -> None:
    s3 = Mock()
    s3.get_paginator.return_value.paginate.return_value = [
        {
            "Versions": [{"Key": "template.json"}],
            "DeleteMarkers": [{"Key": "template.json"}],
        }
    ]
    assert cleanup.inventory(s3, "bucket", "account") == []
    s3.get_paginator.return_value.paginate.return_value = [
        {
            "DeleteMarkers": [{"Key": version(1).key}],
        }
    ]
    with pytest.raises(cleanup.CleanupError, match="bundle_delete_marker_present"):
        cleanup.inventory(s3, "bucket", "account")


@pytest.fixture
def clients(monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock, list[cleanup.Version]]:
    versions, references = sample()
    versions.append(version(1, latest=False))
    s3, cloudformation = Mock(), Mock()
    s3.get_bucket_versioning.return_value = {"Status": "Enabled"}
    monkeypatch.setattr(cleanup, "deployed_references", Mock(return_value=references))
    monkeypatch.setattr(cleanup, "inventory", lambda *_: list(versions))
    monkeypatch.setattr(cleanup, "records_bundle_keys", lambda *_: RECORDS_KEYS)

    def delete(**kwargs: object) -> dict[str, object]:
        payload = kwargs["Delete"]
        assert isinstance(payload, dict)
        targets = {(item["Key"], item["VersionId"]) for item in payload["Objects"]}
        versions[:] = [item for item in versions if (item.key, item.version_id) not in targets]
        return {}

    s3.delete_objects.side_effect = delete
    return s3, cloudformation, versions


def run_prune(
    clients: tuple[Mock, Mock, list[cleanup.Version]],
    *,
    apply: bool = False,
    digest: str | None = None,
) -> dict[str, object]:
    return cleanup.prune(
        clients[0],
        clients[1],
        account="000000000000",
        family="records",
        apply=apply,
        expected_plan=digest,
    )


def test_dry_run_and_changed_plan_never_delete(
    clients: tuple[Mock, Mock, list[cleanup.Version]],
) -> None:
    assert run_prune(clients)["state"] == "planned"
    with pytest.raises(cleanup.CleanupError, match="cleanup_plan_changed"):
        run_prune(clients, apply=True, digest="different-plan")
    clients[0].delete_objects.assert_not_called()


def test_superseded_auxiliary_zip_cannot_consume_a_records_generation() -> None:
    versions, references = sample()
    superseded_auxiliary = version(98)
    versions.append(superseded_auxiliary)
    s3 = Mock()
    objects: dict[str, bytes] = {}
    for item in versions:
        if not cleanup.ZIP_PATTERNS["records"].fullmatch(item.key):
            continue
        with BytesIO() as buffer:
            with ZipFile(buffer, "w") as archive:
                archive.writestr(
                    "shittim_records/__init__.py" if item.key in RECORDS_KEYS else "index.js", ""
                )
            objects[item.key] = buffer.getvalue()
    versions = [
        replace(item, size=len(objects[item.key])) if item.key in objects else item
        for item in versions
    ]
    s3.get_object.side_effect = lambda **kwargs: {"Body": BytesIO(objects[kwargs["Key"]])}
    identified = cleanup.records_bundle_keys(s3, "bucket", "account", versions)
    assert identified == RECORDS_KEYS
    deleted = cleanup.plan_deletions(versions, references, "records", identified)
    assert {item.key for item in deleted} == {version(1).key, version(3).key}
    assert all(call.kwargs["VersionId"] for call in s3.get_object.call_args_list)


def test_invalid_zip_stops_classification_and_closes_stream() -> None:
    s3 = Mock()
    body = BytesIO(b"not-a-zip")
    s3.get_object.return_value = {"Body": body}
    with pytest.raises(cleanup.CleanupError, match="invalid_bundle_zip"):
        cleanup.records_bundle_keys(s3, "bucket", "account", [replace(version(1), size=9)])
    assert body.closed


def test_apply_deletes_explicit_versions_in_separate_phases_and_verifies(
    clients: tuple[Mock, Mock, list[cleanup.Version]],
) -> None:
    plan = run_prune(clients)
    result = run_prune(clients, apply=True, digest=str(plan["plan_sha256"]))
    assert result["state"] == "deleted_and_verified"
    calls = clients[0].delete_objects.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["Delete"]["Objects"] == [
        {
            "Key": version(1).key,
            "VersionId": version(1, latest=False).version_id,
        }
    ]
    assert all(
        set(item) == {"Key", "VersionId"}
        for call in calls
        for item in call.kwargs["Delete"]["Objects"]
    )


def test_partial_failure_stops_before_deleting_current_versions(
    clients: tuple[Mock, Mock, list[cleanup.Version]],
) -> None:
    plan = run_prune(clients)
    clients[0].delete_objects.side_effect = None
    clients[0].delete_objects.return_value = {"Errors": [{"Code": "AccessDenied"}]}
    with pytest.raises(cleanup.CleanupError, match="partial_version_deletion_failed"):
        run_prune(clients, apply=True, digest=str(plan["plan_sha256"]))
    assert clients[0].delete_objects.call_count == 1


def test_changed_deployed_reference_prevents_deletion(
    clients: tuple[Mock, Mock, list[cleanup.Version]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = run_prune(clients)
    _, references = sample()
    monkeypatch.setattr(cleanup, "deployed_references", Mock(side_effect=[references, {}]))
    with pytest.raises(cleanup.CleanupError, match="deployed_references_changed"):
        run_prune(clients, apply=True, digest=str(plan["plan_sha256"]))
    clients[0].delete_objects.assert_not_called()
