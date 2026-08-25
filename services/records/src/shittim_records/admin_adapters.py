"""AWS adapters for Records ADMIN authorization."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_ssm.client import SSMClient

from shittim_records.admin import AdminFailure, AdminSecurityConfiguration
from shittim_records.auth import RecordsOAuthConfig


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
