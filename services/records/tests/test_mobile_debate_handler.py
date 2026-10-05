"""Anonymous cold starts and private-error logging remain bounded."""

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from tests.test_mobile_debate import NOW, REQUEST_ID, event

from shittim_records import mobile_debate_handler
from shittim_records.contracts import DebateRequestResponse, DebateStartRequest


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"headers": {}}, 401),
        ({"headers": {"authorization": "Bearer invalid"}}, 401),
        ({"cookies": ["session=fixture"]}, 400),
    ],
)
def test_anonymous_and_mixed_credentials_fail_before_configuration(monkeypatch, changes, status):
    async def unavailable():
        pytest.fail("configuration/secrets must not be read")

    monkeypatch.setattr(mobile_debate_handler, "_CONTROLLER", None)
    monkeypatch.setattr(mobile_debate_handler, "_controller", unavailable)
    response = mobile_debate_handler.handler(event(**changes), None)
    assert response["statusCode"] == status
    assert response["headers"]["Cache-Control"] == "private, no-store"


def test_sdk_exceptions_never_log_private_payload_or_traceback(monkeypatch, caplog):
    private_input = "Fictional sensitive question"

    def fail(_event):
        raise RuntimeError(f"question={private_input}; token=fictional-private-token")

    monkeypatch.setattr(mobile_debate_handler, "_CONTROLLER", SimpleNamespace(handle=fail))
    response = mobile_debate_handler.handler(event(), None)
    assert response["statusCode"] == 503
    assert json.loads(response["body"])["error"]["code"] == "DEBATE_REQUESTS_UNAVAILABLE"
    assert private_input not in caplog.text and "fictional-private-token" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_submission_models_omit_question_from_repr_and_validation_text():
    private_input = "Fictional private question"
    request = DebateStartRequest(request_id=REQUEST_ID, question=private_input)
    response = DebateRequestResponse(
        request_id=REQUEST_ID,
        question=private_input,
        status="accepted",
        phase=None,
        created_at=NOW,
        updated_at=NOW,
        record_id=None,
        error_code=None,
    )
    assert private_input not in repr(request) and private_input not in repr(response)
    with pytest.raises(ValidationError) as error:
        DebateStartRequest(request_id=REQUEST_ID, question=private_input * 100)
    assert private_input not in str(error.value)
