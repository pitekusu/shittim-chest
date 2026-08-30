"""AWS adapter contracts for Records ADMIN prompt configuration."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item

from shittim_records.admin import (
    AdminFailure,
    PromptManifest,
    PromptOperation,
    PromptRevision,
    PromptRevisionIncomplete,
    PromptRevisionSummary,
    PromptValues,
)
from shittim_records.admin_adapters import (
    AdminSecurityConfigurationRepository,
    DynamoPromptAuditStore,
    SsmLegacyPromptSource,
    SsmPromptRevisionStore,
)

NOW = datetime(2026, 8, 24, 3, 0, tzinfo=UTC)
REVISION = "r01k3gqp6g00000000000000000"
ROOT = "/shittim-chest/production/runtime-prompts"


def _client_error(
    code: str, operation: str, message: str = "private provider detail"
) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


def _revision() -> PromptRevision:
    prompts = PromptValues.from_mapping(
        {
            "system": "system",
            "moderator": "moderator",
            "participant-a": "a",
            "participant-b": "b",
            "participant-c": "c",
        }
    )
    return PromptRevision(
        manifest=PromptManifest(
            revision=REVISION,
            created_at=NOW,
            action="publish",
            base_revision=None,
            checksums=prompts.checksums(),
        ),
        prompts=prompts,
    )


class FakeSsm:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.puts: list[dict[str, Any]] = []

    def get_parameter(self, **kwargs: Any) -> dict[str, Any]:
        name = kwargs["Name"]
        if name not in self.values:
            raise _client_error("ParameterNotFound", "GetParameter")
        return {"Parameter": {"Name": name, "Value": self.values[name]}}

    def get_parameters(self, **kwargs: Any) -> dict[str, Any]:
        names = kwargs["Names"]
        missing = [name for name in names if name not in self.values]
        return {
            "Parameters": [
                {"Name": name, "Value": self.values[name]} for name in names if name in self.values
            ],
            "InvalidParameters": missing,
        }

    def put_parameter(self, **kwargs: Any) -> dict[str, Any]:
        name = kwargs["Name"]
        if name in self.values and not kwargs["Overwrite"]:
            raise _client_error("ParameterAlreadyExists", "PutParameter")
        self.values[name] = kwargs["Value"]
        self.puts.append(kwargs)
        return {"Version": 1}

    def delete_parameters(self, **kwargs: Any) -> dict[str, Any]:
        deleted: list[str] = []
        missing: list[str] = []
        for name in kwargs["Names"]:
            if name in self.values:
                self.values.pop(name)
                deleted.append(name)
            else:
                missing.append(name)
        return {"DeletedParameters": deleted, "InvalidParameters": missing}


class FakeDynamo:
    def __init__(self) -> None:
        self.transactions: list[dict[str, Any]] = []
        self.cancel_next = False

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        self.transactions.append(kwargs)
        if self.cancel_next:
            raise _client_error("TransactionCanceledException", "TransactWriteItems")
        return {}

    def get_item(self, **_kwargs: Any) -> dict[str, Any]:
        return {}


def test_legacy_prompt_source_rejects_persona_version_mismatch() -> None:
    names = tuple(
        f"/shittim-chest/production/personas/v0001/{slot}"
        for slot in ("moderator", "participant-a", "participant-b", "participant-c")
    )
    client = FakeSsm()
    for name, slot in zip(
        names, ("moderator", "participant-a", "participant-b", "participant-c"), strict=True
    ):
        client.values[name] = json.dumps(
            {
                "schema_version": "1",
                "config_version": "v0002" if slot == "participant-b" else "v0001",
                "slot": slot,
                "display_name": slot,
                "system_prompt": f"{slot} prompt",
            }
        )
    source = SsmLegacyPromptSource(
        cast(Any, client),
        system_prompt="system prompt",
        persona_parameter_names=cast(tuple[str, str, str, str], names),
    )

    with pytest.raises(AdminFailure) as caught:
        source.load()

    assert caught.value.code == "PROMPT_CONFIGURATION_INVALID"


def test_active_pointer_retry_is_idempotent_after_pointer_write() -> None:
    client = FakeSsm()
    store = SsmPromptRevisionStore(cast(Any, client), ROOT)
    revision = _revision()
    store.create_revision(revision)
    store.create_revision(revision)
    store.activate(revision=REVISION, expected_base_revision=None)

    store.activate(revision=REVISION, expected_base_revision=None)

    active_puts = [call for call in client.puts if call["Name"] == f"{ROOT}/active"]
    revision_puts = [call for call in client.puts if call["Name"] != f"{ROOT}/active"]
    assert {call["Name"] for call in revision_puts} == {
        f"{ROOT}/{REVISION}/{name}"
        for name in (
            "system",
            "moderator",
            "participant-a",
            "participant-b",
            "participant-c",
            "manifest",
        )
    }
    assert all(
        call["Type"] == "SecureString" and call["Overwrite"] is False and call["Tier"] == "Standard"
        for call in revision_puts
    )
    assert len(active_puts) == 1
    assert active_puts[0]["Type"] == "String"
    assert active_puts[0]["Overwrite"] is False
    assert active_puts[0]["Tier"] == "Standard"
    assert store.load_revision(REVISION) == revision


def test_inactive_revision_delete_is_idempotent_and_active_revision_is_protected() -> None:
    client = FakeSsm()
    store = SsmPromptRevisionStore(cast(Any, client), ROOT)
    revision = _revision()
    store.create_revision(revision)
    client.values[f"{ROOT}/active"] = "r01k3gqp6g00000000000000001"

    store.delete_revision(REVISION)
    store.delete_revision(REVISION)

    assert not any(name.startswith(f"{ROOT}/{REVISION}/") for name in client.values)
    client.values.update(
        {
            f"{ROOT}/{REVISION}/{name}": "retained"
            for name in (
                "system",
                "moderator",
                "participant-a",
                "participant-b",
                "participant-c",
                "manifest",
            )
        }
    )
    client.values[f"{ROOT}/active"] = REVISION

    with pytest.raises(AdminFailure) as caught:
        store.delete_revision(REVISION)

    assert caught.value.code == "PROMPT_CONFIGURATION_INVALID"
    assert all(
        f"{ROOT}/{REVISION}/{name}" in client.values
        for name in (
            "system",
            "moderator",
            "participant-a",
            "participant-b",
            "participant-c",
            "manifest",
        )
    )


def test_missing_inactive_revision_is_distinguished_for_safe_recovery() -> None:
    store = SsmPromptRevisionStore(cast(Any, FakeSsm()), ROOT)

    with pytest.raises(PromptRevisionIncomplete):
        store.load_revision(REVISION)


def test_checksum_mismatch_in_stored_revision_fails_closed() -> None:
    client = FakeSsm()
    store = SsmPromptRevisionStore(cast(Any, client), ROOT)
    store.create_revision(_revision())
    client.values[f"{ROOT}/{REVISION}/participant-a"] = "tampered"

    with pytest.raises(AdminFailure) as caught:
        store.load_revision(REVISION)

    assert caught.value.code == "PROMPT_CONFIGURATION_INVALID"


def test_noncanonical_stored_prompt_fails_closed_even_when_its_normalized_checksum_matches() -> (
    None
):
    client = FakeSsm()
    store = SsmPromptRevisionStore(cast(Any, client), ROOT)
    base = _revision()
    canonical_prompts = PromptValues.from_mapping(
        {**base.prompts.as_mapping(), "participant-a": "é"}
    )
    revision = PromptRevision(
        manifest=PromptManifest(
            revision=REVISION,
            created_at=NOW,
            action="publish",
            base_revision=None,
            checksums=canonical_prompts.checksums(),
        ),
        prompts=canonical_prompts,
    )
    store.create_revision(revision)
    client.values[f"{ROOT}/{REVISION}/participant-a"] = "e\N{COMBINING ACUTE ACCENT}"

    with pytest.raises(AdminFailure) as caught:
        store.load_revision(REVISION)

    assert caught.value.code == "PROMPT_CONFIGURATION_INVALID"


def test_partial_ssm_write_never_changes_the_active_pointer() -> None:
    class PartialWriteSsm(FakeSsm):
        def put_parameter(self, **kwargs: Any) -> dict[str, Any]:
            if len(self.puts) == 2:
                raise _client_error("InternalServerError", "PutParameter")
            return super().put_parameter(**kwargs)

    client = PartialWriteSsm()
    store = SsmPromptRevisionStore(cast(Any, client), ROOT)

    with pytest.raises(AdminFailure) as caught:
        store.create_revision(_revision())

    assert caught.value.code == "PROMPT_CONFIGURATION_UNAVAILABLE"
    assert f"{ROOT}/active" not in client.values
    assert len(client.values) == 2


def test_ssm_provider_error_is_converted_to_content_free_failure() -> None:
    class FailingSsm(FakeSsm):
        def get_parameter(self, **_kwargs: Any) -> dict[str, Any]:
            raise _client_error("AccessDeniedException", "GetParameter", "secret provider text")

    store = SsmPromptRevisionStore(cast(Any, FailingSsm()), ROOT)

    with pytest.raises(AdminFailure) as caught:
        store.load_active_revision_id()

    assert caught.value.code == "PROMPT_CONFIGURATION_UNAVAILABLE"
    assert "secret provider text" not in str(caught.value)


def test_admin_configuration_validation_drops_private_exception_context() -> None:
    private_user_id = "123456789" + "01234567"

    class InvalidSsm(FakeSsm):
        def get_parameters(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "Parameters": [
                    {"Name": kwargs["Names"][0], "Value": "i" * 32},
                    {"Name": kwargs["Names"][1], "Value": "s" * 32},
                    {
                        "Name": kwargs["Names"][2],
                        "Value": '{"schema_version":1,"client_id":"' + private_user_id + '"}',
                    },
                    {"Name": kwargs["Names"][3], "Value": private_user_id},
                ]
            }

    repository = AdminSecurityConfigurationRepository(
        cast(Any, InvalidSsm()),
        identity_parameter_name="identity",
        session_key_parameter_name="session",
        oauth_parameter_name="oauth",
        admin_user_id_parameter_name="admin",
    )

    with pytest.raises(AdminFailure) as caught:
        repository.load()

    assert caught.value.code == "ADMIN_CONFIGURATION_INVALID"
    assert caught.value.__cause__ is None
    assert private_user_id not in repr(caught.value)


def test_dynamo_begin_operation_serializes_same_base_revision() -> None:
    client = FakeDynamo()
    store = DynamoPromptAuditStore(cast(Any, client), "statistics")
    store.begin_operation(
        idempotency_hash="a" * 64,
        request_hash="1" * 64,
        revision=REVISION,
        created_at=NOW,
        action="publish",
        expected_base_revision=None,
        source_revision=None,
    )
    transaction = client.transactions[0]["TransactItems"]
    assert len(transaction) == 2
    assert transaction[1]["Put"]["ConditionExpression"] == (
        "attribute_not_exists(PK) AND attribute_not_exists(SK)"
    )
    current = transaction[1]["Put"]["Item"]
    assert current["pending_request_hash"]["S"] == "1" * 64
    assert current["pending_idempotency_hash"]["S"] == "a" * 64

    client.cancel_next = True
    with pytest.raises(AdminFailure) as caught:
        store.begin_operation(
            idempotency_hash="b" * 64,
            request_hash="2" * 64,
            revision="r01k3gqp6g00000000000000001",
            created_at=NOW,
            action="publish",
            expected_base_revision=None,
            source_revision=None,
        )

    assert caught.value.code == "PROMPT_REVISION_CONFLICT"


def test_managed_base_lock_uses_active_and_pending_conditions() -> None:
    client = FakeDynamo()
    store = DynamoPromptAuditStore(cast(Any, client), "statistics")
    store.begin_operation(
        idempotency_hash="a" * 64,
        request_hash="1" * 64,
        revision="r01k3gqp6g00000000000000001",
        created_at=NOW,
        action="publish",
        expected_base_revision=REVISION,
        source_revision=None,
    )

    update = client.transactions[0]["TransactItems"][1]["Update"]
    assert update["ConditionExpression"] == (
        "active_revision = :base AND attribute_not_exists(pending_revision)"
    )
    assert "pending_request_hash" in update["UpdateExpression"]
    assert "pending_idempotency_hash" in update["UpdateExpression"]


def test_pending_request_binding_recovers_with_a_new_idempotency_key() -> None:
    operation = PromptOperation(
        idempotency_hash="a" * 64,
        request_hash="1" * 64,
        revision=REVISION,
        created_at=NOW,
        action="publish",
        base_revision=None,
        source_revision=None,
        complete=False,
    )
    items = {
        ("ADMIN#PROMPT", "CURRENT"): marshal_item(
            {
                "PK": "ADMIN#PROMPT",
                "SK": "CURRENT",
                "schema_version": 1,
                "record_type": "admin_prompt_current",
                "active_revision": "LEGACY",
                "pending_revision": REVISION,
                "pending_request_hash": operation.request_hash,
                "pending_idempotency_hash": operation.idempotency_hash,
            }
        ),
        (
            "ADMIN#PROMPT",
            f"IDEMPOTENCY#{operation.idempotency_hash}",
        ): marshal_item(DynamoPromptAuditStore._operation_item(operation)),
    }

    class PendingDynamo(FakeDynamo):
        def get_item(self, **kwargs: Any) -> dict[str, Any]:
            key = unmarshal_item(kwargs["Key"])
            item = items.get((cast(str, key["PK"]), cast(str, key["SK"])))
            return {} if item is None else {"Item": item}

    store = DynamoPromptAuditStore(cast(Any, PendingDynamo()), "statistics")

    assert store.get_pending_operation(operation.request_hash) == operation
    assert store.get_pending_operation_any() == operation
    with pytest.raises(AdminFailure) as caught:
        store.get_pending_operation("2" * 64)
    assert caught.value.code == "PROMPT_REVISION_CONFLICT"


def test_completion_clears_all_pending_request_bindings_atomically() -> None:
    operation = PromptOperation(
        idempotency_hash="a" * 64,
        request_hash="1" * 64,
        revision=REVISION,
        created_at=NOW,
        action="publish",
        base_revision=None,
        source_revision=None,
        complete=False,
    )
    summary = PromptRevisionSummary(
        revision=REVISION,
        created_at=NOW,
        action="publish",
        base_revision=None,
        source_revision=None,
        checksum="f" * 64,
    )
    client = FakeDynamo()
    store = DynamoPromptAuditStore(cast(Any, client), "statistics")

    store.complete_operation(operation=operation, summary=summary)

    current = client.transactions[0]["TransactItems"][2]["Update"]
    assert current["UpdateExpression"].endswith(
        "REMOVE pending_revision, pending_request_hash, pending_idempotency_hash"
    )
    assert "pending_request_hash = :request_hash" in current["ConditionExpression"]
    assert "pending_idempotency_hash = :idempotency_hash" in current["ConditionExpression"]


def test_delete_summary_uses_the_scoped_transaction_boundary() -> None:
    client = FakeDynamo()
    store = DynamoPromptAuditStore(cast(Any, client), "statistics")

    store.delete_summary(REVISION)

    assert client.transactions == [
        {
            "TransactItems": [
                {
                    "Delete": {
                        "TableName": "statistics",
                        "Key": marshal_item({"PK": "ADMIN#PROMPT", "SK": f"REVISION#{REVISION}"}),
                    }
                }
            ]
        }
    ]


def test_abort_operation_releases_legacy_lock_and_idempotency_record_atomically() -> None:
    operation = PromptOperation(
        idempotency_hash="a" * 64,
        request_hash="1" * 64,
        revision=REVISION,
        created_at=NOW,
        action="publish",
        base_revision=None,
        source_revision=None,
        complete=False,
    )
    client = FakeDynamo()
    store = DynamoPromptAuditStore(cast(Any, client), "statistics")

    store.abort_operation(operation=operation)

    transaction = client.transactions[0]["TransactItems"]
    assert len(transaction) == 2
    assert transaction[0]["Delete"]["Key"] == marshal_item(
        {"PK": "ADMIN#PROMPT", "SK": f"IDEMPOTENCY#{operation.idempotency_hash}"}
    )
    assert transaction[1]["Delete"]["Key"] == marshal_item({"PK": "ADMIN#PROMPT", "SK": "CURRENT"})
    assert "pending_revision = :revision" in transaction[1]["Delete"]["ConditionExpression"]


@pytest.mark.parametrize("record_kind", ["operation", "summary"])
def test_audit_records_reject_timestamps_without_an_offset(record_kind: str) -> None:
    operation = PromptOperation(
        idempotency_hash="a" * 64,
        request_hash="1" * 64,
        revision=REVISION,
        created_at=NOW,
        action="publish",
        base_revision=None,
        source_revision=None,
        complete=True,
    )
    summary = PromptRevisionSummary(
        revision=REVISION,
        created_at=NOW,
        action="publish",
        base_revision=None,
        source_revision=None,
        checksum="f" * 64,
    )
    item = (
        DynamoPromptAuditStore._operation_item(operation)
        if record_kind == "operation"
        else DynamoPromptAuditStore._summary_item(summary)
    )
    item["created_at"] = "2026-08-24T03:00:00"

    with pytest.raises(AdminFailure) as caught:
        if record_kind == "operation":
            DynamoPromptAuditStore._parse_operation(item)
        else:
            DynamoPromptAuditStore._parse_summary(item)

    assert caught.value.code == "PROMPT_CONFIGURATION_INVALID"
