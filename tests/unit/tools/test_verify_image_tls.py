"""Native TLS probe accepts only completed expected certificate verification."""

from __future__ import annotations

import asyncio
import json
import math
import re
import socket
import ssl
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from tools import verify_image_tls as verifier


def _attempts(result: dict[str, object]) -> list[dict[str, object]]:
    attempts = result["attempts"]
    assert isinstance(attempts, list)
    assert all(isinstance(attempt, dict) for attempt in attempts)
    return cast(list[dict[str, object]], attempts)


@pytest.mark.parametrize("accept", (False, True))
def test_a_completed_handshake_cannot_hide_accepted_incident_chain(monkeypatch, accept):
    class Writer:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def connect(*args, **kwargs):
        assert args == ("public.example", 443)
        assert kwargs["server_hostname"] == "public.example"
        assert kwargs["happy_eyeballs_delay"] == 0.25 and kwargs["interleave"] == 1
        return object(), Writer()

    monkeypatch.setattr(verifier.asyncio, "open_connection", connect)
    result = asyncio.run(
        verifier._handshake(ssl.create_default_context(), "public.example", accept)
    )
    assert result["passed"] is accept
    assert result["result"] == "accepted"
    assert len(_attempts(result)) == 1


@pytest.mark.parametrize("code,passed", ((20, True), (23, True), (10, False), (62, False)))
def test_only_untrusted_issuer_counts_as_incident_ca_rejection(monkeypatch, code, passed):
    calls = []

    async def connect(*args, **kwargs):
        calls.append(1)
        error = ssl.SSLCertVerificationError("public test certificate")
        error.verify_code = code
        raise error

    monkeypatch.setattr(verifier.asyncio, "open_connection", connect)
    result = asyncio.run(verifier._handshake(ssl.create_default_context(), "public.example", False))
    assert result["passed"] is passed
    assert len(calls) == 1
    assert _attempts(result)[0]["verify_code"] == code


@pytest.mark.parametrize(
    "failure,reason",
    (
        (TimeoutError, "timeout"),
        (socket.gaierror, "dns_error"),
        (ConnectionResetError, "connection_reset"),
        (ConnectionRefusedError, "connection_refused"),
        (OSError, "transport_error"),
    ),
)
def test_network_errors_are_unverified_and_fail_closed(monkeypatch, failure, reason):
    backoffs = []

    async def sleep(delay):
        backoffs.append(delay)

    async def connect(*args, **kwargs):
        raise failure("do not expose exception details")

    monkeypatch.setattr(verifier.asyncio, "open_connection", connect)
    monkeypatch.setattr(verifier.asyncio, "sleep", sleep)
    result = asyncio.run(verifier._handshake(ssl.create_default_context(), "public.example", False))
    assert result["passed"] is False
    assert result["result"] == "unverified_connection"
    assert result["reason"] == reason
    assert [attempt["attempt"] for attempt in _attempts(result)] == [1, 2, 3]
    assert all(attempt["passed"] is False for attempt in _attempts(result))
    assert backoffs == [0.25, 0.5]
    assert "details" not in json.dumps(result)


def test_ssl_protocol_failures_are_not_retried_or_mistaken_for_distrust(monkeypatch):
    calls = []

    async def connect(*args, **kwargs):
        calls.append(1)
        raise ssl.SSLError("do not expose SSL protocol details")

    monkeypatch.setattr(verifier.asyncio, "open_connection", connect)
    result = asyncio.run(verifier._handshake(ssl.create_default_context(), "public.example", False))
    assert result["passed"] is False
    assert result["result"] == result["reason"] == "tls_protocol_error"
    assert len(calls) == 1 and len(_attempts(result)) == 1
    assert "details" not in json.dumps(result)


