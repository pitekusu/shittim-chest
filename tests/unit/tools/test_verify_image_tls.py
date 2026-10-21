"""Native TLS probe accepts only completed expected certificate verification."""

from __future__ import annotations

import asyncio
import json
import ssl
import subprocess
from pathlib import Path

import pytest
from tools import verify_image_tls as verifier


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
        return object(), Writer()

    monkeypatch.setattr(verifier.asyncio, "open_connection", connect)
    result = asyncio.run(
        verifier._handshake(ssl.create_default_context(), "public.example", accept)
    )
    assert result["passed"] is accept
    assert result["result"] == "accepted"


@pytest.mark.parametrize("code,passed", ((20, True), (23, True), (10, False), (62, False)))
def test_only_untrusted_issuer_counts_as_incident_ca_rejection(monkeypatch, code, passed):
    async def connect(*args, **kwargs):
        error = ssl.SSLCertVerificationError("public test certificate")
        error.verify_code = code
        raise error

    monkeypatch.setattr(verifier.asyncio, "open_connection", connect)
    result = asyncio.run(verifier._handshake(ssl.create_default_context(), "public.example", False))
    assert result["passed"] is passed


@pytest.mark.parametrize("failure", (TimeoutError, OSError))
def test_network_errors_are_unverified_and_fail_closed(monkeypatch, failure):
    async def connect(*args, **kwargs):
        raise failure("do not expose exception details")

    monkeypatch.setattr(verifier.asyncio, "open_connection", connect)
    result = asyncio.run(verifier._handshake(ssl.create_default_context(), "public.example", False))
    assert result["passed"] is False
    assert result["result"] == "unverified_connection"
    assert "details" not in json.dumps(result)


def test_probe_covers_all_three_stores_and_12_bad_3_good_without_http(monkeypatch):
    contexts = {
        name: ssl.create_default_context()
        for name in ("system", "system_capath_only", "application")
    }
    monkeypatch.setattr(verifier, "_contexts", lambda: contexts)
    monkeypatch.setattr(
        verifier,
        "_store_metadata",
        lambda _: {
            "ca_count": 2,
            "incident_roots_absent": True,
            "verify_required": True,
            "check_hostname": True,
        },
    )

    async def handshake(context, host, accept):
        assert host in verifier.INCIDENT_HOSTS + verifier.NORMAL_HOSTS
        return {"host": host, "passed": True}

    monkeypatch.setattr(verifier, "_handshake", handshake)
    result = asyncio.run(verifier._probe())
    assert len(verifier.INCIDENT_HOSTS) == 12
    checks = result["checks"]
    assert isinstance(checks, list) and len(checks) == 45
    assert result["status"] == "passed"
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
