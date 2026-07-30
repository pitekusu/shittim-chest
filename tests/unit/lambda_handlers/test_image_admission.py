from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import cast

import pytest

from shittim_chest.lambda_handlers.image_admission import (
    ImageAdmissionLambda,
    ImageAdmissionSettings,
)

DIGEST = "sha256:" + "a" * 64
SERVICE_ARN = (
    "arn:aws:ecs:ap-northeast-1:000000000000:service/"
    "shittim-chest-production/shittim-chest-production"
)
DEPLOYMENT_ARN = (
    "arn:aws:ecs:ap-northeast-1:000000000000:service-deployment/"
    "shittim-chest-production/shittim-chest-production/deployment-1"
)
REVISION_ARN = (
    "arn:aws:ecs:ap-northeast-1:000000000000:service-revision/"
    "shittim-chest-production/shittim-chest-production/1"
)
PROFILE_ARN = "arn:aws:signer:ap-northeast-1:000000000000:/signing-profiles/shittim_chest_ecr"
REPOSITORY_URI = "000000000000.dkr.ecr.ap-northeast-1.amazonaws.com/shittim-chest"
GITHUB_BUNDLE = "application/vnd.dev.sigstore.bundle.v0.3+json"
PREDICATE_KEY = "dev.sigstore.bundle.predicateType"


class FakeEcs:
    response: dict[str, object]

    def __init__(self) -> None:
        self.response = {
            "failures": [],
            "serviceRevisions": [
                {
                    "serviceArn": SERVICE_ARN,
                    "serviceRevisionArn": REVISION_ARN,
                    "containerImages": [
                        {
                            "containerName": "application",
                            "image": f"{REPOSITORY_URI}@{DIGEST}",
                            "imageDigest": DIGEST,
                        }
                    ],
                }
            ],
        }

    def describe_service_revisions(self, *, serviceRevisionArns: list[str]) -> Mapping[str, object]:
        assert serviceRevisionArns == [REVISION_ARN]
        return self.response


class FakeEcr:
    signature_status = "COMPLETE"
    referrers: list[Mapping[str, object]]

    def __init__(self) -> None:
        self.referrers = [
            {
                "artifactStatus": "ACTIVE",
                "artifactType": "application/vnd.cncf.notary.signature",
                "digest": "sha256:" + "1" * 64,
            },
            self.attestation("https://slsa.dev/provenance/v1", "2"),
            self.attestation("https://spdx.dev/Document/v2.3", "3"),
            self.attestation(
                "https://github.com/pitekusu/shittim-chest/attestations/"
                "vulnerability-assessment/v1",
                "4",
            ),
        ]

    @staticmethod
    def attestation(predicate: str, suffix: str) -> Mapping[str, object]:
        return {
            "annotations": {PREDICATE_KEY: predicate},
            "artifactStatus": "ACTIVE",
            "artifactType": GITHUB_BUNDLE,
            "digest": "sha256:" + suffix * 64,
        }

    def describe_image_signing_status(
        self, *, repositoryName: str, imageId: Mapping[str, str]
    ) -> Mapping[str, object]:
        assert repositoryName == "shittim-chest"
        assert imageId == {"imageDigest": DIGEST}
        return {
            "signingStatuses": [
                {
                    "signingProfileArn": PROFILE_ARN,
                    "status": self.signature_status,
                }
            ]
        }

    def list_image_referrers(
        self,
        *,
        repositoryName: str,
        subjectId: Mapping[str, str],
        maxResults: int,
        nextToken: str | None = None,
    ) -> Mapping[str, object]:
        assert repositoryName == "shittim-chest"
        assert subjectId == {"imageDigest": DIGEST}
        assert maxResults == 50
        assert nextToken is None
        return {"referrers": self.referrers}


def settings() -> ImageAdmissionSettings:
    return ImageAdmissionSettings(
        container_name="application",
        repository_name="shittim-chest",
        repository_uri=REPOSITORY_URI,
        service_arn=SERVICE_ARN,
        signing_profile_arn=PROFILE_ARN,
    )


def event() -> dict[str, object]:
    return {
        "executionId": "example",
        "executionDetails": {
            "serviceArn": SERVICE_ARN,
            "targetServiceRevisionArn": REVISION_ARN,
        },
        "lifecycleStage": "PRE_SCALE_UP",
        "resourceArn": DEPLOYMENT_ARN,
    }


def handler(*, ecr: FakeEcr | None = None, ecs: FakeEcs | None = None) -> ImageAdmissionLambda:
    return ImageAdmissionLambda(
        ecr=ecr or FakeEcr(),
        ecs=ecs or FakeEcs(),
        settings=settings(),
    )


def test_accepts_digest_with_complete_signature_and_all_active_referrers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        result = handler().handle(event())

    assert result == {"hookStatus": "SUCCEEDED"}
    assert caplog.messages == ['{"event":"image_admission_succeeded"}']


def test_paginates_all_referrers_without_sending_a_null_token() -> None:
    class PaginatedEcr(FakeEcr):
        calls = 0

        def list_image_referrers(
            self,
            *,
            repositoryName: str,
            subjectId: Mapping[str, str],
            maxResults: int,
            nextToken: str | None = None,
        ) -> Mapping[str, object]:
            assert repositoryName == "shittim-chest"
            assert subjectId == {"imageDigest": DIGEST}
            assert maxResults == 50
            self.calls += 1
            if self.calls == 1:
                assert nextToken is None
                return {"nextToken": "page-2", "referrers": self.referrers[:2]}
            assert nextToken == "page-2"
            return {"referrers": self.referrers[2:]}

    ecr = PaginatedEcr()
    assert handler(ecr=ecr).handle(event()) == {"hookStatus": "SUCCEEDED"}
    assert ecr.calls == 2