@pytest.mark.parametrize("accepted", (False, True))
def test_transport_recovery_keeps_each_attempt_and_stops_on_certificate_result(
    monkeypatch, accepted
):
    calls = []
    backoffs = []

    class Writer:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def sleep(delay):
        backoffs.append(delay)

    async def connect(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("private detail")
        if accepted:
            return object(), Writer()
        error = ssl.SSLCertVerificationError("private detail")
        error.verify_code = 20
        raise error

    monkeypatch.setattr(verifier.asyncio, "open_connection", connect)
    monkeypatch.setattr(verifier.asyncio, "sleep", sleep)
    result = asyncio.run(verifier._handshake(ssl.create_default_context(), "public.example", False))
    assert len(calls) == 2
    assert result["passed"] is (not accepted)
    assert backoffs == [0.25]
    assert _attempts(result)[0]["result"] == "unverified_connection"
    assert _attempts(result)[0]["passed"] is False
    assert _attempts(result)[1]["result"] == ("accepted" if accepted else "certificate_rejected")
    assert "private detail" not in json.dumps(result)


def test_docker_budget_covers_the_full_bounded_retry_schedule():
    checks = 4 * (len(verifier.INCIDENT_HOSTS) + len(verifier.NORMAL_HOSTS))
    batches = math.ceil(checks / verifier.MAX_CONCURRENT_HANDSHAKES)
    per_check = (
        verifier.MAX_ATTEMPTS * verifier.ATTEMPT_TIMEOUT_SECONDS
        + sum(verifier.RETRY_BACKOFF_SECONDS * number for number in range(1, verifier.MAX_ATTEMPTS))
        + 1  # Completed TLS shutdown bound.
    )
    assert batches * per_check + 30 <= verifier.DOCKER_TIMEOUT_SECONDS


def test_fallback_context_loads_the_actual_botocore_bundle_without_ambient_roots(
    tmp_path: Path, monkeypatch
):
    from botocore.httpsession import DEFAULT_CA_BUNDLE

    # The fallback must stay independent of environment trust overrides and the
    # separately selected application certifi store.
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing-ca.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "missing-ca-directory"))
    monkeypatch.setattr(
        verifier.ssl, "get_default_verify_paths", lambda: SimpleNamespace(capath=str(tmp_path))
    )
    contexts = verifier._contexts()
    expected = {
        ssl.PEM_cert_to_DER_cert(certificate)
        for certificate in re.findall(
            r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
            Path(DEFAULT_CA_BUNDLE).read_text(),
            re.DOTALL,
        )
    }
    fallback = contexts["botocore_fallback"]
    assert expected and set(fallback.get_ca_certs(binary_form=True)) == expected
    assert set(contexts["application"].get_ca_certs(binary_form=True)) != expected
    assert fallback.verify_mode == ssl.CERT_REQUIRED
    assert fallback.check_hostname is True
    assert verifier._store_metadata(fallback)["incident_roots_absent"] is True


@pytest.mark.parametrize(
    "unsafe_metadata",
    (
        {},
        {"ca_count": 0},
        {"incident_roots_absent": False},
        {"verify_required": False},
        {"check_hostname": False},
    ),
)
def test_probe_covers_all_four_stores_and_12_bad_3_good_without_http(monkeypatch, unsafe_metadata):
    contexts = {
        name: ssl.create_default_context()
        for name in ("system", "system_capath_only", "application", "botocore_fallback")
    }
    monkeypatch.setattr(verifier, "_contexts", lambda: contexts)
    monkeypatch.setattr(
        verifier,
        "_store_metadata",
        lambda context: {
            "ca_count": 2,
            "incident_roots_absent": True,
            "verify_required": True,
            "check_hostname": True,
            **(unsafe_metadata if context is contexts["botocore_fallback"] else {}),
        },
    )

    async def handshake(context, host, accept):
        assert host in verifier.INCIDENT_HOSTS + verifier.NORMAL_HOSTS
        return {"host": host, "expected": "accept" if accept else "reject", "passed": True}

    monkeypatch.setattr(verifier, "_handshake", handshake)
    result = asyncio.run(verifier._probe())
    assert len(verifier.INCIDENT_HOSTS) == 12
    checks = result["checks"]
    assert isinstance(checks, list) and len(checks) == 60
    for name in contexts:
        store_checks = [check for check in checks if check["store"] == name]
        assert sum(check["expected"] == "reject" for check in store_checks) == 12
        assert sum(check["expected"] == "accept" for check in store_checks) == 3
    assert result["status"] == ("failed" if unsafe_metadata else "passed")
    assert result["http_sent"] is result["credentials_supplied"] is False


def test_host_uses_actual_read_only_image_and_preserves_its_identity(tmp_path, monkeypatch):
    commands = []

    def docker(arguments):
        commands.append(arguments)
        data = {
            "docker_image_id": "sha256:" + "a" * 64,
            "architecture": "arm64",
            "rootfs_diff_ids": [],
        }
        if arguments[0] == "run":
            data = {"status": "passed", "checks": []}
        return subprocess.CompletedProcess(arguments, 0, stdout=json.dumps(data))

    monkeypatch.setattr(verifier, "_docker", docker)
    digest = "sha256:" + "b" * 64
    (tmp_path / "production-image-config-digest.txt").write_text(digest)
    output = tmp_path / "image-tls-arm64.json"
    report = verifier.verify_image("local:production", output)
    assert report["architecture"] == "arm64"
    assert report["image_config_digest"] == digest
    assert report["docker_image_id"] != report["image_config_digest"]
    assert "--read-only" in commands[1]
    assert "--cap-drop=ALL" in commands[1]
    assert not any("--env" in arg or "--privileged" in arg for arg in commands[1])
    assert json.loads(output.read_text()) == report


def test_container_failure_cannot_leave_a_passed_report(tmp_path: Path, monkeypatch):
    def docker(arguments):
        if arguments[0] == "run":
            return subprocess.CompletedProcess(arguments, 1, stdout='{"status":"passed"}')
        return subprocess.CompletedProcess(arguments, 0, stdout='{"architecture":"arm64"}')

    monkeypatch.setattr(verifier, "_docker", docker)
    report = verifier.verify_image("local:production", tmp_path / "report.json")
    assert report["status"] == "failed"
