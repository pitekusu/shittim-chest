"""Narrow Parameter Store reader for ingress configuration values."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING

from botocore.exceptions import BotoCoreError, ClientError

from shittim_chest.application.ports import ParameterReadUnavailable

if TYPE_CHECKING:
    from mypy_boto3_ssm.client import SSMClient


class SsmParameterReader:
    """Read one explicitly named parameter without exposing error content."""

    __slots__ = ("_client",)

    def __init__(self, *, client: SSMClient) -> None:
        self._client = client

    async def get_parameter(self, name: str, *, with_decryption: bool = True) -> str:
        """Return a parameter value without blocking the event loop."""

        if not name or name != name.strip():
            raise ValueError("parameter name must not be empty or padded")
        if not isinstance(with_decryption, bool):
            raise TypeError("with_decryption must be a boolean")
        return await asyncio.to_thread(self._get_parameter, name, with_decryption)

    async def get_parameters(
        self,
        names: tuple[str, ...],
        *,
        with_decryption: bool = True,
    ) -> Mapping[str, str]:
        """Return one exact batch of explicitly named parameters."""

        if not names or len(names) > 10 or len(set(names)) != len(names):
            raise ValueError("parameter names must be a unique non-empty batch of at most ten")
        if any(not name or name != name.strip() for name in names):
            raise ValueError("parameter names must not be empty or padded")
        if not isinstance(with_decryption, bool):
            raise TypeError("with_decryption must be a boolean")
        return await asyncio.to_thread(self._get_parameters, names, with_decryption)

    def _get_parameter(self, name: str, with_decryption: bool) -> str:
        try:
            response = self._client.get_parameter(
                Name=name,
                WithDecryption=with_decryption,
            )
        except BotoCoreError, ClientError:
            raise ParameterReadUnavailable from None
        parameter = response.get("Parameter")
        if not isinstance(parameter, Mapping):
            raise ParameterReadUnavailable
        value = parameter.get("Value")
        if not isinstance(value, str) or not value:
            raise ParameterReadUnavailable
        return value

    def _get_parameters(
        self,
        names: tuple[str, ...],
        with_decryption: bool,
    ) -> Mapping[str, str]:
        try:
            response = self._client.get_parameters(
                Names=list(names),
                WithDecryption=with_decryption,
            )
        except BotoCoreError, ClientError:
            raise ParameterReadUnavailable from None
        invalid = response.get("InvalidParameters")
        parameters = response.get("Parameters")
        if invalid not in (None, []) or not isinstance(parameters, list):
            raise ParameterReadUnavailable
        values: dict[str, str] = {}
        for parameter in parameters:
            if not isinstance(parameter, Mapping):
                raise ParameterReadUnavailable
            name = parameter.get("Name")
            value = parameter.get("Value")
            if not isinstance(name, str) or not isinstance(value, str) or not value:
                raise ParameterReadUnavailable
            values[name] = value
        if set(values) != set(names):
            raise ParameterReadUnavailable
        return values
