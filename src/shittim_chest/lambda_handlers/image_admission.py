"""Fail-closed ECS deployment lifecycle hook for production image admission."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

import boto3
from botocore.config import Config

LOGGER = logging.getLogger(__name__)

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REPOSITORY_URI = re.compile(
    r"([0-9]{12})\.dkr\.ecr\.ap-northeast-1\.amazonaws\.com/shittim-chest\Z"
)
_SIGNING_PROFILE_ARN = re.compile(
    r"arn:aws:signer:ap-northeast-1:([0-9]{12}):/signing-profiles/"
    r"shittim_chest_ecr\Z"
)
_NOTATION_SIGNATURE = "application/vnd.cncf.notary.signature"
_GITHUB_BUNDLE = "application/vnd.dev.sigstore.bundle.v0.3+json"
_PREDICATE_ANNOTATION = "dev.sigstore.bundle.predicateType"
_PROVENANCE_PREDICATE = "https://slsa.dev/provenance/v1"
_SPDX_PREDICATE = "https://spdx.dev/Document/v2.3"
_VULNERABILITY_PREDICATE = (
    "https://github.com/pitekusu/shittim-chest/attestations/vulnerability-assessment/v1"
)
_REQUIRED_PREDICATES = frozenset({_PROVENANCE_PREDICATE, _SPDX_PREDICATE, _VULNERABILITY_PREDICATE})
_SDK_CONFIG = Config(
    connect_timeout=3,
    read_timeout=5,
    retries={"mode": "standard", "total_max_attempts": 2},
    user_agent_extra="shittim-chest-image-admission/1",
)


class _EcsClient(Protocol):
    def describe_service_revisions(
        self, *, serviceRevisionArns: list[str]
    ) -> Mapping[str, object]: ...

    def describe_task_definition(self, *, taskDefinition: str) -> Mapping[str, object]: ...


class _EcrClient(Protocol):
    def describe_image_signing_status(
        self, *, repositoryName: str, imageId: Mapping[str, str]
    ) -> Mapping[str, object]: ...

    def list_image_referrers(
        self,
        *,
        repositoryName: str,
        subjectId: Mapping[str, str],
        maxResults: int,
        nextToken: str | None = None,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class ImageAdmissionSettings:
    """Immutable public deployment identity expected by the admission hook."""

    container_name: str
    repository_name: str
    repository_uri: str
    service_arn: str
    signing_profile_arn: str

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> ImageAdmissionSettings:
        values = {
            "container_name": environment.get("SHITTIM_EXPECTED_CONTAINER_NAME", ""),
            "repository_name": environment.get("SHITTIM_ECR_REPOSITORY_NAME", ""),
            "repository_uri": environment.get("SHITTIM_ECR_REPOSITORY_URI", ""),
            "service_arn": environment.get("SHITTIM_ECS_SERVICE_ARN", ""),
            "signing_profile_arn": environment.get("SHITTIM_SIGNING_PROFILE_ARN", ""),
        }
        if any(not value or value.strip() != value for value in values.values()):
            raise ValueError("image admission configuration is incomplete")
        repository = _REPOSITORY_URI.fullmatch(values["repository_uri"])
        if repository is None:
            raise ValueError("image admission repository URI is invalid")
        signing_profile = _SIGNING_PROFILE_ARN.fullmatch(values["signing_profile_arn"])
        if signing_profile is None:
            raise ValueError("image admission signing profile ARN is invalid")
        if repository.group(1) != signing_profile.group(1):
            raise ValueError("image admission AWS accounts do not match")
        return cls(**values)


class ImageAdmissionRejected(RuntimeError):
    """Stable, content-free rejection surfaced only as a bounded category."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True, slots=True)
class _Target:
    digest: str
    revision_arn: str


