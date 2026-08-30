"""AWS adapters for Records ADMIN authorization and immutable prompt revisions."""

from __future__ import annotations

import hmac
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeGuard, cast

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import TransactWriteItemTypeDef
    from mypy_boto3_ssm.client import SSMClient

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.serializer import DynamoItem
from shittim_chest.config.models import PersonaConfigPayload

from shittim_records.admin import (
    PROMPT_KEYS,
    REVISION_PATTERN,
    AdminFailure,
    AdminSecurityConfiguration,
    PromptAction,
    PromptHistoryPage,
    PromptOperation,
    PromptRevision,
    PromptRevisionIncomplete,
    PromptRevisionSummary,
    PromptValues,
    manifest_json,
    parse_manifest,
)
from shittim_records.auth import RecordsOAuthConfig

_LEGACY_PERSONA_PARAMETER_PATTERN = re.compile(
    r"^/shittim-chest/production/personas/"
    r"(?P<version>v[0-9]{4})/"
    r"(?P<slot>moderator|participant-a|participant-b|participant-c)$"
)


class AdminSecurityConfigurationRepository:
    """Load the four private inputs used to authorize one ADMIN request."""

    def __init__(
        self,
        client: SSMClient,
        *,
        identity_parameter_name: str,
        session_key_parameter_name: str,
        oauth_parameter_name: str,
        admin_user_id_parameter_name: str,
    ) -> None:
        self._client = client
        self._names = (
            identity_parameter_name,
            session_key_parameter_name,
            oauth_parameter_name,
            admin_user_id_parameter_name,
        )
        self._cached: AdminSecurityConfiguration | None = None

    def load(self) -> AdminSecurityConfiguration:
        if self._cached is not None:
            return self._cached
        try:
            response = self._client.get_parameters(Names=list(self._names), WithDecryption=True)
        except ClientError:
            raise AdminFailure("ADMIN_CONFIGURATION_UNAVAILABLE", 503) from None
        if response.get("InvalidParameters"):
            raise AdminFailure("ADMIN_CONFIGURATION_UNAVAILABLE", 503)
        values = {item["Name"]: item.get("Value", "") for item in response.get("Parameters", [])}
        if set(values) != set(self._names):
            raise AdminFailure("ADMIN_CONFIGURATION_UNAVAILABLE", 503)
        try:
            raw_values = tuple(values[name] for name in self._names)
            if any(not isinstance(value, str) for value in raw_values):
                raise TypeError
            identity_key = raw_values[0].encode()
            session_key = raw_values[1].encode()
            oauth = RecordsOAuthConfig.model_validate_json(raw_values[2])
            admin_id = raw_values[3]
        except KeyError, TypeError, ValueError, json.JSONDecodeError:
            raise AdminFailure("ADMIN_CONFIGURATION_INVALID", 503) from None
        if (
            len(identity_key) < 32
            or len(session_key) < 32
            or not 17 <= len(admin_id) <= 20
            or not admin_id.isdecimal()
        ):
            raise AdminFailure("ADMIN_CONFIGURATION_INVALID", 503)
        self._cached = AdminSecurityConfiguration(
            identity_hmac_key=identity_key,
            session_hmac_key=session_key,
            admin_discord_user_id=admin_id,
            allowed_origin=oauth.allowed_origin,
        )
        return self._cached


class SsmLegacyPromptSource:
    """Read the current participant personas while managed revisions are absent."""

    def __init__(
        self,
        client: SSMClient,
        *,
        system_prompt: str,
        persona_parameter_names: tuple[str, str, str, str],
    ) -> None:
        self._client = client
        self._system = system_prompt
        self._names = persona_parameter_names

    def load(self) -> PromptValues:
        try:
            response = self._client.get_parameters(Names=list(self._names), WithDecryption=True)
        except ClientError:
            raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503) from None
        if response.get("InvalidParameters"):
            raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503)
        values = {item["Name"]: item.get("Value", "") for item in response.get("Parameters", [])}
        if set(values) != set(self._names):
            raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503)
        prompts: dict[str, object] = {
            "system": self._system,
        }
        for expected_slot, name in zip(("moderator", *PROMPT_KEYS[2:]), self._names, strict=True):
            try:
                persona = PersonaConfigPayload.model_validate_json(values[name])
            except TypeError, ValueError, json.JSONDecodeError:
                raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503) from None
            parameter_match = _LEGACY_PERSONA_PARAMETER_PATTERN.fullmatch(name)
            if (
                parameter_match is None
                or parameter_match.group("slot") != expected_slot
                or persona.slot.value != expected_slot
                or persona.config_version != parameter_match.group("version")
            ):
                raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)
            prompts[expected_slot] = persona.system_prompt
        try:
            return PromptValues.from_mapping(prompts)
        except AdminFailure:
            raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503) from None


