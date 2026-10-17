"""Native acceptance, ownership, publication and credential boundaries."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from shittim_chest.application.discord import (
    DiscordBotSlot,
    DiscordIdentityConfig,
    DiscordRuntimeConfig,
    PanelAction,
)
from shittim_chest.application.ports import RepositoryIdentityConflict, RepositoryQueueFull
from shittim_chest.application.scale_to_zero import (
    EnqueuedIngress,
    IngressKind,
    IngressOperationResult,
    IngressStatus,
)
from shittim_chest.domain import AttemptId, DebateId, DebatePhase

from shittim_records.archive import derive_requester_key
from shittim_records.auth import AuthFailure, _digest
from shittim_records.contracts import DebateControlRequest, DebateStartRequest
from shittim_records.mobile_auth import MOBILE_SESSION_TTL_SECONDS, MobileSessionRecord
from shittim_records.mobile_debate import (
    DebateProgress,
    DebateRequestCursor,
    DebateRequestFailure,
    DebateRequestPage,
    MobileDebateService,
)
from shittim_records.mobile_debate_http import MobileDebateHttpController

NOW = datetime(2026, 10, 4, tzinfo=UTC)
KEY = b"fictional-unit-test-key-material-32"
TOKEN = "t" * 43
USER = str(10**17 + 1)
CHANNEL = str(10**17 + 2)
GUILD = str(10**17 + 3)
REQUEST_ID = "00000000-0000-4000-8000-00000000000a"


class Sessions:
    def __init__(self):
        self.session = MobileSessionRecord(
            requester_key=derive_requester_key(KEY, USER),
            display_name="Fictional requester",
            avatar_asset_key=None,
            guild_verified_at=NOW,
            discord_user_id=USER,
            discord_username="fictional-requester",
            created_at=int(NOW.timestamp()),
            expires_at=int(NOW.timestamp()) + MOBILE_SESSION_TTL_SECONDS,
        )

    def get_session(self, *, session_hash):
        return self.session if session_hash == _digest(KEY, "mobile-session", TOKEN) else None

    def delete_session(self, *, session_hash):
        if session_hash == _digest(KEY, "mobile-session", TOKEN):
            self.session = None


class Requests:
    def __init__(self):
        self.requests = {}
        self.saved = []
        self.phase = DebateProgress()
        self.full = False

    async def get(self, *, owner_key, request_id):
        return self.requests.get((owner_key, request_id))

    async def enqueue(self, request, *, session_hash, session, now):
        if self.full:
            raise RepositoryQueueFull()
        self.saved.append((session_hash, session, now))
        self.requests[(request.owner_key, request.mobile_request_id)] = request
        return EnqueuedIngress(
            request,
            IngressOperationResult(
                operation_id=request.operation_id,
                interaction_id=request.interaction_id,
                request_sort_key="fictional-sort-key",
                status=request.status,
                created_at=request.created_at,
                updated_at=request.updated_at,
            ),
            True,
        )

    async def page(self, *, owner_key, limit, after):
        records = tuple(
            request for (owner, _), request in self.requests.items() if owner == owner_key
        )
        return DebateRequestPage(records[:limit])

    def progress(self, request):
        return self.phase


class Permissions:
    def __init__(self):
        self.checked = []
        self.fail = None
        self.before_return = lambda: None

    async def check(self, *, requester_id):
        self.checked.append(requester_id)
        self.before_return()
        if self.fail:
            raise DebateRequestFailure(self.fail, 403)


@pytest.fixture
def setup():
    sessions, repository, permissions = Sessions(), Requests(), Permissions()
    runtime = DiscordRuntimeConfig(
        guild_id=GUILD,
        allowed_channel_ids=frozenset({CHANNEL}),
        farewell_channel_id=CHANNEL,
        identities=tuple(
            DiscordIdentityConfig(slot, str(10**17 + 10 + index))
            for index, slot in enumerate(DiscordBotSlot)
        ),
        schema_version="v0001",
    )
    service = MobileDebateService(
        sessions=sessions,
        repository=repository,
        permissions=permissions,
        runtime=runtime,
        channel_id=CHANNEL,
        enabled=True,
        session_key=KEY,
        identity_key=KEY,
        clock=lambda: NOW,
    )
    return service, sessions, repository, permissions


def start(service, question="  Fictional question 😀  "):
    return asyncio.run(
        service.start(
            token=TOKEN,
            payload=DebateStartRequest(request_id=REQUEST_ID, question=question),
        )
    )


def test_submission_preserves_input_and_replays_before_profile_or_channel_changes(setup):
    service, sessions, repository, permissions = setup
    first, created = start(service)
    assert created and first.question == "  Fictional question 😀  "
    assert first.status == "accepted" and first.record_id is None
    sessions.session = sessions.session.model_copy(update={"display_name": "Renamed"})
    service._enabled = False
    second, created = start(service)
    assert not created and second == first
    assert permissions.checked == [USER] and len(repository.saved) == 1
    assert not any(
        private in first.model_dump_json(by_alias=True)
        for private in (
            USER,
            GUILD,
            CHANNEL,
            sessions.session.requester_key,
            TOKEN,
        )
    )
    with pytest.raises(DebateRequestFailure, match="REQUEST_ID_CONFLICT"):
        start(service, "Different question")


@pytest.mark.parametrize("same_question", [True, False])
def test_concurrent_submission_converges_to_winning_receipt(setup, monkeypatch, same_question):
    service, _, repository, _ = setup
    question = "Fictional racing question"
    captured_enqueue = repository.enqueue

    async def race(request, **kwargs):
        # Simulate the other POST accepting between this POST's get and enqueue,
        # with a changed display name but the same immutable owner/request ID.
        competing = replace(request, requester_display_name="Renamed elsewhere")
        if not same_question:
            competing = replace(competing, question="Different fictional question")
        await captured_enqueue(competing, **kwargs)
        raise RepositoryIdentityConflict()

    monkeypatch.setattr(repository, "enqueue", race)
    if same_question:
        response, created = start(service, question)
        assert not created and response.question == question
        assert len(repository.saved) == 1
    else:
        with pytest.raises(DebateRequestFailure, match="REQUEST_ID_CONFLICT"):
            start(service, question)


def test_starting_requires_confirmed_saved_status_message(setup):
    service, _, repository, _ = setup
    start(service)
    request = next(iter(repository.requests.values()))
    repository.requests[(request.owner_key, request.mobile_request_id)] = replace(
        request, status_message_id=str(10**17 + 20), status_message_updated_at=NOW
    )
    assert asyncio.run(service.get(token=TOKEN, request_id=REQUEST_ID)).status == "starting"


@pytest.mark.parametrize(
    "code,revoked",
    [
        ("GUILD_MEMBERSHIP_REQUIRED", True),
        ("CHANNEL_PERMISSION_REQUIRED", False),
        ("DISCORD_UNAVAILABLE", False),
    ],
)
def test_only_confirmed_guild_departure_revokes_presented_session(setup, code, revoked):
    service, sessions, _, permissions = setup
    permissions.fail = code
    with pytest.raises(DebateRequestFailure, match=code):
        start(service)
    assert (sessions.session is None) == revoked
    # Same token cannot obtain another read authorization after confirmed loss.
    if revoked:
        http = MobileDebateHttpController(service, allowed_origin="https://records.example")
        result = http.handle(event(method="GET", body=""))
        assert result["statusCode"] == 401
        assert json.loads(result["body"])["error"]["code"] == "AUTHENTICATION_REQUIRED"
        assert "question" not in result["body"]


@pytest.mark.parametrize(
    "condition,code",
    [
        ("legacy", "DEBATE_START_REAUTH_REQUIRED"),
        ("disabled", "DEBATE_START_DISABLED"),
        ("permission", "CHANNEL_PERMISSION_REQUIRED"),
        ("full", "DEBATE_QUEUE_FULL"),
        ("identity", "session_required"),
        ("revoked", "session_required"),
    ],
)
def test_new_submission_fails_closed_without_faking_acceptance(setup, condition, code):
    service, sessions, repository, permissions = setup
    if condition == "legacy":
        sessions.session = sessions.session.model_copy(
            update={"discord_user_id": None, "discord_username": None}
        )
    elif condition == "disabled":
        service._enabled = False
    elif condition == "permission":
        permissions.fail = "CHANNEL_PERMISSION_REQUIRED"
    elif condition == "full":
        repository.full = True
    elif condition == "identity":
        sessions.session = sessions.session.model_copy(update={"requester_key": "a" * 43})
    else:
        permissions.before_return = lambda: setattr(sessions, "session", None)
    with pytest.raises((DebateRequestFailure, AuthFailure), match=code):
        start(service)
    assert not repository.saved


def test_legacy_session_can_read_own_requests_without_enabling_new_submissions(setup):
    service, sessions, _, _ = setup
    first, _ = start(service)
    sessions.session = sessions.session.model_copy(
        update={"discord_user_id": None, "discord_username": None}
    )
    assert asyncio.run(service.get(token=TOKEN, request_id=REQUEST_ID)) == first
    assert asyncio.run(service.list(token=TOKEN)).items == [first]
    sessions.session = sessions.session.model_copy(update={"requester_key": "a" * 43})
    with pytest.raises(DebateRequestFailure, match="DEBATE_REQUEST_NOT_FOUND"):
        asyncio.run(service.get(token=TOKEN, request_id=REQUEST_ID))


@pytest.mark.parametrize(
    "phase,status",
    [
        (DebatePhase.ACCEPTED, "queued"),
        (DebatePhase.DISCUSSING, "running"),
        (DebatePhase.COMPLETED, "publishing"),
        (DebatePhase.CANCELLED, "cancelled"),
        (DebatePhase.FAILED, "failed"),
    ],
)
def test_phase_is_not_publication_and_terminal_errors_are_allowlisted(setup, phase, status):
    service, _, repository, _ = setup
    start(service)
    repository.phase = DebateProgress(phase, NOW + timedelta(seconds=1))
    view = asyncio.run(service.get(token=TOKEN, request_id=REQUEST_ID))
    assert view.status == status and view.phase == phase.value and view.record_id is None
    repository.phase = DebateProgress(DebatePhase.COMPLETED, NOW + timedelta(seconds=2), "r" * 43)
    assert asyncio.run(service.get(token=TOKEN, request_id=REQUEST_ID)).status == "published"


def test_cursor_is_owner_query_and_expiry_bound_without_private_keys():
    codec = DebateRequestCursor(KEY)
    cursor = codec.encode(owner="a" * 43, limit=20, at=NOW, request_id=REQUEST_ID, now=NOW)
    assert codec.decode(owner="a" * 43, limit=20, cursor=cursor, now=NOW) == (NOW, REQUEST_ID)
    for owner, limit, now in (
        ("b" * 43, 20, NOW),
        ("a" * 43, 21, NOW),
        ("a" * 43, 20, NOW + timedelta(hours=1)),
    ):
        with pytest.raises(DebateRequestFailure, match="CURSOR_INVALID"):
            codec.decode(owner=owner, limit=limit, cursor=cursor, now=now)


def event(body=None, method="POST", suffix="", **extra):
    return {
        "routeKey": f"{method} /api/v1/debate-requests{suffix}",
        "requestContext": {"requestId": "fixture-http-id"},
        "rawQueryString": "",
        "headers": {"authorization": f"Bearer {TOKEN}", "content-type": "application/json"},
        "pathParameters": {"requestId": REQUEST_ID},
        "body": json.dumps({"requestId": REQUEST_ID, "question": "Fixture"})
        if body is None
        else body,
        **extra,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"cookies": ["session=fixture"]},
        {"headers": {"cookie": "session=fixture"}},
        {"isBase64Encoded": True},
        {"body": '{"requestId":"a","requestId":"b","question":"Fixture"}'},
        {"body": json.dumps({"requestId": REQUEST_ID, "question": " "})},
        {"body": json.dumps({"requestId": REQUEST_ID, "question": "x" * 1001})},
        {"body": json.dumps({"requestId": REQUEST_ID, "question": "Fixture", "requesterId": USER})},
    ],
)
def test_http_rejects_ambient_credentials_duplicate_keys_and_client_identity(setup, change):
    service, _, repository, _ = setup
    response = MobileDebateHttpController(service, allowed_origin="https://records.example").handle(
        event(**change)
    )
    assert response["statusCode"] == 400 and not repository.saved
    assert response["headers"]["Cache-Control"] == "private, no-store"


def test_http_accepts_astral_codepoints_then_replays_and_requires_bearer(setup):
    service, _, repository, _ = setup
    http = MobileDebateHttpController(service, allowed_origin="https://records.example")
    data = event(body=json.dumps({"requestId": REQUEST_ID, "question": "😀" * 1000}))
    assert http.handle(data)["statusCode"] == 202
    assert http.handle(data)["statusCode"] == 202 and len(repository.saved) == 1
    data["headers"].pop("authorization")
    assert http.handle(data)["statusCode"] == 401


OPERATION_ID = "00000000-0000-4000-8000-00000000000b"
SECOND_OPERATION_ID = "00000000-0000-4000-8000-00000000000c"


def prepared_control(setup, phase=DebatePhase.DISCUSSING):
    service, _, repository, _ = setup
    start(service)
    key, request = next(iter(repository.requests.items()))
    attempt_id = AttemptId.new()
    repository.requests[key] = replace(
        request,
        accepted_debate_id=DebateId.new(),
        accepted_attempt_id=attempt_id,
    )
    repository.phase = DebateProgress(
        phase=phase,
        updated_at=NOW,
        attempt_id=attempt_id,
        thread_id=str(10**17 + 40),
        control_panel_message_id=str(10**17 + 41),
    )
    return asyncio.run(service.get(token=TOKEN, request_id=REQUEST_ID))


def submit_control(service, receipt, *, action=PanelAction.CANCEL, operation_id=OPERATION_ID):
    return asyncio.run(
        service.control(
            token=TOKEN,
            request_id=REQUEST_ID,
            action=action,
            payload=DebateControlRequest(
                operation_id=operation_id,
                attempt_token=receipt.attempt_token,
            ),
        )
    )


@pytest.mark.parametrize(
    "action,phase",
    [
        (PanelAction.CANCEL, DebatePhase.DISCUSSING),
        (PanelAction.RETRY, DebatePhase.FAILED),
    ],
)
def test_control_reuses_durable_ingress_and_replays_original_attempt(setup, action, phase):
    service, _, repository, permissions = setup
    receipt = prepared_control(setup, phase)
    assert receipt.can_cancel == (action is PanelAction.CANCEL)
    assert receipt.can_retry == (action is PanelAction.RETRY)
    assert len(receipt.attempt_token) == 64
    result = submit_control(service, receipt, action=action)
    assert result.status == "accepted" and result.action == action.value
    assert not result.request.can_cancel and not result.request.can_retry
    operation = repository.requests[(next(iter(repository.requests))[0], OPERATION_ID)]
    assert operation.kind is IngressKind(action.value)
    assert not operation.requester_can_manage_messages
    assert operation.mobile_target_request_id == REQUEST_ID
    assert operation.expected_attempt_id == repository.phase.attempt_id
    assert operation.source_thread_id == repository.phase.thread_id
    assert operation.source_message_id == repository.phase.control_panel_message_id
    assert len(repository.saved) == 2 and permissions.checked == [USER, USER]
    # Ambiguous response retries resolve frozen content even after the attempt,
    # enabled flag, mutable profile or Discord authorization changes.
    repository.phase = replace(repository.phase, attempt_id=AttemptId.new())
    service._enabled = False
    permissions.fail = "DISCORD_UNAVAILABLE"
    replay = submit_control(service, receipt, action=action)
    assert replay.operation_id == OPERATION_ID and len(repository.saved) == 2
    assert permissions.checked == [USER, USER]
    with pytest.raises(DebateRequestFailure, match="REQUEST_ID_CONFLICT"):
        submit_control(
            service,
            receipt,
            action=(PanelAction.RETRY if action is PanelAction.CANCEL else PanelAction.CANCEL),
        )


def test_stale_attempt_and_invalid_phase_never_enqueue_control(setup):
    service, _, repository, _ = setup
    receipt = prepared_control(setup)
    repository.phase = replace(repository.phase, attempt_id=AttemptId.new())
    with pytest.raises(DebateRequestFailure, match="DEBATE_CONTROL_STALE"):
        submit_control(service, receipt)
    receipt = asyncio.run(service.get(token=TOKEN, request_id=REQUEST_ID))
    with pytest.raises(DebateRequestFailure, match="DEBATE_CONTROL_NOT_ALLOWED"):
        submit_control(service, receipt, action=PanelAction.RETRY)
    repository.phase = replace(repository.phase, phase=DebatePhase.COMPLETED)
    with pytest.raises(DebateRequestFailure, match="DEBATE_CONTROL_NOT_ALLOWED"):
        submit_control(service, receipt)
    assert len(repository.saved) == 1


@pytest.mark.parametrize(
    "action,phase",
    [
        (PanelAction.CANCEL, DebatePhase.DISCUSSING),
        (PanelAction.RETRY, DebatePhase.FAILED),
    ],
)
def test_operation_poll_tracks_acceptance_failure_and_owner_boundary(setup, action, phase):
    service, sessions, repository, _ = setup
    receipt = prepared_control(setup, phase)
    submit_control(service, receipt, action=action)
    key = (sessions.session.requester_key, OPERATION_ID)
    operation = repository.requests[key]
    repository.requests[key] = replace(
        operation,
        status=IngressStatus.ACCEPTED,
        accepted_debate_id=operation.target_debate_id,
        accepted_attempt_id=operation.expected_attempt_id,
    )
    if action is PanelAction.CANCEL:
        pending = asyncio.run(
            service.get_operation(
                token=TOKEN,
                request_id=REQUEST_ID,
                operation_id=OPERATION_ID,
            )
        )
        assert pending.status == "completed"
        repository.phase = replace(repository.phase, phase=DebatePhase.CANCELLED)
    result = asyncio.run(
        service.get_operation(
            token=TOKEN,
            request_id=REQUEST_ID,
            operation_id=OPERATION_ID,
        )
    )
    assert result.status == "completed"
    repository.requests[key] = replace(
        operation,
        status=IngressStatus.REJECTED,
        completed_at=NOW,
        error_code="private_detail",
    )
    failed = asyncio.run(
        service.get_operation(
            token=TOKEN,
            request_id=REQUEST_ID,
            operation_id=OPERATION_ID,
        )
    )
    assert failed.status == "failed" and failed.error_code == "DEBATE_CONTROL_FAILED"
    assert "private_detail" not in failed.model_dump_json(by_alias=True)
    sessions.session = sessions.session.model_copy(update={"requester_key": "another-owner"})
    with pytest.raises(DebateRequestFailure, match="DEBATE_REQUEST_NOT_FOUND"):
        asyncio.run(
            service.get_operation(
                token=TOKEN,
                request_id=REQUEST_ID,
                operation_id=OPERATION_ID,
            )
        )


def test_control_revoked_session_and_lost_channel_permission_do_not_enqueue(setup):
    service, sessions, repository, permissions = setup
    receipt = prepared_control(setup)
    permissions.fail = "CHANNEL_PERMISSION_REQUIRED"
    with pytest.raises(DebateRequestFailure, match="CHANNEL_PERMISSION_REQUIRED"):
        submit_control(service, receipt)
    permissions.fail = None
    permissions.before_return = lambda: sessions.delete_session(
        session_hash=_digest(KEY, "mobile-session", TOKEN),
    )
    with pytest.raises(AuthFailure, match="session_required"):
        submit_control(service, receipt)
    assert len(repository.saved) == 1


def test_control_http_receipt_lookup_and_strict_boundary(setup):
    service, _, repository, _ = setup
    receipt = prepared_control(setup)
    http = MobileDebateHttpController(service, allowed_origin="https://records.example")
    body = {"operationId": OPERATION_ID, "attemptToken": receipt.attempt_token}
    request = event(body=json.dumps(body), suffix="/{requestId}/cancel")
    assert http.handle(request)["statusCode"] == 202
    lookup = event(
        method="GET",
        body="",
        suffix="/{requestId}/operations/{operationId}",
        pathParameters={"requestId": REQUEST_ID, "operationId": OPERATION_ID},
    )
    response = http.handle(lookup)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["operationId"] == OPERATION_ID
    assert response["headers"]["Cache-Control"] == "private, no-store"
    for invalid in (
        {**body, "requesterId": USER},
        {**body, "canManageMessages": True},
        {**body, "attemptToken": "invalid"},
        {**body, "operationId": REQUEST_ID},
    ):
        response = http.handle(event(body=json.dumps(invalid), suffix="/{requestId}/cancel"))
        assert response["statusCode"] in {400, 409}
    lookup["pathParameters"]["operationId"] = SECOND_OPERATION_ID
    assert http.handle(lookup)["statusCode"] == 404
    assert len(repository.saved) == 2


@pytest.mark.parametrize("old_status", [IngressStatus.FAILED, IngressStatus.COMPLETED])
def test_retried_current_phase_overrides_original_ingress_terminal_status(setup, old_status):
    service, _, repository, _ = setup
    prepared_control(setup)
    key, request = next(iter(repository.requests.items()))
    repository.requests[key] = replace(request, status=old_status, completed_at=NOW)
    response = asyncio.run(service.get(token=TOKEN, request_id=REQUEST_ID))
    assert response.status == "running" and response.can_cancel
    repository.phase = replace(repository.phase, phase=DebatePhase.ACCEPTED)
    assert asyncio.run(service.get(token=TOKEN, request_id=REQUEST_ID)).status == "queued"


def test_cancel_operation_completion_survives_later_current_attempt(setup):
    service, sessions, repository, _ = setup
    receipt = prepared_control(setup)
    submit_control(service, receipt)
    key = (sessions.session.requester_key, OPERATION_ID)
    operation = repository.requests[key]
    repository.requests[key] = replace(
        operation,
        status=IngressStatus.ACCEPTED,
        accepted_debate_id=operation.target_debate_id,
        accepted_attempt_id=operation.expected_attempt_id,
    )
    repository.phase = replace(repository.phase, attempt_id=AttemptId.new())
    result = asyncio.run(
        service.get_operation(
            token=TOKEN,
            request_id=REQUEST_ID,
            operation_id=OPERATION_ID,
        )
    )
    assert result.status == "completed"
    assert result.request.attempt_token != receipt.attempt_token


@pytest.mark.parametrize("same_binding", [True, False])
def test_concurrent_control_submission_replays_only_identical_binding(
    setup, monkeypatch, same_binding
):
    service, _, repository, _ = setup
    receipt = prepared_control(setup)
    captured = repository.enqueue

    async def race(request, **kwargs):
        competing = request if same_binding else replace(request, kind=IngressKind.RETRY)
        await captured(competing, **kwargs)
        raise RepositoryIdentityConflict()

    monkeypatch.setattr(repository, "enqueue", race)
    if same_binding:
        assert submit_control(service, receipt).operation_id == OPERATION_ID
    else:
        with pytest.raises(DebateRequestFailure, match="REQUEST_ID_CONFLICT"):
            submit_control(service, receipt)
    assert len(repository.saved) == 2
