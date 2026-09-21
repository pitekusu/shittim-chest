"""Mobile handoff contracts only; no persistence, HTTP routes, or OAuth execution."""

from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from shittim_records.auth import OAUTH_TTL, SESSION_TTL, AuthFailure
from shittim_records.contracts import NonEmptyText, PublicModel, SessionUser

MOBILE_TRANSACTION_TTL_SECONDS = int(OAUTH_TTL.total_seconds())
MOBILE_CODE_TTL_SECONDS = 60
MOBILE_SESSION_TTL_SECONDS = int(SESSION_TTL.total_seconds())
# Relative to the configured Records HTTPS origin, never supplied by the caller.
MOBILE_CALLBACK_PATH = "/auth/mobile/callback"

OpaqueValue = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$", repr=False)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", repr=False)]
CodeVerifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9._~-]{43,128}$", repr=False)]
ReturnDestination = Annotated[str, Field(pattern=r"^/(?:records/[A-Za-z0-9_-]{43})?$")]
EpochSeconds = Annotated[int, Field(strict=True, ge=0)]


class MobileModel(PublicModel):
    """Wire DTO; validation errors must still be sanitized by the HTTP adapter."""

    model_config = ConfigDict(hide_input_in_errors=True, validate_by_name=False)


class MobileStartRequest(MobileModel):
    code_challenge: OpaqueValue
    code_challenge_method: Literal["S256"]
    state: OpaqueValue
    return_to: ReturnDestination = "/"


class MobileStartResponse(MobileModel):
    schema_version: Literal[1]
    transaction_id: OpaqueValue
    # Android resolves this path against its fixed API origin before opening Custom Tabs.
    authorize_path: Annotated[
        str,
        Field(
            pattern=r"^/api/v1/auth/mobile/authorize\?transaction=[A-Za-z0-9_-]{43}$",
            repr=False,
        ),
    ]
    expires_at: AwareDatetime


class MobileAuthorizeRequest(MobileModel):
    transaction: OpaqueValue


class MobileCallbackParameters(MobileModel):
    """App Link query on success: correlation and one-time code, never a session token."""

    transaction: OpaqueValue
    code: OpaqueValue
    state: OpaqueValue


class MobileExchangeRequest(MobileModel):
    transaction_id: OpaqueValue
    code: OpaqueValue
    code_verifier: CodeVerifier


class MobileSessionResponse(MobileModel):
    """An invalid/expired Bearer session returns 401, not this success DTO."""

    schema_version: Literal[1]
    user: SessionUser = Field(repr=False)
    is_admin: bool = Field(strict=True)
    expires_at: AwareDatetime


class MobileExchangeResponse(MobileSessionResponse):
    access_token: OpaqueValue
    token_type: Literal["Bearer"]
    return_to: ReturnDestination


def parse_mobile_request[Model: MobileModel](model: type[Model], value: object) -> Model:
    """Expose only a stable category, never Pydantic's input-bearing error details."""

    try:
        return model.model_validate(value)
    except ValidationError:
        raise AuthFailure("mobile_request_invalid") from None


class MobileTransaction(BaseModel):
    """Server-only storage shape. Raw codes, verifier and Bearer token are never stored."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    transaction_hash: Digest
    code_challenge: OpaqueValue
    # App-generated correlation value must be returned unchanged, but grants no authority.
    client_state: OpaqueValue
    return_to: ReturnDestination
    created_at: EpochSeconds
    expires_at: EpochSeconds

    @model_validator(mode="after")
    def validate_lifetime(self) -> MobileTransaction:
        if not 0 < self.expires_at - self.created_at <= MOBILE_TRANSACTION_TTL_SECONDS:
            raise ValueError("invalid_mobile_transaction_lifetime")
        return self


class MobileStartedTransaction(MobileTransaction):
    status: Literal["started"]


class MobileAuthorizingTransaction(MobileTransaction):
    status: Literal["authorizing"]
    browser_nonce_hash: Digest
    oauth_state_hash: Digest


class MobileAuthorizedTransaction(MobileTransaction):
    status: Literal["authorized"]
    code_hash: Digest
    code_issued_at: EpochSeconds
    code_expires_at: EpochSeconds
    requester_key: OpaqueValue
    display_name: NonEmptyText = Field(repr=False)
    avatar_asset_key: str | None = Field(repr=False)
    guild_verified_at: AwareDatetime

    @model_validator(mode="after")
    def validate_code_lifetime(self) -> MobileAuthorizedTransaction:
        if not (
            self.created_at <= self.code_issued_at < self.code_expires_at <= self.expires_at
            and self.code_expires_at - self.code_issued_at <= MOBILE_CODE_TTL_SECONDS
        ):
            raise ValueError("invalid_mobile_code_lifetime")
        return self

    @model_validator(mode="after")
    def validate_avatar_asset(self) -> MobileAuthorizedTransaction:
        if self.avatar_asset_key not in (None, f"requesters/{self.requester_key}/avatar.webp"):
            raise ValueError("invalid_mobile_avatar_asset")
        return self


class MobileConsumedTransaction(MobileTransaction):
    status: Literal["consumed"]
    consumed_at: EpochSeconds

    @model_validator(mode="after")
    def validate_consumed_at(self) -> MobileConsumedTransaction:
        if not self.created_at <= self.consumed_at < self.expires_at:
            raise ValueError("invalid_mobile_consumption_time")
        return self


MobileTransactionState = Annotated[
    MobileStartedTransaction
    | MobileAuthorizingTransaction
    | MobileAuthorizedTransaction
    | MobileConsumedTransaction,
    Field(discriminator="status"),
]
MOBILE_TRANSACTION_ADAPTER = TypeAdapter(MobileTransactionState)

MOBILE_WIRE_MODELS = (
    MobileStartRequest,
    MobileStartResponse,
    MobileAuthorizeRequest,
    MobileCallbackParameters,
    MobileExchangeRequest,
    MobileSessionResponse,
    MobileExchangeResponse,
)


def build_mobile_auth_schema() -> dict[str, object]:
    """Unpublished wire contract, deliberately separate from live OpenAPI and Web validators."""

    definitions: dict[str, object] = {}
    for model in MOBILE_WIRE_MODELS:
        schema = model.model_json_schema(by_alias=True)
        definitions.update(schema.pop("$defs", {}))
        definitions[model.__name__] = schema
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://shittim-chest.invalid/contracts/records/v1/mobile-auth.schema.json",
        "title": "Records mobile authentication (not yet routed)",
        "$defs": definitions,
        "oneOf": [{"$ref": f"#/$defs/{model.__name__}"} for model in MOBILE_WIRE_MODELS],
    }