class SsmPromptRevisionStore:
    """Persist six immutable SecureStrings and switch the active pointer last."""

    def __init__(self, client: SSMClient, parameter_root: str) -> None:
        root = parameter_root.rstrip("/")
        if root != "/shittim-chest/production/runtime-prompts":
            raise ValueError("runtime prompt parameter root is outside the production boundary")
        self._client = client
        self._root = root
        self._active_name = f"{root}/active"

    def load_active_revision_id(self) -> str | None:
        try:
            response = self._client.get_parameter(Name=self._active_name, WithDecryption=True)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ParameterNotFound":
                return None
            raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503) from None
        value = response.get("Parameter", {}).get("Value")
        if not isinstance(value, str) or REVISION_PATTERN.fullmatch(value) is None:
            raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)
        return value

    def load_revision(self, revision: str) -> PromptRevision:
        if REVISION_PATTERN.fullmatch(revision) is None:
            raise AdminFailure("PROMPT_REVISION_INVALID", 400)
        names = self._revision_names(revision)
        try:
            response = self._client.get_parameters(
                Names=list(names.values()),
                WithDecryption=True,
            )
        except ClientError:
            raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503) from None
        if response.get("InvalidParameters"):
            raise PromptRevisionIncomplete
        values = {item["Name"]: item.get("Value", "") for item in response.get("Parameters", [])}
        if set(values) != set(names.values()):
            raise PromptRevisionIncomplete
        manifest = parse_manifest(values[names["manifest"]])
        if manifest.revision != revision:
            raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)
        stored_prompts = {key: values[names[key]] for key in PROMPT_KEYS}
        try:
            prompts = PromptValues.from_mapping(stored_prompts)
        except AdminFailure:
            raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503) from None
        if stored_prompts != prompts.as_mapping() or prompts.checksums() != dict(
            manifest.checksums
        ):
            raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)
        return PromptRevision(manifest=manifest, prompts=prompts)

    def create_revision(self, revision: PromptRevision) -> None:
        names = self._revision_names(revision.manifest.revision)
        values = {
            **revision.prompts.as_mapping(),
            "manifest": manifest_json(revision.manifest),
        }
        for key, name in names.items():
            try:
                self._client.put_parameter(
                    Name=name,
                    Value=values[key],
                    Type="SecureString",
                    Overwrite=False,
                    Tier="Standard",
                )
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != "ParameterAlreadyExists":
                    raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503) from None
        stored = self.load_revision(revision.manifest.revision)
        if stored != revision:
            raise AdminFailure("PROMPT_CONFIGURATION_CONFLICT", 409)

    def activate(self, *, revision: str, expected_base_revision: str | None) -> None:
        current = self.load_active_revision_id()
        if current == revision:
            stored = self.load_revision(revision)
            if stored.manifest.base_revision != expected_base_revision:
                raise AdminFailure("PROMPT_REVISION_CONFLICT", 409)
            return
        if current != expected_base_revision:
            raise AdminFailure("PROMPT_REVISION_CONFLICT", 409)
        try:
            self._client.put_parameter(
                Name=self._active_name,
                Value=revision,
                Type="String",
                Overwrite=current is not None,
                Tier="Standard",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ParameterAlreadyExists":
                raise AdminFailure("PROMPT_REVISION_CONFLICT", 409) from None
            raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503) from None

    def delete_revision(self, revision: str) -> None:
        if REVISION_PATTERN.fullmatch(revision) is None:
            raise AdminFailure("PROMPT_REVISION_INVALID", 400)
        if self.load_active_revision_id() == revision:
            raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)
        names = set(self._revision_names(revision).values())
        try:
            response = self._client.delete_parameters(Names=sorted(names))
        except ClientError:
            raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503) from None
        deleted = set(response.get("DeletedParameters", []))
        missing = set(response.get("InvalidParameters", []))
        if deleted & missing or deleted | missing != names:
            raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503)

    def _revision_names(self, revision: str) -> dict[str, str]:
        base = f"{self._root}/{revision}"
        return {key: f"{base}/{key}" for key in (*PROMPT_KEYS, "manifest")}