@pytest.mark.parametrize(
    ("mutate", "category"),
    [
        (lambda value: value.update(lifecycleStage="POST_SCALE_UP"), "unexpected_stage"),
        (lambda value: value.update(resourceArn="wrong"), "unexpected_service"),
        (
            lambda value: value.update(
                resourceArn=(
                    "arn:aws:ecs:ap-northeast-1:000000000000:service-deployment/"
                    "shittim-chest-production/other-service/deployment-1"
                )
            ),
            "unexpected_service",
        ),
        (
            lambda value: value["executionDetails"].update(  # type: ignore[union-attr]
                targetServiceRevisionArn=""
            ),
            "missing_revision",
        ),
    ],
)
def test_rejects_malformed_or_misdirected_hook_event(
    mutate: Callable[[dict[str, object]], None],
    category: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = event()
    mutate(payload)

    with caplog.at_level(logging.ERROR):
        result = handler().handle(payload)

    assert result == {"hookStatus": "FAILED"}
    assert category in caplog.text
    assert DIGEST not in caplog.text


def test_rejects_tag_uri_even_when_service_reports_a_digest() -> None:
    ecs = FakeEcs()
    revisions = cast(list[dict[str, object]], ecs.response["serviceRevisions"])
    containers = cast(list[dict[str, object]], revisions[0]["containerImages"])
    containers[0]["image"] = f"{REPOSITORY_URI}:latest"

    assert handler(ecs=ecs).handle(event()) == {"hookStatus": "FAILED"}


def test_rejects_incomplete_managed_signature() -> None:
    ecr = FakeEcr()
    ecr.signature_status = "IN_PROGRESS"

    assert handler(ecr=ecr).handle(event()) == {"hookStatus": "FAILED"}


@pytest.mark.parametrize("missing_index", range(4))
def test_rejects_each_missing_or_inactive_required_referrer(missing_index: int) -> None:
    ecr = FakeEcr()
    ecr.referrers[missing_index] = {
        **ecr.referrers[missing_index],
        "artifactStatus": "INACTIVE",
    }

    assert handler(ecr=ecr).handle(event()) == {"hookStatus": "FAILED"}


def test_rejects_provider_failure_without_leaking_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class UnavailableEcs(FakeEcs):
        def describe_service_revisions(
            self, *, serviceRevisionArns: list[str]
        ) -> Mapping[str, object]:
            del serviceRevisionArns
            raise RuntimeError("private-provider-detail")

    with caplog.at_level(logging.ERROR):
        result = handler(ecs=UnavailableEcs()).handle(event())

    assert result == {"hookStatus": "FAILED"}
    assert "provider_unavailable" in caplog.text
    assert "private-provider-detail" not in caplog.text


def test_settings_accept_the_bound_repository_and_unversioned_profile() -> None:
    assert (
        ImageAdmissionSettings.from_environment(
            {
                "SHITTIM_EXPECTED_CONTAINER_NAME": "application",
                "SHITTIM_ECR_REPOSITORY_NAME": "shittim-chest",
                "SHITTIM_ECR_REPOSITORY_URI": REPOSITORY_URI,
                "SHITTIM_ECS_SERVICE_ARN": SERVICE_ARN,
                "SHITTIM_SIGNING_PROFILE_ARN": PROFILE_ARN,
            }
        )
        == settings()
    )


def test_settings_fail_closed_on_missing_or_mismatched_identifiers() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        ImageAdmissionSettings.from_environment({})
    with pytest.raises(ValueError, match="repository URI"):
        ImageAdmissionSettings.from_environment(
            {
                "SHITTIM_EXPECTED_CONTAINER_NAME": "application",
                "SHITTIM_ECR_REPOSITORY_NAME": "shittim-chest",
                "SHITTIM_ECR_REPOSITORY_URI": f"{REPOSITORY_URI}@{DIGEST}",
                "SHITTIM_ECS_SERVICE_ARN": SERVICE_ARN,
                "SHITTIM_SIGNING_PROFILE_ARN": PROFILE_ARN,
            }
        )

    with pytest.raises(ValueError, match="signing profile ARN"):
        ImageAdmissionSettings.from_environment(
            {
                "SHITTIM_EXPECTED_CONTAINER_NAME": "application",
                "SHITTIM_ECR_REPOSITORY_NAME": "shittim-chest",
                "SHITTIM_ECR_REPOSITORY_URI": REPOSITORY_URI,
                "SHITTIM_ECS_SERVICE_ARN": SERVICE_ARN,
                "SHITTIM_SIGNING_PROFILE_ARN": f"{PROFILE_ARN}/ABCDEFGHIJ",
            }
        )

    with pytest.raises(ValueError, match="AWS accounts"):
        ImageAdmissionSettings.from_environment(
            {
                "SHITTIM_EXPECTED_CONTAINER_NAME": "application",
                "SHITTIM_ECR_REPOSITORY_NAME": "shittim-chest",
                "SHITTIM_ECR_REPOSITORY_URI": REPOSITORY_URI,
                "SHITTIM_ECS_SERVICE_ARN": SERVICE_ARN,
                "SHITTIM_SIGNING_PROFILE_ARN": PROFILE_ARN.replace("000000000000", "0" * 11 + "1"),
            }
        )
