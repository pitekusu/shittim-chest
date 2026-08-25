"""AWS adapter contracts for Records ADMIN authorization."""

from __future__ import annotations

from typing import Any, cast

import pytest

from shittim_records.admin import AdminFailure
from shittim_records.admin_adapters import AdminSecurityConfigurationRepository


class FakeSsm:
    def get_parameters(self, **kwargs: Any) -> dict[str, Any]:
        private_user_id = "123456789" + "01234567"
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


def test_admin_configuration_validation_drops_private_exception_context() -> None:
    private_user_id = "123456789" + "01234567"
    repository = AdminSecurityConfigurationRepository(
        cast(Any, FakeSsm()),
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