class DynamoPromptAuditStore:
    """Persist content-free revision history and durable idempotency bindings."""

    _PK = "ADMIN#PROMPT"
    _CURRENT_SK = "CURRENT"
    _LEGACY = "LEGACY"

    def __init__(self, client: DynamoDBClient, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

    def begin_operation(
        self,
        *,
        idempotency_hash: str,
        request_hash: str,
        revision: str,
        created_at: datetime,
        action: PromptAction,
        expected_base_revision: str | None,
        source_revision: str | None,
    ) -> PromptOperation:
        operation = PromptOperation(
            idempotency_hash=idempotency_hash,
            request_hash=request_hash,
            revision=revision,
            created_at=created_at,
            action=action,
            base_revision=expected_base_revision,
            source_revision=source_revision,
            complete=False,
        )
        expected = expected_base_revision or self._LEGACY
        current_action: TransactWriteItemTypeDef
        if expected_base_revision is None:
            current_action = {
                "Put": {
                    "TableName": self._table_name,
                    "Item": marshal_item(
                        {
                            "PK": self._PK,
                            "SK": self._CURRENT_SK,
                            "schema_version": 1,
                            "record_type": "admin_prompt_current",
                            "active_revision": self._LEGACY,
                            "pending_revision": revision,
                            "pending_request_hash": request_hash,
                            "pending_idempotency_hash": idempotency_hash,
                        }
                    ),
                    "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
                }
            }
        else:
            current_action = {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item({"PK": self._PK, "SK": self._CURRENT_SK}),
                    "UpdateExpression": (
                        "SET pending_revision = :revision, "
                        "pending_request_hash = :request_hash, "
                        "pending_idempotency_hash = :idempotency_hash"
                    ),
                    "ConditionExpression": (
                        "active_revision = :base AND attribute_not_exists(pending_revision)"
                    ),
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":base": expected,
                            ":revision": revision,
                            ":request_hash": request_hash,
                            ":idempotency_hash": idempotency_hash,
                        }
                    ),
                }
            }
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": marshal_item(self._operation_item(operation)),
                            "ConditionExpression": (
                                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                            ),
                        }
                    },
                    current_action,
                ]
            )
            return operation
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503) from None
        existing = self.get_operation(idempotency_hash)
        if existing is not None:
            return existing
        pending = self.get_pending_operation(request_hash)
        if pending is not None:
            return pending
        raise AdminFailure("PROMPT_REVISION_CONFLICT", 409)

    def get_operation(self, idempotency_hash: str) -> PromptOperation | None:
        try:
            response = self._client.get_item(
                TableName=self._table_name,
                Key=marshal_item(self._operation_key(idempotency_hash)),
                ConsistentRead=True,
            )
        except ClientError:
            raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503) from None
        item = response.get("Item")
        if item is None:
            return None
        return self._parse_operation(unmarshal_item(item))

    def get_pending_operation(self, request_hash: str) -> PromptOperation | None:
        binding = self._load_pending_binding()
        if binding is None:
            return None
        pending_revision, pending_request_hash, pending_idempotency_hash = binding
        if not isinstance(request_hash, str) or not _is_sha256(request_hash):
            raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)
        if not hmac.compare_digest(pending_request_hash, request_hash):
            raise AdminFailure("PROMPT_REVISION_CONFLICT", 409)
        return self._load_bound_operation(
            pending_revision=pending_revision,
            pending_request_hash=pending_request_hash,
            pending_idempotency_hash=pending_idempotency_hash,
        )

    def get_pending_for_active_revision(self, revision: str) -> PromptOperation | None:
        binding = self._load_pending_binding()
        if binding is None or binding[0] != revision:
            return None
        return self._load_bound_operation(
            pending_revision=binding[0],
            pending_request_hash=binding[1],
            pending_idempotency_hash=binding[2],
        )

    def get_pending_operation_any(self) -> PromptOperation | None:
        binding = self._load_pending_binding()
        if binding is None:
            return None
        return self._load_bound_operation(
            pending_revision=binding[0],
            pending_request_hash=binding[1],
            pending_idempotency_hash=binding[2],
        )

    def _load_pending_binding(self) -> tuple[str, str, str] | None:
        try:
            response = self._client.get_item(
                TableName=self._table_name,
                Key=marshal_item({"PK": self._PK, "SK": self._CURRENT_SK}),
                ConsistentRead=True,
            )
        except ClientError:
            raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503) from None
        raw = response.get("Item")
        if raw is None:
            return None
        item = unmarshal_item(raw)
        if (
            item.get("PK") != self._PK
            or item.get("SK") != self._CURRENT_SK
            or item.get("schema_version") != 1
            or item.get("record_type") != "admin_prompt_current"
        ):
            raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)
        active_revision = item.get("active_revision")
        if active_revision != self._LEGACY and (
            not isinstance(active_revision, str)
            or REVISION_PATTERN.fullmatch(active_revision) is None
        ):
            raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)
        pending_revision = item.get("pending_revision")
        pending_request_hash = item.get("pending_request_hash")
        pending_idempotency_hash = item.get("pending_idempotency_hash")
        values = (pending_revision, pending_request_hash, pending_idempotency_hash)
        if all(value is None for value in values):
            return None
        if (
            not isinstance(pending_revision, str)
            or REVISION_PATTERN.fullmatch(pending_revision) is None
            or not _is_sha256(pending_request_hash)
            or not _is_sha256(pending_idempotency_hash)
        ):
            raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)
        return pending_revision, pending_request_hash, pending_idempotency_hash

    def _load_bound_operation(
        self,
        *,
        pending_revision: str,
        pending_request_hash: str,
        pending_idempotency_hash: str,
    ) -> PromptOperation:
        operation = self.get_operation(pending_idempotency_hash)
        if (
            operation is None
            or operation.complete
            or operation.revision != pending_revision
            or not hmac.compare_digest(operation.request_hash, pending_request_hash)
        ):
            raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)
        return operation

    def complete_operation(
        self,
        *,
        operation: PromptOperation,
        summary: PromptRevisionSummary,
    ) -> None:
        if summary.revision != operation.revision:
            raise ValueError("prompt operation and summary revisions differ")
        expected = summary.base_revision or self._LEGACY
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": marshal_item(self._summary_item(summary)),
                            "ConditionExpression": (
                                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                            ),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": marshal_item(self._operation_key(operation.idempotency_hash)),
                            "UpdateExpression": "SET #state = :complete",
                            "ConditionExpression": (
                                "#state = :pending AND request_hash = :request_hash "
                                "AND revision = :revision"
                            ),
                            "ExpressionAttributeNames": {"#state": "state"},
                            "ExpressionAttributeValues": marshal_item(
                                {
                                    ":complete": "complete",
                                    ":pending": "pending",
                                    ":request_hash": operation.request_hash,
                                    ":revision": operation.revision,
                                }
                            ),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": marshal_item({"PK": self._PK, "SK": self._CURRENT_SK}),
                            "UpdateExpression": (
                                "SET active_revision = :revision "
                                "REMOVE pending_revision, pending_request_hash, "
                                "pending_idempotency_hash"
                            ),
                            "ConditionExpression": (
                                "active_revision = :base AND pending_revision = :revision "
                                "AND pending_request_hash = :request_hash "
                                "AND pending_idempotency_hash = :idempotency_hash"
                            ),
                            "ExpressionAttributeValues": marshal_item(
                                {
                                    ":base": expected,
                                    ":revision": operation.revision,
                                    ":request_hash": operation.request_hash,
                                    ":idempotency_hash": operation.idempotency_hash,
                                }
                            ),
                        }
                    },
                ]
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503) from None
            existing = self.get_operation(operation.idempotency_hash)
            if existing is not None and existing.complete:
                return
            raise AdminFailure("PROMPT_REVISION_CONFLICT", 409) from None

    def abort_operation(self, *, operation: PromptOperation) -> None:
        expected = operation.base_revision or self._LEGACY
        current_action: TransactWriteItemTypeDef
        if operation.base_revision is None:
            current_action = {
                "Delete": {
                    "TableName": self._table_name,
                    "Key": marshal_item({"PK": self._PK, "SK": self._CURRENT_SK}),
                    "ConditionExpression": (
                        "active_revision = :base AND pending_revision = :revision "
                        "AND pending_request_hash = :request_hash "
                        "AND pending_idempotency_hash = :idempotency_hash"
                    ),
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":base": expected,
                            ":revision": operation.revision,
                            ":request_hash": operation.request_hash,
                            ":idempotency_hash": operation.idempotency_hash,
                        }
                    ),
                }
            }
        else:
            current_action = {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item({"PK": self._PK, "SK": self._CURRENT_SK}),
                    "UpdateExpression": (
                        "REMOVE pending_revision, pending_request_hash, pending_idempotency_hash"
                    ),
                    "ConditionExpression": (
                        "active_revision = :base AND pending_revision = :revision "
                        "AND pending_request_hash = :request_hash "
                        "AND pending_idempotency_hash = :idempotency_hash"
                    ),
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":base": expected,
                            ":revision": operation.revision,
                            ":request_hash": operation.request_hash,
                            ":idempotency_hash": operation.idempotency_hash,
                        }
                    ),
                }
            }
        operation_action: TransactWriteItemTypeDef = {
            "Delete": {
                "TableName": self._table_name,
                "Key": marshal_item(self._operation_key(operation.idempotency_hash)),
                "ConditionExpression": (
                    "#state = :pending AND request_hash = :request_hash AND revision = :revision"
                ),
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": marshal_item(
                    {
                        ":pending": "pending",
                        ":request_hash": operation.request_hash,
                        ":revision": operation.revision,
                    }
                ),
            }
        }
        try:
            self._client.transact_write_items(
                TransactItems=[
                    operation_action,
                    current_action,
                ]
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise AdminFailure("PROMPT_REVISION_CONFLICT", 409) from None
            raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503) from None

    def get_summary(self, revision: str) -> PromptRevisionSummary | None:
        try:
            response = self._client.get_item(
                TableName=self._table_name,
                Key=marshal_item(self._summary_key(revision)),
                ConsistentRead=True,
            )
        except ClientError:
            raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503) from None
        item = response.get("Item")
        return None if item is None else self._parse_summary(unmarshal_item(item))

    def list_summaries(self, *, limit: int, cursor: str | None) -> PromptHistoryPage:
        parameters: dict[str, Any] = {
            "TableName": self._table_name,
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
            "ExpressionAttributeValues": marshal_item({":pk": self._PK, ":prefix": "REVISION#"}),
            "ScanIndexForward": False,
            "Limit": limit,
            "ConsistentRead": True,
        }
        if cursor is not None:
            parameters["ExclusiveStartKey"] = marshal_item(self._summary_key(cursor))
        try:
            response = self._client.query(**parameters)
        except ClientError:
            raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503) from None
        items = tuple(
            self._parse_summary(unmarshal_item(item)) for item in response.get("Items", [])
        )
        last_key = response.get("LastEvaluatedKey")
        next_cursor: str | None = None
        if last_key:
            decoded = unmarshal_item(cast(dict[str, Any], last_key))
            sk = decoded.get("SK")
            if not isinstance(sk, str) or not sk.startswith("REVISION#"):
                raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)
            next_cursor = sk.removeprefix("REVISION#")
        return PromptHistoryPage(items=items, next_cursor=next_cursor)

    def delete_summary(self, revision: str) -> None:
        if REVISION_PATTERN.fullmatch(revision) is None:
            raise AdminFailure("PROMPT_REVISION_INVALID", 400)
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Delete": {
                            "TableName": self._table_name,
                            "Key": marshal_item(self._summary_key(revision)),
                        }
                    }
                ]
            )
        except ClientError:
            raise AdminFailure("PROMPT_CONFIGURATION_UNAVAILABLE", 503) from None

    @classmethod
    def _operation_key(cls, idempotency_hash: str) -> dict[str, str]:
        return {"PK": cls._PK, "SK": f"IDEMPOTENCY#{idempotency_hash}"}

    @classmethod
    def _summary_key(cls, revision: str) -> dict[str, str]:
        return {"PK": cls._PK, "SK": f"REVISION#{revision}"}

    @classmethod
    def _operation_item(cls, operation: PromptOperation) -> DynamoItem:
        return {
            **cls._operation_key(operation.idempotency_hash),
            "schema_version": 1,
            "record_type": "admin_prompt_idempotency",
            "request_hash": operation.request_hash,
            "revision": operation.revision,
            "created_at": operation.created_at.astimezone(UTC).isoformat(),
            "action": operation.action,
            "base_revision": operation.base_revision,
            "source_revision": operation.source_revision,
            "state": "complete" if operation.complete else "pending",
        }

    @classmethod
    def _summary_item(cls, summary: PromptRevisionSummary) -> DynamoItem:
        return {
            **cls._summary_key(summary.revision),
            "schema_version": 1,
            "record_type": "admin_prompt_revision",
            "revision": summary.revision,
            "created_at": summary.created_at.astimezone(UTC).isoformat(),
            "action": summary.action,
            "base_revision": summary.base_revision,
            "source_revision": summary.source_revision,
            "checksum": summary.checksum,
        }

    @staticmethod
    def _parse_operation(item: DynamoItem) -> PromptOperation:
        try:
            pk = cast(str, item["PK"])
            sk = cast(str, item["SK"])
            created_at = _parse_aware_datetime(item["created_at"])
            operation = PromptOperation(
                idempotency_hash=sk.removeprefix("IDEMPOTENCY#"),
                request_hash=cast(str, item["request_hash"]),
                revision=cast(str, item["revision"]),
                created_at=created_at,
                action=cast(Any, item["action"]),
                base_revision=cast(str | None, item.get("base_revision")),
                source_revision=cast(str | None, item.get("source_revision")),
                complete=item["state"] == "complete",
            )
        except KeyError, TypeError, ValueError:
            raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503) from None
        if (
            item.get("schema_version") != 1
            or item.get("record_type") != "admin_prompt_idempotency"
            or pk != DynamoPromptAuditStore._PK
            or sk != f"IDEMPOTENCY#{operation.idempotency_hash}"
            or item.get("state") not in {"pending", "complete"}
            or len(operation.idempotency_hash) != 64
            or len(operation.request_hash) != 64
            or REVISION_PATTERN.fullmatch(operation.revision) is None
            or operation.action not in {"publish", "rollback"}
            or (
                operation.base_revision is not None
                and REVISION_PATTERN.fullmatch(operation.base_revision) is None
            )
            or (
                operation.source_revision is not None
                and REVISION_PATTERN.fullmatch(operation.source_revision) is None
            )
            or (operation.action == "publish" and operation.source_revision is not None)
            or (
                operation.action == "rollback"
                and (operation.base_revision is None or operation.source_revision is None)
            )
        ):
            raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)
        if any(
            character not in "0123456789abcdef"
            for value in (operation.idempotency_hash, operation.request_hash)
            for character in value
        ):
            raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)
        return operation

    @staticmethod
    def _parse_summary(item: DynamoItem) -> PromptRevisionSummary:
        try:
            summary = PromptRevisionSummary(
                revision=cast(str, item["revision"]),
                created_at=_parse_aware_datetime(item["created_at"]),
                action=cast(Any, item["action"]),
                base_revision=cast(str | None, item.get("base_revision")),
                source_revision=cast(str | None, item.get("source_revision")),
                checksum=cast(str, item["checksum"]),
            )
        except KeyError, TypeError, ValueError:
            raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503) from None
        revisions = (summary.revision, summary.base_revision, summary.source_revision)
        if (
            item.get("schema_version") != 1
            or item.get("record_type") != "admin_prompt_revision"
            or item.get("PK") != DynamoPromptAuditStore._PK
            or item.get("SK") != f"REVISION#{summary.revision}"
            or summary.action not in {"publish", "rollback"}
            or any(
                value is not None and REVISION_PATTERN.fullmatch(value) is None
                for value in revisions
            )
            or len(summary.checksum) != 64
            or (summary.action == "publish" and summary.source_revision is not None)
            or (
                summary.action == "rollback"
                and (summary.base_revision is None or summary.source_revision is None)
            )
        ):
            raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)
        if any(character not in "0123456789abcdef" for character in summary.checksum):
            raise AdminFailure("PROMPT_CONFIGURATION_INVALID", 503)
        return summary


def _is_sha256(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _parse_aware_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include an offset")
    return parsed.astimezone(UTC)
