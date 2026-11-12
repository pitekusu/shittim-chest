"""Lambda entry point for browser and mobile authentication."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3

from shittim_records.auth import AuthConfiguration, AuthFailure, AuthService
from shittim_records.auth_adapters import (
    AuthConfigurationRepository,
    DiscordOAuthClient,
    DynamoAuthStore,
    S3AvatarStore,
)
from shittim_records.http_api import AuthHttpController
from shittim_records.lambda_support import (
    SDK_CONFIG,
    content_free_http_failure,
    environment,
    regional_s3_client,
)

if TYPE_CHECKING:
    import httpx2
    from mypy_boto3_dynamodb.client import DynamoDBClient

    from shittim_records.mobile_http import MobileAuthHttpController
    from shittim_records.mobile_notifications import MobileNotificationRegistrationService

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
_AUTH_CONTROLLER: AuthHttpController | None = None


def auth_handler(event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    """Handle public OAuth and session routes."""

    # Discord member/avatar paths and HTTP headers contain private identifiers.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        return _auth_controller().handle(event, now=datetime.now(UTC))
    except Exception as error:
        return content_free_http_failure(
            event,
            code="RECORDS_UNAVAILABLE",
            log_event="records_auth_request_failed",
            error=error,
            logger=LOGGER,
        )


def _auth_controller() -> AuthHttpController:
    global _AUTH_CONTROLLER
    if _AUTH_CONTROLLER is not None:
        return _AUTH_CONTROLLER

    # Reuse successful initialization within a warm Lambda environment. A failed
    # factory call is retried on the next invocation.
    @cache
    def configuration() -> AuthConfiguration:
        repository = AuthConfigurationRepository(
            boto3.client("ssm", config=SDK_CONFIG),
            identity_parameter_name=environment("IDENTITY_HMAC_PARAMETER_NAME"),
            oauth_parameter_name=environment("OAUTH_CONFIG_PARAMETER_NAME"),
            client_secret_parameter_name=environment("OAUTH_CLIENT_SECRET_PARAMETER_NAME"),
            session_key_parameter_name=environment("SESSION_KEY_PARAMETER_NAME"),
            admin_user_id_parameter_name=environment("ADMIN_DISCORD_USER_ID_PARAMETER_NAME"),
        )
        try:
            return repository.load()
        except AuthFailure:
            # Keep configuration errors at the Lambda boundary, outside the
            # OAuth controller's handling of request and provider failures.
            raise RuntimeError("Records authentication configuration is unavailable") from None

    @cache
    def dynamodb() -> DynamoDBClient:
        return boto3.client("dynamodb", config=SDK_CONFIG)

    @cache
    def avatars() -> S3AvatarStore:
        return S3AvatarStore(cache(regional_s3_client), environment("MEDIA_BUCKET_NAME"))

    discord = DiscordOAuthClient(cache(_discord_http_client))

    @cache
    def browser() -> AuthService:
        return AuthService(
            store=DynamoAuthStore(dynamodb(), environment("SESSION_TABLE_NAME")),
            discord=discord,
            avatars=avatars(),
            configuration=configuration(),
        )

    @cache
    def mobile() -> MobileAuthHttpController:
        from shittim_records.mobile_auth_adapters import DynamoMobileAuthStore
        from shittim_records.mobile_callback import MobileCallbackService
        from shittim_records.mobile_exchange import MobileExchangeService
        from shittim_records.mobile_http import MobileAuthHttpController
        from shittim_records.mobile_login import MobileLoginService
        from shittim_records.mobile_session import MobileSessionService

        config = configuration()
        store = DynamoMobileAuthStore(dynamodb(), environment("SESSION_TABLE_NAME"))
        avatar_store = avatars()

        @cache
        def notifications() -> MobileNotificationRegistrationService:
            from shittim_records.mobile_notification_adapters import DynamoMobileNotificationStore
            from shittim_records.mobile_notifications import MobileNotificationRegistrationService

            return MobileNotificationRegistrationService(
                sessions=store,
                store=DynamoMobileNotificationStore(
                    boto3.resource("dynamodb", config=SDK_CONFIG),
                    statistics=environment("STATISTICS_TABLE_NAME"),
                    sessions=environment("SESSION_TABLE_NAME"),
                ),
                session_key=config.session_hmac_key,
            )

        return MobileAuthHttpController(
            login=MobileLoginService(
                store=store,
                discord=discord,
                oauth=config.oauth,
                hmac_key=config.session_hmac_key,
            ),
            callback=MobileCallbackService(
                store=store,
                discord=discord,
                avatars=avatar_store,
                configuration=config,
            ),
            exchange=MobileExchangeService(
                store=store,
                avatars=avatar_store,
                session_key=config.session_hmac_key,
                admin_requester_key=config.admin_requester_key,
            ),
            sessions=MobileSessionService(
                store=store,
                avatars=avatar_store,
                session_key=config.session_hmac_key,
                admin_requester_key=config.admin_requester_key,
            ),
            allowed_origin=config.oauth.allowed_origin,
            notifications=notifications,
        )

    _AUTH_CONTROLLER = AuthHttpController(browser, mobile)
    return _AUTH_CONTROLLER


def _discord_http_client() -> httpx2.Client:
    import httpx2
    from shittim_chest.adapters.tls import create_public_tls_context

    return httpx2.Client(
        timeout=httpx2.Timeout(3.0, connect=2.0),
        follow_redirects=False,
        verify=create_public_tls_context(),
    )
