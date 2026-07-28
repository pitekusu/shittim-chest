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