class ImageAdmissionLambda:
    """Validate one target service revision before ECS starts its tasks."""

    __slots__ = ("_ecr", "_ecs", "_settings")

    def __init__(
        self,
        *,
        ecr: _EcrClient,
        ecs: _EcsClient,
        settings: ImageAdmissionSettings,
    ) -> None:
        self._ecr = ecr
        self._ecs = ecs
        self._settings = settings

    def handle(self, event: object) -> dict[str, str]:
        """Return only an ECS hook status; all malformed or unavailable input fails closed."""

        try:
            target = self._target(event)
            self._verify_managed_signature(target.digest)
            self._verify_referrers(target.digest)
        except ImageAdmissionRejected as error:
            LOGGER.error(
                json.dumps(
                    {
                        "category": error.category,
                        "event": "image_admission_failed",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return {"hookStatus": "FAILED"}
        except Exception:
            LOGGER.error('{"category":"provider_unavailable","event":"image_admission_failed"}')
            return {"hookStatus": "FAILED"}
        LOGGER.info('{"event":"image_admission_succeeded"}')
        return {"hookStatus": "SUCCEEDED"}

    def _target(self, event: object) -> _Target:
        payload = _string_mapping(event, "event")
        if payload.get("lifecycleStage") != "PRE_SCALE_UP":
            raise ImageAdmissionRejected("unexpected_stage")
        deployment_prefix = self._service_deployment_prefix()
        resource_arn = payload.get("resourceArn")
        if (
            not isinstance(resource_arn, str)
            or not resource_arn.startswith(deployment_prefix)
            or not resource_arn.removeprefix(deployment_prefix)
            or "/" in resource_arn.removeprefix(deployment_prefix)
        ):
            raise ImageAdmissionRejected("unexpected_service")
        details = _string_mapping(payload.get("executionDetails"), "execution_details")
        if details.get("serviceArn") != self._settings.service_arn:
            raise ImageAdmissionRejected("unexpected_service")
        revision_arn = details.get("targetServiceRevisionArn")
        if not isinstance(revision_arn, str) or not revision_arn:
            raise ImageAdmissionRejected("missing_revision")

        response = self._ecs.describe_service_revisions(serviceRevisionArns=[revision_arn])
        if _sequence(response.get("failures")):
            raise ImageAdmissionRejected("revision_unavailable")
        revisions = _sequence(response.get("serviceRevisions"))
        if len(revisions) != 1:
            raise ImageAdmissionRejected("revision_unavailable")
        revision = _string_mapping(revisions[0], "service_revision")
        if (
            revision.get("serviceRevisionArn") != revision_arn
            or revision.get("serviceArn") != self._settings.service_arn
        ):
            raise ImageAdmissionRejected("revision_mismatch")
        task_definition_arn = revision.get("taskDefinition")
        if not isinstance(task_definition_arn, str) or not task_definition_arn:
            raise ImageAdmissionRejected("task_definition_unavailable")
        task_definition_response = self._ecs.describe_task_definition(
            taskDefinition=task_definition_arn
        )
        task_definition = _string_mapping(
            task_definition_response.get("taskDefinition"), "task_definition"
        )
        if task_definition.get("taskDefinitionArn") != task_definition_arn:
            raise ImageAdmissionRejected("task_definition_mismatch")
        images = _sequence(task_definition.get("containerDefinitions"))
        matching = [
            _string_mapping(image, "container_image")
            for image in images
            if isinstance(image, Mapping) and image.get("name") == self._settings.container_name
        ]
        if len(matching) != 1:
            raise ImageAdmissionRejected("container_mismatch")
        image = matching[0]
        uri = image.get("image")
        prefix = f"{self._settings.repository_uri}@"
        if not isinstance(uri, str) or not uri.startswith(prefix):
            raise ImageAdmissionRejected("image_uri_mismatch")
        digest = uri.removeprefix(prefix)
        if _DIGEST.fullmatch(digest) is None:
            raise ImageAdmissionRejected("digest_invalid")
        return _Target(digest=digest, revision_arn=revision_arn)

    def _service_deployment_prefix(self) -> str:
        arn_prefix, separator, service_path = self._settings.service_arn.partition(":service/")
        if not separator or not service_path or service_path.count("/") != 1:
            raise ImageAdmissionRejected("unexpected_service")
        return f"{arn_prefix}:service-deployment/{service_path}/"

    def _verify_managed_signature(self, digest: str) -> None:
        response = self._ecr.describe_image_signing_status(
            repositoryName=self._settings.repository_name,
            imageId={"imageDigest": digest},
        )
        statuses = _sequence(response.get("signingStatuses"))
        matching = [
            _string_mapping(status, "signing_status")
            for status in statuses
            if isinstance(status, Mapping)
            and status.get("signingProfileArn") == self._settings.signing_profile_arn
        ]
        if len(matching) != 1 or matching[0].get("status") != "COMPLETE":
            raise ImageAdmissionRejected("signature_incomplete")

    def _verify_referrers(self, digest: str) -> None:
        referrers: list[Mapping[str, object]] = []
        token: str | None = None
        seen_tokens: set[str] = set()
        # The current ECR service model has no paginator for ListImageReferrers.
        for _ in range(20):
            if token is None:
                response = self._ecr.list_image_referrers(
                    repositoryName=self._settings.repository_name,
                    subjectId={"imageDigest": digest},
                    maxResults=50,
                )
            else:
                response = self._ecr.list_image_referrers(
                    repositoryName=self._settings.repository_name,
                    subjectId={"imageDigest": digest},
                    maxResults=50,
                    nextToken=token,
                )
            referrers.extend(
                _string_mapping(item, "referrer") for item in _sequence(response.get("referrers"))
            )
            next_token = response.get("nextToken")
            if next_token is None:
                break
            if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
                raise ImageAdmissionRejected("referrer_pagination_invalid")
            seen_tokens.add(next_token)
            token = next_token
        else:
            raise ImageAdmissionRejected("referrer_limit_exceeded")

        active = [item for item in referrers if item.get("artifactStatus") == "ACTIVE"]
        has_signature = any(item.get("artifactType") == _NOTATION_SIGNATURE for item in active)
        predicates: set[str] = set()
        for item in active:
            if item.get("artifactType") != _GITHUB_BUNDLE:
                continue
            annotations = item.get("annotations")
            if isinstance(annotations, Mapping):
                predicate = annotations.get(_PREDICATE_ANNOTATION)
                if isinstance(predicate, str):
                    predicates.add(predicate)
        if not has_signature or not _REQUIRED_PREDICATES.issubset(predicates):
            raise ImageAdmissionRejected("referrers_incomplete")


_handler: ImageAdmissionLambda | None = None


def lambda_handler(event: object, context: object) -> dict[str, str]:
    """AWS entrypoint. The ECS hook contract is a status response, never an exception."""

    del context
    return _get_handler().handle(event)


def _get_handler() -> ImageAdmissionLambda:
    global _handler
    if _handler is None:
        region = os.environ.get("AWS_REGION", "")
        if not region:
            raise ValueError("AWS_REGION is required")
        _handler = ImageAdmissionLambda(
            ecr=cast(_EcrClient, boto3.client("ecr", region_name=region, config=_SDK_CONFIG)),
            ecs=cast(_EcsClient, boto3.client("ecs", region_name=region, config=_SDK_CONFIG)),
            settings=ImageAdmissionSettings.from_environment(os.environ),
        )
    return _handler


def _string_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ImageAdmissionRejected(f"{name}_invalid")
    return cast(Mapping[str, object], value)


def _sequence(value: object) -> Sequence[object]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise ImageAdmissionRejected("provider_response_invalid")
    return value
