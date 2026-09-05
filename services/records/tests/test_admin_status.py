"""Sanitized AWS status aggregation and cache tests."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

import shittim_records.admin_status_adapters as status_adapters
from shittim_records.admin_status import AdminStatusCollection, AdminStatusService
from shittim_records.admin_status_adapters import (
    ADMIN_STATUS_BUDGET_NAMES,
    ADMIN_STATUS_PARAMETER_NAMES,
    ADMIN_STATUS_STACK_NAMES,
    AwsAdminStatusConfiguration,
    AwsAdminStatusSource,
)
from shittim_records.contracts import (
    AdminEcrDetails,
    AdminEcsDetails,
    AdminInspectorDetails,
    AdminStatusOverall,
    AdminStatusSection,
)
from shittim_records.inspector_translations import (
    InspectorJapaneseSummary,
    InspectorTranslationUnavailable,
    inspector_description,
)

NOW = datetime(2026, 8, 24, 3, 0, tzinfo=UTC)
AWS_ACCOUNT_ID = "123456" + "789012"
RUNTIME_DIGEST = "sha256:" + "a" * 64
TASK_DEFINITION_ARN = (
    f"arn:aws:ecs:ap-northeast-1:{AWS_ACCOUNT_ID}:task-definition/private-runtime:42"
)
NEXT_TASK_IMAGE_TAGS = ("release-2026-08-24", "release-short")
CERTIFICATE_ARN = (
    f"arn:aws:acm:us-east-1:{AWS_ACCOUNT_ID}:certificate/12345678-1234-1234-1234-123456789abc"
)
INSPECTOR_DESCRIPTION = (
    "A boundary validation flaw can allow a remote attacker to submit malformed input and "
    "cause the affected process to read outside its intended memory region."
)
INSPECTOR_SUMMARY_JA = (
    "入力値の境界確認が不十分なため、遠隔の攻撃者が細工したデータを送ると、対象プロセスが本来の範囲外にある"
    "メモリを読み取る可能性があります。その結果、処理の異常終了や、プロセス内で扱われる情報の一部が意図せず"
    "露出するおそれがある脆弱性です。"
)
STATUS_COLLECTORS = (
    ("ecs", "_ecs_section"),
    ("ecr", "_ecr_section"),
    ("inspector", "_inspector_section"),
    ("s3", "_s3_section"),
    ("dynamodb", "_dynamodb_section"),
    ("lambda", "_lambda_section"),
    ("cloudfront", "_cloudfront_section"),
    ("sqs", "_sqs_section"),
    ("apigateway", "_apigateway_section"),
    ("eventbridge", "_eventbridge_section"),
    ("cloudformation", "_cloudformation_section"),
    ("sns", "_sns_section"),
    ("ssm", "_ssm_section"),
    ("cost_governance", "_cost_governance_section"),
    ("signer", "_signer_section"),
    ("external", "_external_section"),
)


def configuration() -> AwsAdminStatusConfiguration:
    return AwsAdminStatusConfiguration(
        aws_account_id=AWS_ACCOUNT_ID,
        cluster_name="private-cluster-name",
        service_name="private-service-name",
        container_name="application",
        ecr_repository_name="private-repository-name",
        runtime_stack_name="ShittimChest-Prod-Runtime",
        buckets={
            "web": "private-web-bucket",
            "media": "private-media-bucket",
            "release": "private-release-bucket",
            "memorial_upload": "private-memorial-upload-bucket",
        },
        tables={
            "debate": "private-debate-table",
            "archive": "private-archive-table",
            "statistics": "private-statistics-table",
            "session": "private-session-table",
        },
        functions={"auth": "auth-function"},
        records_public_hostname="records.example.com",
        projector_dlq_url=(
            f"https://sqs.ap-northeast-1.amazonaws.com/{AWS_ACCOUNT_ID}/projector-dlq"
        ),
        memorial_generation_queue_url=(
            f"https://sqs.ap-northeast-1.amazonaws.com/{AWS_ACCOUNT_ID}/memorial-generation"
        ),
        memorial_generation_dlq_url=(
            f"https://sqs.ap-northeast-1.amazonaws.com/{AWS_ACCOUNT_ID}/memorial-generation-dlq"
        ),
        stacks=ADMIN_STATUS_STACK_NAMES,
        static_parameters=ADMIN_STATUS_PARAMETER_NAMES,
        runtime_scheduler_name="shittim-chest-production-runtime-reconciler",
        sns_topic_arn=(
            f"arn:aws:sns:ap-northeast-1:{AWS_ACCOUNT_ID}:shittim-chest-production-operations"
        ),
        signing_profile_name="shittim_chest_ecr",
        budgets=ADMIN_STATUS_BUDGET_NAMES,
        anomaly_subscription_name="shittim-chest-production-cost-anomalies",
    )


class Empty:
    pass


class CloudWatch:
    def __init__(self, samples: dict[str, list[float]] | None = None) -> None:
        self.statistics_calls: list[dict[str, Any]] = []
        self.samples = samples

    def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
        self.statistics_calls.append(kwargs)
        return {"Datapoints": [{"Timestamp": NOW, "Maximum": 42.0}]}

    def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "MetricDataResults": [
                {
                    "Id": query["Id"],
                    "StatusCode": "Complete",
                    "Values": (
                        [1.0]
                        if self.samples is None
                        else self.samples.get(query["MetricStat"]["Metric"]["MetricName"], [])
                    ),
                }
                for query in kwargs["MetricDataQueries"]
            ]
        }


class Lambda:
    def get_function_configuration(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "State": "Active",
            "LastUpdateStatus": "Successful",
            "Runtime": "python3.14",
            "Architectures": ["arm64"],
        }

    def get_function_concurrency(self, **_kwargs: Any) -> dict[str, Any]:
        return {"ReservedConcurrentExecutions": 1}


class HealthyAffectionCheckpoints:
    def get_item(self, *, Key: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        key = status_adapters.unmarshal_item(Key)
        generated_at = NOW - timedelta(minutes=10)
        item = (
            {
                **key,
                "schema_version": 1,
                "record_type": "affection_ranking_pointer",
                "generation_id": "a" * 32,
                "generated_at": generated_at.isoformat(),
                "profile_count": 7,
                "page_count": 1,
                "checksum": "a" * 64,
            }
            if key["PK"] == "RANKING#AFFECTION"
            else {
                **key,
                "schema_version": 1,
                "record_type": "affection_seed_checkpoint",
                "generated_at": generated_at.isoformat(),
                "archive_count": 12,
                "profile_count": 7,
                "complete": True,
            }
        )
        return {"Item": status_adapters.marshal_item(item)}


class DynamoTables(HealthyAffectionCheckpoints):
    def __init__(self, *, stream_view: str = "NEW_IMAGE", session_ttl: str = "ENABLED") -> None:
        self.stream_view = stream_view
        self.session_ttl = session_ttl

    def describe_table(self, *, TableName: str) -> dict[str, Any]:
        table: dict[str, Any] = {
            "TableStatus": "ACTIVE",
            "DeletionProtectionEnabled": True,
            "ItemCount": 4,
        }
        if TableName == configuration().tables["debate"]:
            table["StreamSpecification"] = {
                "StreamEnabled": True,
                "StreamViewType": self.stream_view,
            }
        return {"Table": table}

    def describe_continuous_backups(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "ContinuousBackupsDescription": {
                "PointInTimeRecoveryDescription": {"PointInTimeRecoveryStatus": "ENABLED"}
            }
        }

    def describe_time_to_live(self, *, TableName: str) -> dict[str, Any]:
        status = self.session_ttl if TableName == configuration().tables["session"] else "ENABLED"
        return {"TimeToLiveDescription": {"TimeToLiveStatus": status}}


class Paginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        return self.pages


class EcrInventory:
    def __init__(self, image_details: list[dict[str, Any]]) -> None:
        self.paginator = Paginator([{"imageDetails": image_details}])

    def get_paginator(self, name: str) -> Paginator:
        assert name == "describe_images"
        return self.paginator


class EcsTaskDefinitionLookup:
    def __init__(self) -> None:
        self.task_definition_calls: list[str] = []

    def describe_task_definition(self, *, taskDefinition: str) -> dict[str, Any]:
        self.task_definition_calls.append(taskDefinition)
        return {
            "taskDefinition": {
                "taskDefinitionArn": TASK_DEFINITION_ARN,
                "containerDefinitions": [
                    {
                        "name": configuration().container_name,
                        "image": (
                            f"{AWS_ACCOUNT_ID}.dkr.ecr.ap-northeast-1.amazonaws.com/"
                            f"{configuration().ecr_repository_name}@{RUNTIME_DIGEST}"
                        ),
                    }
                ],
            }
        }


class EcrNextTaskImage:
    def __init__(self) -> None:
        self.describe_calls: list[dict[str, Any]] = []

    def describe_images(self, **kwargs: Any) -> dict[str, Any]:
        self.describe_calls.append(kwargs)
        return {
            "imageDetails": [
                {
                    "imageDigest": RUNTIME_DIGEST,
                    "imageTags": list(NEXT_TASK_IMAGE_TAGS),
                }
            ]
        }


def tagged_image_detail(
    *,
    digest: str = RUNTIME_DIGEST,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "imageDigest": digest,
        "imageTags": tags or ["release-2026-08-24"],
        "imagePushedAt": NOW,
        "lastRecordedPullTime": NOW,
        "imageSizeInBytes": 128,
        "imageManifestMediaType": "application/vnd.oci.image.manifest.v1+json",
    }


def inspector_finding(*, severity: str, digest: str = RUNTIME_DIGEST) -> dict[str, Any]:
    return {
        "description": INSPECTOR_DESCRIPTION,
        "severity": severity,
        "fixAvailable": "YES",
        "resources": [
            {
                "type": "AWS_ECR_CONTAINER_IMAGE",
                "details": {"awsEcrContainerImage": {"imageHash": digest}},
            }
        ],
        "packageVulnerabilityDetails": {
            "vulnerabilityId": "CVE-2026-12345",
            "vulnerablePackages": [
                {
                    "name": "example-package",
                    "version": "1.2.3",
                    "release": "4",
                    "epoch": 0,
                    "fixedInVersion": "1.2.4-1",
                    "packageManager": "OS",
                }
            ],
        },
    }


class Inspector:
    def __init__(
        self,
        *,
        counts: dict[str, int],
        findings: list[dict[str, Any]],
        aggregation_type: str | None = None,
    ) -> None:
        aggregation: dict[str, Any] = {
            "responses": [
                {
                    "awsEcrContainerAggregation": {
                        "imageSha": RUNTIME_DIGEST,
                        "severityCounts": counts,
                    }
                }
            ]
        }
        if aggregation_type is not None:
            aggregation["aggregationType"] = aggregation_type
        self.aggregations = Paginator([aggregation])
        self.findings = Paginator([{"findings": findings}])
        self.coverage = Paginator(
            [
                {
                    "coveredResources": [
                        {
                            "resourceId": RUNTIME_DIGEST,
                            "accountId": AWS_ACCOUNT_ID,
                            "scanStatus": {"statusCode": "ACTIVE"},
                            "lastScannedAt": NOW,
                        }
                    ]
                }
            ]
        )

    def get_paginator(self, name: str) -> Paginator:
        return {
            "list_finding_aggregations": self.aggregations,
            "list_findings": self.findings,
            "list_coverage": self.coverage,
        }[name]


def distribution_pages() -> Paginator:
    return Paginator(
        [
            {
                "DistributionList": {
                    "Items": [
                        {
                            "Id": "E123456789AB",
                            "Aliases": {"Items": [configuration().records_public_hostname]},
                        }
                    ]
                }
            }
        ]
    )


class CloudFront:
    requested_distribution: str | None = None

    def get_paginator(self, _name: str) -> Paginator:
        return distribution_pages()

    def get_distribution(self, *, Id: str) -> dict[str, Any]:
        self.requested_distribution = Id
        return {
            "Distribution": {
                "Status": "Deployed",
                "DistributionConfig": {
                    "Enabled": True,
                    "ViewerCertificate": {
                        "ACMCertificateArn": CERTIFICATE_ARN,
                        "MinimumProtocolVersion": "TLSv1.3_2025",
                    },
                },
            }
        }

    def list_invalidations(self, **_kwargs: Any) -> dict[str, Any]:
        return {"InvalidationList": {"Items": []}}


class Acm:
    def __init__(self, certificate: dict[str, Any] | None = None) -> None:
        self.requested_arn: str | None = None
        self.certificate = (
            certificate
            if certificate is not None
            else {
                "Status": "ISSUED",
                "KeyAlgorithm": "EC_prime256v1",
                "NotAfter": NOW + timedelta(days=30),
            }
        )

    def describe_certificate(self, *, CertificateArn: str) -> dict[str, Any]:
        self.requested_arn = CertificateArn
        return {"Certificate": self.certificate}


def source(**clients: Any) -> AwsAdminStatusSource:
    empty = Empty()
    cloudwatch = clients.get("cloudwatch", CloudWatch())
    return AwsAdminStatusSource(
        configuration=configuration(),
        ecs=clients.get("ecs", empty),
        cloudformation=clients.get("cloudformation", empty),
        ecr=clients.get("ecr", empty),
        inspector=clients.get("inspector", empty),
        s3=clients.get("s3", empty),
        dynamodb=clients.get("dynamodb", empty),
        lambda_client=clients.get("lambda_client", empty),
        cloudfront=clients.get("cloudfront", empty),
        acm=clients.get("acm", empty),
        sqs=clients.get("sqs", empty),
        apigateway=clients.get("apigateway", empty),
        events=clients.get("events", empty),
        scheduler=clients.get("scheduler", empty),
        sns=clients.get("sns", empty),
        ssm=clients.get("ssm", empty),
        budgets=clients.get("budgets", empty),
        cost_explorer=clients.get("cost_explorer", empty),
        signer=clients.get("signer", empty),
        cloudwatch=cloudwatch,
        cloudwatch_global=clients.get("cloudwatch_global", cloudwatch),
        cloudformation_global=clients.get("cloudformation_global", empty),
        translations=clients.get("translations"),
    )


def metrics(section: AdminStatusSection) -> dict[str, str | int | bool | None]:
    return {metric.name: metric.value for metric in section.metrics}


def test_ecs_includes_runtime_heartbeat_without_exposing_resource_names() -> None:
    class Ecs(EcsTaskDefinitionLookup):
        def describe_services(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "services": [
                    {
                        "desiredCount": 0,
                        "runningCount": 0,
                        "pendingCount": 0,
                        "status": "ACTIVE",
                        "schedulingStrategy": "REPLICA",
                        "launchType": "FARGATE",
                        "platformVersion": "1.4.0",
                        "taskDefinition": TASK_DEFINITION_ARN,
                        "deploymentController": {"type": "ECS"},
                        "deploymentConfiguration": {
                            "minimumHealthyPercent": 0,
                            "maximumPercent": 100,
                            "deploymentCircuitBreaker": {"enable": True, "rollback": True},
                        },
                        "enableExecuteCommand": False,
                        "deployments": [
                            {
                                "status": "PRIMARY",
                                "rolloutState": "COMPLETED",
                                "failedTasks": 0,
                                "updatedAt": NOW,
                            }
                        ],
                    }
                ]
            }

    class Dynamo:
        def get_item(self, **_kwargs: Any) -> dict[str, Any]:
            return {}

    cloudwatch = CloudWatch()
    ecs = Ecs()
    ecr = EcrNextTaskImage()
    section = source(
        ecs=ecs,
        ecr=ecr,
        dynamodb=Dynamo(),
        cloudwatch=cloudwatch,
    )._ecs_section(NOW)

    assert section.state == "healthy"
    assert metrics(section)["heartbeat_age_seconds"] == "42.000"
    values = metrics(section)
    assert values["service_status"] == "ACTIVE"
    assert values["launch_mode"] == "FARGATE"
    assert values["platform_version"] == "1.4.0"
    assert values["task_definition_revision"] == 42
    assert values["rollout_state"] == "COMPLETED"
    assert values["circuit_breaker_enabled"] is True
    assert values["circuit_breaker_rollback"] is True
    assert values["execute_command_enabled"] is False
    assert isinstance(section.details, AdminEcsDetails)
    assert section.details.next_task_image_tags == NEXT_TASK_IMAGE_TAGS
    assert ecs.task_definition_calls == [TASK_DEFINITION_ARN]
    assert ecr.describe_calls == [
        {
            "repositoryName": configuration().ecr_repository_name,
            "imageIds": [{"imageDigest": RUNTIME_DIGEST}],
        }
    ]
    assert cloudwatch.statistics_calls[0]["Namespace"] == "ShittimChest/Prod"
    assert cloudwatch.statistics_calls[0]["MetricName"] == "HeartbeatAgeSeconds"
    assert configuration().cluster_name not in section.model_dump_json()
    assert configuration().service_name not in section.model_dump_json()


@pytest.mark.parametrize(
    "image",
    [
        (
            f"{AWS_ACCOUNT_ID}.dkr.ecr.ap-northeast-1.amazonaws.com/"
            f"{configuration().ecr_repository_name}:mutable-tag"
        ),
        (
            f"{AWS_ACCOUNT_ID}.dkr.ecr.ap-northeast-1.amazonaws.com/"
            f"other-repository@{RUNTIME_DIGEST}"
        ),
    ],
)
def test_ecs_rejects_unpinned_or_out_of_repository_next_task_images(image: str) -> None:
    class Ecs:
        def describe_task_definition(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "taskDefinition": {
                    "taskDefinitionArn": TASK_DEFINITION_ARN,
                    "containerDefinitions": [
                        {"name": configuration().container_name, "image": image}
                    ],
                }
            }

    class Ecr:
        def describe_images(self, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("ECR must not be queried for an untrusted task image")

    with pytest.raises(ValueError, match="ECS task image"):
        source(ecs=Ecs(), ecr=Ecr())._next_task_image_tags(TASK_DEFINITION_ARN)


@pytest.mark.parametrize(
    ("include_controls", "include_heartbeat", "expected_state"),
    [
        (True, True, "healthy"),
        (False, True, "unknown"),
        (True, False, "unknown"),
    ],
)
def test_ecs_requires_runtime_telemetry_while_tasks_are_active(
    *,
    include_controls: bool,
    include_heartbeat: bool,
    expected_state: str,
) -> None:
    class Ecs(EcsTaskDefinitionLookup):
        def describe_services(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "services": [
                    {
                        "desiredCount": 1,
                        "runningCount": 1,
                        "pendingCount": 0,
                        "taskDefinition": TASK_DEFINITION_ARN,
                        "deployments": [{}],
                    }
                ]
            }

    class Dynamo:
        def get_item(self, *, Key: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            if not include_controls:
                return {}
            key = status_adapters.unmarshal_item(Key)
            if key["SK"] == "ACTIVE_ATTEMPT_COUNT":
                item = {**key, "active_attempt_count": 1}
            elif key["SK"] == "ACTIVITY":
                item = {**key, "pending_count": 0, "claimed_count": 0}
            else:
                return {}
            return {"Item": status_adapters.marshal_item(item)}

    class RuntimeCloudWatch(CloudWatch):
        def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
            self.statistics_calls.append(kwargs)
            return (
                {"Datapoints": [{"Timestamp": NOW, "Maximum": 42.0}]}
                if include_heartbeat
                else {"Datapoints": []}
            )

    section = source(
        ecs=Ecs(),
        ecr=EcrNextTaskImage(),
        dynamodb=Dynamo(),
        cloudwatch=RuntimeCloudWatch(),
    )._ecs_section(NOW)

    assert section.state == expected_state
    assert section.summary == (
        "稼働中" if expected_state == "healthy" else "状態を取得できません。"
    )


def test_ecs_marks_failed_rollout_without_exposing_provider_reason() -> None:
    private_reason = "deployment failed for private-service-name"

    class Ecs(EcsTaskDefinitionLookup):
        def describe_services(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "services": [
                    {
                        "desiredCount": 0,
                        "runningCount": 0,
                        "pendingCount": 0,
                        "status": "ACTIVE",
                        "taskDefinition": TASK_DEFINITION_ARN,
                        "deployments": [
                            {
                                "status": "PRIMARY",
                                "rolloutState": "FAILED",
                                "rolloutStateReason": private_reason,
                                "failedTasks": 1,
                            }
                        ],
                    }
                ]
            }

    class Dynamo:
        def get_item(self, **_kwargs: Any) -> dict[str, Any]:
            return {}

    section = source(
        ecs=Ecs(),
        ecr=EcrNextTaskImage(),
        dynamodb=Dynamo(),
    )._ecs_section(NOW)

    assert section.state == "warning"
    assert section.summary == "デプロイ状態の確認が必要です。"
    assert metrics(section)["rollout_state"] == "FAILED"
    assert metrics(section)["failed_task_count"] == 1
    assert private_reason not in section.model_dump_json()


def test_dynamodb_includes_stream_and_one_hour_throttles() -> None:
    section = source(dynamodb=DynamoTables())._dynamodb_section(NOW)
    values = metrics(section)

    assert section.state == "warning"
    assert values["debate_stream_enabled"] is True
    assert values["debate_stream_view_type"] == "NEW_IMAGE"
    assert values["affection_ranking_ready"] is True
    assert values["affection_ranking_fresh"] is True
    assert values["affection_profile_count"] == 7
    assert values["affection_seed_complete"] is True
    for label in ("debate", "archive", "statistics", "session"):
        assert values[f"{label}_read_throttles"] == 1
        assert values[f"{label}_write_throttles"] == 1
        assert configuration().tables[label] not in section.model_dump_json()


def test_dynamodb_preserves_unknown_throttles_for_incomplete_metric_data() -> None:
    class CloudWatchWithIncompleteData(CloudWatch):
        def get_metric_data(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "MetricDataResults": [
                    {"Id": "d0", "StatusCode": "Complete", "Values": [2.0]},
                    {"Id": "d1", "StatusCode": "PartialData", "Values": [3.0]},
                    {"Id": "d2", "StatusCode": "Complete", "Values": []},
                    {"Id": "d3", "StatusCode": "InternalError", "Values": [4.0]},
                ]
            }

    throttles = source(cloudwatch=CloudWatchWithIncompleteData())._dynamodb_throttles(NOW)

    assert throttles[("debate", "read")] == 2
    assert throttles[("debate", "write")] is None
    assert throttles[("archive", "read")] == 0
    assert throttles[("archive", "write")] is None
    assert throttles[("statistics", "read")] is None
    assert throttles[("session", "write")] is None


@pytest.mark.parametrize(
    ("stream_view_type", "expected_state"),
    [
        ("NEW_IMAGE", "healthy"),
        ("KEYS_ONLY", "warning"),
        ("OLD_IMAGE", "warning"),
        ("NEW_AND_OLD_IMAGES", "warning"),
    ],
)
def test_dynamodb_requires_new_image_stream_view(
    stream_view_type: str,
    expected_state: str,
) -> None:
    section = source(
        dynamodb=DynamoTables(stream_view=stream_view_type), cloudwatch=CloudWatch({})
    )._dynamodb_section(NOW)

    assert section.state == expected_state
    assert metrics(section)["debate_stream_view_type"] == stream_view_type


def test_dynamodb_requires_session_ttl_to_be_enabled() -> None:
    section = source(
        dynamodb=DynamoTables(session_ttl="DISABLED"), cloudwatch=CloudWatch({})
    )._dynamodb_section(NOW)

    assert section.state == "warning"
    assert metrics(section)["session_ttl"] == "DISABLED"


def test_ecr_resolves_release_approved_digests_instead_of_tags() -> None:
    class CloudFormation:
        def __init__(self) -> None:
            self.stack_names: list[str] = []

        def describe_stacks(self, *, StackName: str) -> dict[str, Any]:
            self.stack_names.append(StackName)
            return {
                "Stacks": [
                    {
                        "Parameters": [
                            {
                                "ParameterKey": "RuntimeImageDigest",
                                "ParameterValue": RUNTIME_DIGEST,
                            },
                        ]
                    }
                ]
            }

    class Ecr:
        def __init__(self) -> None:
            self.paginator = Paginator(
                [
                    {
                        "imageDetails": [
                            {
                                "imageDigest": RUNTIME_DIGEST,
                                "imageTags": ["approved"],
                                "imagePushedAt": NOW,
                                "lastRecordedPullTime": NOW,
                                "imageSizeInBytes": 128,
                                "artifactMediaType": "application/vnd.oci.image.config.v1+json",
                                "imageManifestMediaType": (
                                    "application/vnd.oci.image.manifest.v1+json"
                                ),
                            },
                            {
                                "imageDigest": "sha256:" + "c" * 64,
                                "imagePushedAt": NOW - timedelta(minutes=1),
                                "imageSizeInBytes": 64,
                                "artifactMediaType": "application/vnd.cncf.notary.signature",
                            },
                        ]
                    }
                ]
            )

        def describe_repositories(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "repositories": [
                    {
                        "imageTagMutability": "IMMUTABLE",
                        "encryptionConfiguration": {"encryptionType": "KMS"},
                        "imageScanningConfiguration": {"scanOnPush": False},
                        "createdAt": NOW - timedelta(days=30),
                    }
                ]
            }

        def get_paginator(self, name: str) -> Paginator:
            assert name == "describe_images"
            return self.paginator

    ecr = Ecr()
    cloudformation = CloudFormation()
    section = source(ecr=ecr, cloudformation=cloudformation)._ecr_section()

    assert section.state == "healthy"
    assert ecr.paginator.calls == [{"repositoryName": configuration().ecr_repository_name}]
    assert cloudformation.stack_names == [configuration().runtime_stack_name]
    values = metrics(section)
    assert values["repository_image_count"] == 2
    assert values["repository_tagged_image_count"] == 1
    assert values["repository_untagged_image_count"] == 1
    assert values["repository_total_size_bytes"] == 192
    assert isinstance(section.details, AdminEcrDetails)
    assert len(section.details.images) == 1
    assert section.details.images[0].tags == ("approved",)
    assert section.details.images[0].media_type == "OCI_IMAGE"
    assert section.details.images[0].last_pulled_at == NOW
    assert "break_glass" not in values
    assert "sha256:" not in section.model_dump_json()
    assert RUNTIME_DIGEST[:19] not in section.model_dump_json()


def test_ecr_rejects_missing_runtime_stack_digest_without_using_stale_configuration() -> None:
    class CloudFormation:
        def describe_stacks(self, **_kwargs: Any) -> dict[str, Any]:
            return {"Stacks": [{"Parameters": []}]}

    with pytest.raises(ValueError, match="Runtime image digest is invalid"):
        source(cloudformation=CloudFormation())._runtime_image_digest()


def test_ecr_state_includes_tag_immutability() -> None:
    class CloudFormation:
        def describe_stacks(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "Stacks": [
                    {
                        "Parameters": [
                            {
                                "ParameterKey": "RuntimeImageDigest",
                                "ParameterValue": RUNTIME_DIGEST,
                            },
                        ]
                    }
                ]
            }

    class Ecr:
        def __init__(self) -> None:
            self.paginator = Paginator(
                [
                    {
                        "imageDetails": [
                            {
                                "imageDigest": RUNTIME_DIGEST,
                                "imagePushedAt": NOW,
                                "imageSizeInBytes": 128,
                            }
                        ]
                    }
                ]
            )

        def describe_repositories(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "repositories": [
                    {
                        "imageTagMutability": "MUTABLE",
                        "encryptionConfiguration": {"encryptionType": "AES256"},
                    }
                ]
            }

        def get_paginator(self, name: str) -> Paginator:
            assert name == "describe_images"
            return self.paginator

    section = source(ecr=Ecr(), cloudformation=CloudFormation())._ecr_section()

    assert section.state == "warning"
    assert metrics(section)["tag_mutability"] == "MUTABLE"


def test_alarm_query_uses_the_exact_deployed_production_prefix() -> None:
    alarms = Paginator([{"MetricAlarms": [], "CompositeAlarms": []}])

    class Alarms:
        def get_paginator(self, _name: str) -> Paginator:
            return alarms

    result = source(cloudwatch=Alarms())._alarm_counts()

    assert result == (0, 0, (), False)
    assert alarms.calls == [
        {
            "AlarmNamePrefix": "shittim-chest-production-",
            "AlarmTypes": ["CompositeAlarm", "MetricAlarm"],
            "StateValue": "ALARM",
        }
    ]


def test_alarm_counts_only_severity_bearing_composites() -> None:
    alarms = Paginator(
        [
            {
                "MetricAlarms": [
                    {"AlarmName": "shittim-chest-production-runtime-active"},
                    {"AlarmName": "shittim-chest-production-heartbeat-stale"},
                    {"AlarmName": "shittim-chest-production-status-publish-failure"},
                ],
                "CompositeAlarms": [
                    {"AlarmName": "shittim-chest-production-critical"},
                    {"AlarmName": "shittim-chest-production-warning"},
                ],
            }
        ]
    )

    class Alarms:
        def get_paginator(self, _name: str) -> Paginator:
            return alarms

    critical, warning, active, unknown = source(cloudwatch=Alarms())._alarm_counts()

    assert (critical, warning, unknown) == (1, 1, False)
    assert [alarm.model_dump(mode="json") for alarm in active] == [
        {"code": "heartbeat-stale", "severity": "critical", "service": "ecs"},
        {
            "code": "status-publish-failure",
            "severity": "warning",
            "service": "lambda",
        },
    ]


def test_alarm_details_require_their_composite_alarm_to_be_active() -> None:
    alarms = Paginator(
        [
            {
                "MetricAlarms": [
                    {"AlarmName": "shittim-chest-production-heartbeat-stale"},
                    {"AlarmName": "shittim-chest-production-status-publish-failure"},
                ],
                "CompositeAlarms": [],
            }
        ]
    )

    class Alarms:
        def get_paginator(self, _name: str) -> Paginator:
            return alarms

    assert source(cloudwatch=Alarms())._alarm_counts() == (0, 0, (), False)


def test_runtime_health_alarm_details_require_the_runtime_active_gate() -> None:
    alarms = Paginator(
        [
            {
                "MetricAlarms": [
                    {"AlarmName": "shittim-chest-production-bot-not-ready"},
                    {"AlarmName": "shittim-chest-production-heartbeat-stale"},
                    {"AlarmName": "shittim-chest-production-ingress-runtime-mismatch"},
                ],
                "CompositeAlarms": [{"AlarmName": "shittim-chest-production-critical"}],
            }
        ]
    )

    class Alarms:
        def get_paginator(self, _name: str) -> Paginator:
            return alarms

    critical, warning, active, unknown = source(cloudwatch=Alarms())._alarm_counts()

    assert (critical, warning, unknown) == (1, 0, False)
    assert [alarm.model_dump(mode="json") for alarm in active] == [
        {
            "code": "ingress-runtime-mismatch",
            "severity": "critical",
            "service": "ecs",
        }
    ]


def test_unknown_alarm_names_are_not_exposed() -> None:
    alarms = Paginator(
        [
            {
                "MetricAlarms": [{"AlarmName": "shittim-chest-production-private-resource-name"}],
                "CompositeAlarms": [{"AlarmName": "shittim-chest-production-warning"}],
            }
        ]
    )

    class Alarms:
        def get_paginator(self, _name: str) -> Paginator:
            return alarms

    assert source(cloudwatch=Alarms())._alarm_counts() == (0, 1, (), True)


def test_cloudfront_derives_the_current_certificate_from_the_exact_distribution() -> None:
    acm = Acm()
    cloudfront = CloudFront()
    section = source(cloudfront=cloudfront, acm=acm)._cloudfront_section(NOW)

    assert section.state == "healthy"
    assert cloudfront.requested_distribution == "E123456789AB"
    assert acm.requested_arn == CERTIFICATE_ARN
    assert CERTIFICATE_ARN not in section.model_dump_json()


@pytest.mark.parametrize(
    ("certificate", "expected_state"),
    (
        ({"Status": "PENDING_VALIDATION", "NotAfter": NOW + timedelta(days=30)}, "warning"),
        ({"Status": "ISSUED", "NotAfter": NOW - timedelta(seconds=1)}, "warning"),
        ({"Status": "ISSUED"}, "unknown"),
    ),
)
def test_cloudfront_state_includes_certificate_health(
    certificate: dict[str, Any], expected_state: str
) -> None:
    section = source(cloudfront=CloudFront(), acm=Acm(certificate))._cloudfront_section(NOW)

    assert section.state == expected_state


@pytest.mark.parametrize("aggregation_type", ("AWS_ECR_CONTAINER", "AWS_CONTAINER"))
def test_inspector_includes_repository_coverage_and_last_scan(aggregation_type: str) -> None:
    inspector = Inspector(
        counts={"all": 1, "critical": 0, "high": 1, "medium": 0},
        findings=[inspector_finding(severity="HIGH")],
        aggregation_type=aggregation_type,
    )

    translation_source = inspector_description(
        vulnerability_id="CVE-2026-12345",
        description=INSPECTOR_DESCRIPTION,
    )

    class Translations:
        def load(self, keys: tuple[str, ...]) -> dict[str, InspectorJapaneseSummary]:
            assert keys == (translation_source.key,)
            return {
                translation_source.key: InspectorJapaneseSummary(
                    key=translation_source.key,
                    vulnerability_id=translation_source.vulnerability_id,
                    source_sha256=translation_source.source_sha256,
                    summary_ja=INSPECTOR_SUMMARY_JA,
                    translated_at=NOW,
                )
            }

    section = source(
        ecr=EcrInventory([tagged_image_detail(tags=["release-2026-08-24", "stable"])]),
        inspector=inspector,
        translations=Translations(),
    )._inspector_section()
    values = metrics(section)

    assert section.state == "warning"
    assert values["coverage_count"] == 1
    assert values["coverage_active"] == 1
    assert values["last_scanned_at"] == NOW.isoformat()
    assert values["active_high"] == 1
    assert values["translation_cache_count"] == 1
    assert values["translation_missing_count"] == 0
    assert values["translation_last_translated_at"] == NOW.isoformat()
    assert isinstance(section.details, AdminInspectorDetails)
    assert len(section.details.images) == 1
    image = section.details.images[0]
    assert image.tags == ("release-2026-08-24", "stable")
    assert image.counts.high == 1
    assert image.findings[0].vulnerability_id == "CVE-2026-12345"
    assert image.findings[0].summary_ja == INSPECTOR_SUMMARY_JA
    assert image.findings[0].affected_packages[0].installed_version == "1.2.3-4"
    assert RUNTIME_DIGEST not in section.model_dump_json()
    assert AWS_ACCOUNT_ID not in section.model_dump_json()
    assert INSPECTOR_DESCRIPTION not in section.model_dump_json()
    assert inspector.aggregations.calls == [
        {
            "aggregationType": "AWS_ECR_CONTAINER",
            "aggregationRequest": {
                "awsEcrContainerAggregation": {
                    "repositories": [
                        {
                            "comparison": "EQUALS",
                            "value": configuration().ecr_repository_name,
                        }
                    ]
                }
            },
        }
    ]

    class UnavailableTranslations:
        def load(self, _keys: tuple[str, ...]) -> dict[str, InspectorJapaneseSummary]:
            raise InspectorTranslationUnavailable("cache_unavailable")

    pending = source(
        ecr=EcrInventory([tagged_image_detail(tags=["release-2026-08-24", "stable"])]),
        inspector=inspector,
        translations=UnavailableTranslations(),
    )._inspector_section()

    assert isinstance(pending.details, AdminInspectorDetails)
    assert pending.details.images[0].findings[0].summary_ja is None
    pending_values = metrics(pending)
    assert pending_values["translation_cache_count"] is None
    assert pending_values["translation_missing_count"] is None
    assert pending_values["translation_last_translated_at"] is None

    uncached = source(
        ecr=EcrInventory([tagged_image_detail(tags=["release-2026-08-24", "stable"])]),
        inspector=inspector,
    )._inspector_section()
    uncached_values = metrics(uncached)
    assert uncached_values["translation_cache_count"] == 0
    assert uncached_values["translation_missing_count"] == 1
    assert uncached_values["translation_last_translated_at"] is None


def test_inspector_treats_untriaged_findings_as_warning() -> None:
    section = source(
        ecr=EcrInventory([tagged_image_detail()]),
        inspector=Inspector(
            counts={"all": 1},
            findings=[inspector_finding(severity="UNTRIAGED")],
        ),
    )._inspector_section()

    assert section.state == "warning"
    assert metrics(section)["active_untriaged"] == 1


@pytest.mark.parametrize("severity", ("CRITICAL", "HIGH"))
def test_inspector_rejects_details_that_exceed_severity_aggregates(severity: str) -> None:
    class Translations:
        def load(self, _keys: tuple[str, ...]) -> dict[str, InspectorJapaneseSummary]:
            raise AssertionError("inconsistent findings must fail before cache access")

    with pytest.raises(ValueError, match="details exceed severity aggregates"):
        source(
            ecr=EcrInventory([tagged_image_detail()]),
            inspector=Inspector(
                counts={"all": 1, "critical": 0, "high": 0, "medium": 1},
                findings=[inspector_finding(severity=severity)],
            ),
            translations=Translations(),
        )._inspector_section()


def test_s3_accepts_non_versioned_one_day_memorial_upload_lifecycle() -> None:
    class S3:
        def __init__(self, *, expiration_days: int = 1) -> None:
            self.expiration_days = expiration_days

        def get_bucket_versioning(self, *, Bucket: str) -> dict[str, Any]:
            return {} if Bucket == "private-memorial-upload-bucket" else {"Status": "Enabled"}

        def get_bucket_encryption(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "ServerSideEncryptionConfiguration": {
                    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
                }
            }

        def get_public_access_block(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                }
            }

        def get_bucket_lifecycle_configuration(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "Rules": [
                    {
                        "Status": "Enabled",
                        "Expiration": {"Days": self.expiration_days},
                        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                    }
                ]
            }

    healthy = source(s3=S3())._s3_section()
    warning = source(s3=S3(expiration_days=2))._s3_section()

    assert healthy.state == "healthy"
    assert metrics(healthy)["memorial_upload_versioning"] == "Disabled"
    assert metrics(healthy)["memorial_upload_expiration_days"] == 1
    assert metrics(healthy)["memorial_upload_abort_days"] == 1
    assert warning.state == "warning"


@pytest.mark.parametrize("failure", ("partial", "missing"))
def test_lambda_section_is_unknown_for_incomplete_provider_metrics(failure: str) -> None:
    class IncompleteCloudWatch(CloudWatch):
        def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
            results = super().get_metric_data(**kwargs)["MetricDataResults"]
            if failure == "partial":
                results[1] = {**results[1], "StatusCode": "PartialData"}
            else:
                results.pop()
            return {"MetricDataResults": results}

    section = source(lambda_client=Lambda(), cloudwatch=IncompleteCloudWatch())._lambda_section(NOW)

    assert section.state == "unknown"
    values = metrics(section)
    assert any(
        values[f"auth_hour_{metric}"] is None
        for metric in ("invocations", "errors", "throttles", "duration")
    )


def test_lambda_section_treats_complete_empty_idle_metrics_as_zero() -> None:
    section = source(lambda_client=Lambda(), cloudwatch=CloudWatch({}))._lambda_section(NOW)
    values = metrics(section)

    assert section.state == "healthy"
    assert values["auth_hour_invocations"] == 0
    assert values["auth_hour_errors"] == 0
    assert values["auth_hour_throttles"] == 0
    assert values["auth_hour_duration"] is None


def test_lambda_section_requires_duration_when_invocations_exist() -> None:
    section = source(
        lambda_client=Lambda(),
        cloudwatch=CloudWatch({"Invocations": [1.0], "Errors": [1.0], "Throttles": [1.0]}),
    )._lambda_section(NOW)

    assert section.state == "unknown"
    assert metrics(section)["auth_hour_duration"] is None


@pytest.mark.parametrize(
    ("sampled_metric", "sample_value", "expected_state", "expected_value"),
    (
        ("Duration", 12.5, "unknown", "12.500"),
        ("Errors", 1.0, "unknown", 1),
        ("Throttles", 1.0, "warning", 1),
    ),
)
def test_lambda_section_validates_idle_metric_relationships(
    sampled_metric: str,
    sample_value: float,
    expected_state: str,
    expected_value: str | int,
) -> None:
    section = source(
        lambda_client=Lambda(),
        cloudwatch=CloudWatch({sampled_metric: [sample_value]}),
    )._lambda_section(NOW)

    assert section.state == expected_state
    values = metrics(section)
    assert values["auth_hour_invocations"] == 0
    assert values[f"auth_hour_{sampled_metric.casefold()}"] == expected_value


@pytest.mark.parametrize(
    ("failing_metric", "expected_state"),
    [
        (None, "healthy"),
        ("Errors", "warning"),
        ("Throttles", "warning"),
    ],
)
def test_lambda_section_warns_for_errors_and_throttles(
    failing_metric: str | None,
    expected_state: str,
) -> None:
    samples = {"Invocations": [1.0], "Errors": [0.0], "Throttles": [0.0], "Duration": [12.5]}
    if failing_metric:
        samples[failing_metric] = [1.0]
    section = source(
        lambda_client=Lambda(),
        cloudwatch=CloudWatch(samples),
    )._lambda_section(NOW)

    assert section.state == expected_state


def test_lambda_section_propagates_reserved_concurrency_provider_failure() -> None:
    class UnavailableConcurrency(Lambda):
        def get_function_concurrency(self, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("provider failure")

    with pytest.raises(RuntimeError, match="provider failure"):
        source(lambda_client=UnavailableConcurrency())._lambda_section(NOW)


def test_cloudfront_section_is_unknown_for_incomplete_provider_metrics() -> None:
    class IncompleteCloudWatch(CloudWatch):
        def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
            results = super().get_metric_data(**kwargs)["MetricDataResults"]
            results[1] = {**results[1], "StatusCode": "PartialData"}
            return {"MetricDataResults": results}

    section = source(
        cloudfront=CloudFront(),
        acm=Acm(),
        cloudwatch_global=IncompleteCloudWatch(),
    )._cloudfront_section(NOW)

    assert section.state == "unknown"
    assert metrics(section)["hour_4xx_rate"] is None


def test_cloudfront_section_treats_complete_empty_quiet_hour_as_zero() -> None:
    class QuietCloudWatch(CloudWatch):
        def __init__(self) -> None:
            super().__init__()
            self.metric_names: list[str] = []

        def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
            self.metric_names = [
                query["MetricStat"]["Metric"]["MetricName"] for query in kwargs["MetricDataQueries"]
            ]
            return {
                "MetricDataResults": [
                    {"Id": query["Id"], "StatusCode": "Complete", "Values": []}
                    for query in kwargs["MetricDataQueries"]
                ]
            }

    cloudwatch = QuietCloudWatch()
    section = source(
        cloudfront=CloudFront(),
        acm=Acm(),
        cloudwatch_global=cloudwatch,
    )._cloudfront_section(NOW)
    values = metrics(section)

    assert section.state == "healthy"
    assert cloudwatch.metric_names == ["Requests", "4xxErrorRate", "5xxErrorRate"]
    assert values["hour_requests"] == 0
    assert values["hour_4xx_rate"] == "0.000"
    assert values["hour_5xx_rate"] == "0.000"
    assert values["hour_cache_hit_rate"] == "DISABLED"


def test_cloudfront_metrics_require_error_rates_when_requests_exist() -> None:
    provider_metrics, provider_complete = source(
        cloudwatch_global=CloudWatch({"Requests": [5.0]})
    )._cloudfront_metrics(NOW, distribution_id="E123456789AB")
    values = {metric.name: metric.value for metric in provider_metrics}

    assert provider_complete is False
    assert values["hour_requests"] == 5
    assert values["hour_4xx_rate"] is None
    assert values["hour_5xx_rate"] is None


def test_cloudfront_metrics_reject_rate_samples_when_requests_are_empty() -> None:
    provider_metrics, provider_complete = source(
        cloudwatch_global=CloudWatch({"4xxErrorRate": [1.5]})
    )._cloudfront_metrics(NOW, distribution_id="E123456789AB")
    values = {metric.name: metric.value for metric in provider_metrics}

    assert provider_complete is False
    assert values["hour_requests"] == 0
    assert values["hour_4xx_rate"] == "1.500"
    assert values["hour_5xx_rate"] == "0.000"


def test_sqs_includes_oldest_age_without_reading_messages_or_returning_queue_name() -> None:
    class Sqs:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def get_queue_attributes(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            return {
                "Attributes": {
                    "ApproximateNumberOfMessages": "2",
                    "ApproximateNumberOfMessagesNotVisible": "0",
                    "ApproximateNumberOfMessagesDelayed": "0",
                    "SqsManagedSseEnabled": "true",
                    "MessageRetentionPeriod": "1209600",
                }
            }

    sqs = Sqs()
    cloudwatch = CloudWatch()
    section = source(sqs=sqs, cloudwatch=cloudwatch)._sqs_section(NOW)

    assert section.state == "warning"
    assert "非同期処理" in section.summary
    assert metrics(section)["oldest_message_age_seconds"] == "42.000"
    assert sqs.calls[0]["AttributeNames"] and "ReceiveMessage" not in repr(sqs.calls)
    assert "projector-dlq" not in section.model_dump_json()
    assert cloudwatch.statistics_calls[0]["Dimensions"] == [
        {"Name": "QueueName", "Value": "projector-dlq"}
    ]


@pytest.mark.parametrize(
    ("encrypted", "retention"),
    ((False, "1209600"), (True, "345600")),
)
def test_sqs_state_includes_encryption_and_retention(
    encrypted: bool,
    retention: str,
) -> None:
    class Sqs:
        def get_queue_attributes(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "Attributes": {
                    "ApproximateNumberOfMessages": "0",
                    "ApproximateNumberOfMessagesNotVisible": "0",
                    "ApproximateNumberOfMessagesDelayed": "0",
                    "SqsManagedSseEnabled": "true" if encrypted else "false",
                    "MessageRetentionPeriod": retention,
                }
            }

    section = source(sqs=Sqs())._sqs_section(NOW)

    assert section.state == "warning"


def test_sqs_memorial_inflight_is_healthy_but_dlq_or_stale_work_warns() -> None:
    class Sqs:
        def __init__(self, *, dlq_visible: int = 0) -> None:
            self.dlq_visible = dlq_visible

        def get_queue_attributes(self, *, QueueUrl: str, **_kwargs: Any) -> dict[str, Any]:
            is_memorial = QueueUrl.endswith("/memorial-generation")
            is_memorial_dlq = QueueUrl.endswith("/memorial-generation-dlq")
            return {
                "Attributes": {
                    "ApproximateNumberOfMessages": str(self.dlq_visible if is_memorial_dlq else 0),
                    "ApproximateNumberOfMessagesNotVisible": "1" if is_memorial else "0",
                    "ApproximateNumberOfMessagesDelayed": "0",
                    "SqsManagedSseEnabled": "true",
                    "MessageRetentionPeriod": "86400" if is_memorial else "1209600",
                }
            }

    class FreshCloudWatch(CloudWatch):
        def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
            self.statistics_calls.append(kwargs)
            return {"Datapoints": [{"Timestamp": NOW, "Maximum": 30.0}]}

    healthy = source(sqs=Sqs(), cloudwatch=FreshCloudWatch())._sqs_section(NOW)
    warning = source(
        sqs=Sqs(dlq_visible=1),
        cloudwatch=FreshCloudWatch(),
    )._sqs_section(NOW)

    assert healthy.state == "healthy"
    assert metrics(healthy)["memorial_inflight_messages"] == 1
    assert metrics(healthy)["memorial_dlq_visible_messages"] == 0
    assert warning.state == "warning"
    assert metrics(warning)["memorial_dlq_visible_messages"] == 1


def test_apigateway_reports_allowlisted_apis_without_exposing_ids() -> None:
    class ApiGateway:
        def get_api(self, *, ApiId: str) -> dict[str, Any]:
            return {
                "Name": {
                    "discord-id": "shittim-chest-production-discord-interactions",
                    "records-id": "shittim-chest-production-records",
                }[ApiId],
                "ProtocolType": "HTTP",
            }

        def get_stages(self, **_kwargs: Any) -> dict[str, Any]:
            return {"Items": [{"StageName": "$default", "AutoDeploy": True}]}

    class ApiMetrics(CloudWatch):
        def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
            values = (10.0, 1.0, 0.0, 125.5, 88.25) * 2
            return {
                "MetricDataResults": [
                    {"Id": query["Id"], "StatusCode": "Complete", "Values": [value]}
                    for query, value in zip(kwargs["MetricDataQueries"], values, strict=True)
                ]
            }

    status_source = source(apigateway=ApiGateway(), cloudwatch=ApiMetrics())
    cast(Any, status_source)._stack_resources = lambda stack, _type: (
        "discord-id" if stack == "runtime" else "records-id",
    )

    section = status_source._apigateway_section(NOW)
    values = metrics(section)

    assert section.state == "healthy"
    assert values["discord_hour_requests"] == 10
    assert values["records_hour_5xx"] == 0
    assert "discord-id" not in section.model_dump_json()
    assert "records-id" not in section.model_dump_json()


def test_stack_resource_lookup_does_not_reuse_replaced_physical_ids() -> None:
    class ChangingPaginator:
        def __init__(self) -> None:
            self.calls = 0

        def paginate(self, **_kwargs: Any) -> list[dict[str, Any]]:
            self.calls += 1
            return [
                {
                    "StackResourceSummaries": [
                        {
                            "ResourceType": "AWS::ApiGatewayV2::Api",
                            "PhysicalResourceId": f"api-{self.calls}",
                        }
                    ]
                }
            ]

    paginator = ChangingPaginator()

    class CloudFormation:
        def get_paginator(self, name: str) -> ChangingPaginator:
            assert name == "list_stack_resources"
            return paginator

    status_source = source(cloudformation=CloudFormation())

    first = status_source._stack_resources("runtime", "AWS::ApiGatewayV2::Api")
    second = status_source._stack_resources("runtime", "AWS::ApiGatewayV2::Api")

    assert first == ("api-1",)
    assert second == ("api-2",)
    assert paginator.calls == 2


def test_eventbridge_reports_schedule_and_rules_without_names() -> None:
    descriptions = status_adapters._EVENT_RULE_DESCRIPTIONS
    rules = {
        "ranking-rule": descriptions["ranking"],
        "aws-rule": descriptions["aws_fx"],
        "openai-rule": descriptions["openai"],
        "translation-rule": descriptions["inspector_translation"],
        "stop-rule": descriptions["abnormal_stop"],
    }

    class Events:
        def describe_rule(self, *, Name: str) -> dict[str, Any]:
            return {
                "Name": Name,
                "Description": rules[Name],
                "State": "ENABLED",
                "ScheduleExpression": None if Name == "stop-rule" else "rate(15 minutes)",
            }

    class Scheduler:
        def get_schedule(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "Name": configuration().runtime_scheduler_name,
                "State": "ENABLED",
                "ScheduleExpression": "rate(1 minute)",
                "Target": {"RetryPolicy": {"MaximumRetryAttempts": 2}},
            }

    class EventMetrics(CloudWatch):
        def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "MetricDataResults": [
                    {
                        "Id": query["Id"],
                        "StatusCode": "Complete",
                        "Values": [
                            0.0
                            if query["MetricStat"]["Metric"]["MetricName"] == "FailedInvocations"
                            else 1.0
                        ],
                    }
                    for query in kwargs["MetricDataQueries"]
                ]
            }

    status_source = source(
        events=Events(),
        scheduler=Scheduler(),
        cloudwatch=EventMetrics(),
    )
    cast(Any, status_source)._stack_resources = lambda stack, _type: (
        ("ranking-rule", "aws-rule", "openai-rule", "translation-rule")
        if stack == "records_application"
        else ("stop-rule",)
    )

    section = status_source._eventbridge_section(NOW)
    values = metrics(section)

    assert section.state == "healthy"
    assert values["runtime_retry_attempts"] == 2
    assert values["abnormal_stop_expression"] == "event pattern"
    assert not any(name in section.model_dump_json() for name in rules)


def test_cloudformation_uses_last_recorded_drift_for_eight_stacks() -> None:
    class CloudFormation:
        def describe_stacks(self, *, StackName: str) -> dict[str, Any]:
            return {
                "Stacks": [
                    {
                        "StackName": StackName,
                        "StackStatus": "UPDATE_COMPLETE",
                        "DriftInformation": {"StackDriftStatus": "IN_SYNC"},
                        "EnableTerminationProtection": True,
                        "LastUpdatedTime": NOW,
                    }
                ]
            }

    client = CloudFormation()
    status_source = source(cloudformation=client, cloudformation_global=client)
    section = status_source._cloudformation_section()
    values = metrics(section)

    assert section.state == "healthy"
    assert values["records_edge_drift"] == "IN_SYNC"
    assert len(section.metrics) == 8 * 4
    assert "ShittimChest" not in section.model_dump_json()


@pytest.mark.parametrize(
    ("drift", "expected_state"),
    (
        ("DRIFTED", "warning"),
        ("NOT_CHECKED", "unknown"),
        ("UNKNOWN", "unknown"),
        ("CHECK_IN_PROGRESS", "unknown"),
        ("FUTURE_STATUS", "unknown"),
    ),
)
def test_cloudformation_classifies_stack_drift_status(
    drift: str,
    expected_state: str,
) -> None:
    class CloudFormation:
        def describe_stacks(self, *, StackName: str) -> dict[str, Any]:
            return {
                "Stacks": [
                    {
                        "StackName": StackName,
                        "StackStatus": "UPDATE_COMPLETE",
                        "DriftInformation": {"StackDriftStatus": drift},
                        "EnableTerminationProtection": True,
                        "LastUpdatedTime": NOW,
                    }
                ]
            }

    client = CloudFormation()
    section = source(cloudformation=client, cloudformation_global=client)._cloudformation_section()

    assert section.state == expected_state


def test_sns_reports_delivery_counts_without_subscription_addresses() -> None:
    class Sns:
        def get_topic_attributes(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "Attributes": {
                    "SubscriptionsConfirmed": "1",
                    "SubscriptionsPending": "0",
                    "PrivateAddress": "private-recipient",
                }
            }

    class SnsMetrics(CloudWatch):
        def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "MetricDataResults": [
                    {
                        "Id": query["Id"],
                        "StatusCode": "Complete",
                        "Values": [
                            0.0
                            if query["MetricStat"]["Metric"]["MetricName"]
                            == "NumberOfNotificationsFailed"
                            else 1.0
                        ],
                    }
                    for query in kwargs["MetricDataQueries"]
                ]
            }

    section = source(sns=Sns(), cloudwatch=SnsMetrics())._sns_section(NOW)
    values = metrics(section)

    assert section.state == "healthy"
    assert values["confirmed_subscriptions"] == 1
    assert "private-recipient" not in section.model_dump_json()


def test_ssm_checks_metadata_only_and_groups_readiness() -> None:
    class CloudFormation:
        def describe_stacks(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "Stacks": [
                    {
                        "Parameters": [
                            {"ParameterKey": "RuntimeConfigVersion", "ParameterValue": "v0001"}
                        ]
                    }
                ]
            }

    configured_names = set(configuration().static_parameters.values())
    configured_names.update(
        {
            "/shittim-chest/production/runtime/v0001",
            "/shittim-chest/production/personas/v0001/moderator",
            "/shittim-chest/production/personas/v0001/participant-a",
            "/shittim-chest/production/personas/v0001/participant-b",
            "/shittim-chest/production/personas/v0001/participant-c",
        }
    )
    expected_names = configured_names - {"/shittim-chest/production/runtime-prompts/active"}
    metadata = [
        {
            "Name": name,
            "Type": "SecureString",
            "Version": 1,
            "LastModifiedDate": NOW,
        }
        for name in sorted(expected_names)
    ]

    class Ssm:
        def __init__(self) -> None:
            self.paginator = Paginator(
                [
                    {
                        "Parameters": [
                            *metadata,
                            {
                                "Name": "/shittim-chest/production/unrelated",
                                "Type": "SecureString",
                                "Version": 1,
                                "LastModifiedDate": NOW,
                            },
                        ]
                    },
                ]
            )

        def get_paginator(self, name: str) -> Paginator:
            assert name == "describe_parameters"
            return self.paginator

    ssm = Ssm()
    section = source(cloudformation=CloudFormation(), ssm=ssm)._ssm_section()
    values = metrics(section)

    assert section.state == "healthy"
    assert values["discord_ready"] == 5
    assert values["runtime_ready"] == 6
    assert values["records_ready"] == 8
    assert values["cost_ready"] == 2
    assert values["runtime_prompt_pointer_present"] is False
    assert ssm.paginator.calls == [
        {
            "ParameterFilters": [
                {
                    "Key": "Name",
                    "Option": "Equals",
                    "Values": sorted(configured_names),
                }
            ],
            "PaginationConfig": {"PageSize": 50},
        }
    ]
    assert "/shittim-chest/" not in section.model_dump_json()


def test_cost_governance_exposes_percentages_without_amounts_or_addresses() -> None:
    class Budgets:
        def describe_budget(self, *, BudgetName: str, **_kwargs: Any) -> dict[str, Any]:
            actual = "10" if BudgetName.endswith("project") else "15"
            return {
                "Budget": {
                    "BudgetLimit": {"Amount": "20", "Unit": "USD"},
                    "CalculatedSpend": {
                        "ActualSpend": {"Amount": actual, "Unit": "USD"},
                        "ForecastedSpend": {"Amount": "16", "Unit": "USD"},
                    },
                    "HealthStatus": {"Status": "HEALTHY"},
                }
            }

    class CostExplorer:
        def get_anomaly_subscriptions(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "AnomalySubscriptions": [
                    {
                        "SubscriptionName": configuration().anomaly_subscription_name,
                        "Frequency": "DAILY",
                        "Subscribers": [
                            {
                                "Address": "private-recipient",
                                "Type": "EMAIL",
                                "Status": "CONFIRMED",
                            }
                        ],
                    }
                ]
            }

    section = source(
        budgets=Budgets(),
        cost_explorer=CostExplorer(),
    )._cost_governance_section()
    values = metrics(section)

    assert section.state == "healthy"
    assert values["project_actual_percent"] == "50"
    assert values["account_actual_percent"] == "75"
    serialized = section.model_dump_json()
    assert "private-recipient" not in serialized
    assert "shittim-chest-production" not in serialized


def test_signer_reports_profile_health_without_arn() -> None:
    class Signer:
        def get_signing_profile(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "profileName": configuration().signing_profile_name,
                "status": "Active",
                "platformId": "Notation-OCI-SHA384-ECDSA",
                "signatureValidityPeriod": {"value": 12, "type": "MONTHS"},
                "arn": "private-profile-arn",
            }

    section = source(signer=Signer())._signer_section()

    assert section.state == "healthy"
    assert metrics(section)["validity_value"] == 12
    assert "private-profile-arn" not in section.model_dump_json()


def test_external_reports_checkpoint_freshness_without_keys_or_cursors() -> None:
    class Dynamo:
        def get_item(self, *, Key: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            key = status_adapters.unmarshal_item(Key)
            source_name = cast(str, key["SK"])
            item = {
                **key,
                "schema_version": 1,
                "record_type": "cost_checkpoint",
                "source": source_name,
                "next_date": "2026-08-25",
                "initial_complete": True,
                "last_success_at": (NOW - timedelta(hours=1)).isoformat(),
            }
            return {"Item": status_adapters.marshal_item(item)}

    section = source(dynamodb=Dynamo())._external_section(NOW)
    values = metrics(section)

    assert section.state == "healthy"
    assert values["openai_fresh"] is True
    assert values["frankfurter_initial_complete"] is True
    serialized = section.model_dump_json()
    assert "COLLECTOR#COST" not in serialized
    assert "next_date" not in serialized


def test_dynamodb_affection_metrics_report_checkpoint_without_private_keys() -> None:
    generated_at = NOW - timedelta(minutes=10)

    class Dynamo:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def get_item(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            key = status_adapters.unmarshal_item(kwargs["Key"])
            if key["PK"] == "RANKING#AFFECTION":
                item = {
                    **key,
                    "schema_version": 1,
                    "record_type": "affection_ranking_pointer",
                    "generation_id": "a" * 32,
                    "generated_at": generated_at.isoformat(),
                    "profile_count": 7,
                    "page_count": 1,
                    "checksum": "a" * 64,
                }
            else:
                item = {
                    **key,
                    "schema_version": 1,
                    "record_type": "affection_seed_checkpoint",
                    "generated_at": generated_at.isoformat(),
                    "archive_count": 12,
                    "profile_count": 7,
                    "complete": True,
                }
            return {"Item": status_adapters.marshal_item(item)}

    dynamodb = Dynamo()
    status_metrics, state = source(dynamodb=dynamodb)._affection_dynamodb_metrics(NOW)
    values = {metric.name: metric.value for metric in status_metrics}

    assert state == "healthy"
    assert values == {
        "affection_ranking_ready": True,
        "affection_ranking_fresh": True,
        "affection_profile_count": 7,
        "affection_page_count": 1,
        "affection_ranking_generated_at": generated_at.isoformat(),
        "affection_seed_complete": True,
        "affection_seed_archive_count": 12,
    }
    assert all(call["ConsistentRead"] is True for call in dynamodb.calls)
    serialized = repr(status_metrics)
    assert "RANKING#AFFECTION" not in serialized
    assert "AFFECTION#SEED" not in serialized
    assert "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in serialized


def test_dynamodb_affection_metrics_warn_when_checkpoint_is_missing() -> None:
    class Dynamo:
        def get_item(self, **_kwargs: Any) -> dict[str, Any]:
            return {}

    status_metrics, state = source(dynamodb=Dynamo())._affection_dynamodb_metrics(NOW)
    values = {metric.name: metric.value for metric in status_metrics}

    assert state == "warning"
    assert values["affection_ranking_ready"] is False
    assert values["affection_ranking_fresh"] is None
    assert values["affection_seed_complete"] is None


def test_dynamodb_affection_metrics_warn_when_ranking_checkpoint_is_stale() -> None:
    generated_at = NOW - timedelta(minutes=36)

    class Dynamo:
        def get_item(self, *, Key: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            key = status_adapters.unmarshal_item(Key)
            item = (
                {
                    **key,
                    "schema_version": 1,
                    "record_type": "affection_ranking_pointer",
                    "generation_id": "b" * 32,
                    "generated_at": generated_at.isoformat(),
                    "profile_count": 1,
                    "page_count": 1,
                    "checksum": "b" * 64,
                }
                if key["PK"] == "RANKING#AFFECTION"
                else {
                    **key,
                    "schema_version": 1,
                    "record_type": "affection_seed_checkpoint",
                    "generated_at": generated_at.isoformat(),
                    "archive_count": 1,
                    "profile_count": 1,
                    "complete": True,
                }
            )
            return {"Item": status_adapters.marshal_item(item)}

    status_metrics, state = source(dynamodb=Dynamo())._affection_dynamodb_metrics(NOW)

    assert state == "warning"
    assert {metric.name: metric.value for metric in status_metrics}[
        "affection_ranking_fresh"
    ] is False


def test_dynamodb_affection_metrics_reject_inconsistent_atomic_checkpoints() -> None:
    class Dynamo:
        def get_item(self, *, Key: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            key = status_adapters.unmarshal_item(Key)
            item = (
                {
                    **key,
                    "schema_version": 1,
                    "record_type": "affection_ranking_pointer",
                    "generation_id": "c" * 32,
                    "generated_at": NOW.isoformat(),
                    "profile_count": 1,
                    "page_count": 1,
                    "checksum": "c" * 64,
                }
                if key["PK"] == "RANKING#AFFECTION"
                else {
                    **key,
                    "schema_version": 1,
                    "record_type": "affection_seed_checkpoint",
                    "generated_at": NOW.isoformat(),
                    "archive_count": 1,
                    "profile_count": 2,
                    "complete": True,
                }
            )
            return {"Item": status_adapters.marshal_item(item)}

    with pytest.raises(ValueError, match="inconsistent"):
        source(dynamodb=Dynamo())._affection_dynamodb_metrics(NOW)


def test_status_service_reuses_warm_cache_for_sixty_seconds() -> None:
    sections = tuple(
        AdminStatusSection(
            service=cast(Any, service),
            state="healthy",
            summary="正常です。",
            metrics=(),
        )
        for service, _method in STATUS_COLLECTORS
    )

    class StatusSource:
        calls = 0

        def collect(self, *, now: datetime) -> AdminStatusCollection:
            del now
            self.calls += 1
            return AdminStatusCollection(
                overall=AdminStatusOverall(
                    state="healthy",
                    critical_alarms=0,
                    warning_alarms=0,
                    partial=False,
                ),
                sections=sections,
            )

    provider = StatusSource()
    service = AdminStatusService(provider)

    first = service.get(now=NOW)
    cached = service.refresh(now=NOW + timedelta(seconds=59))
    stale = service.get(now=NOW + timedelta(seconds=60))
    refreshed = service.refresh(now=NOW + timedelta(seconds=60))

    assert provider.calls == 2
    assert cached.generated_at == first.generated_at
    assert stale.stale is True
    assert refreshed.stale is False


def test_critical_service_section_promotes_overall_state() -> None:
    class Alarms:
        def get_paginator(self, _name: str) -> Paginator:
            return Paginator([{"MetricAlarms": [], "CompositeAlarms": []}])

    status_source = source(cloudwatch=Alarms())
    healthy = AdminStatusSection(
        service="ecs",
        state="healthy",
        summary="正常です。",
        metrics=(),
    )
    critical = AdminStatusSection(
        service="inspector",
        state="critical",
        summary="確認が必要です。",
        metrics=(),
    )
    collectors: dict[str, Any] = {
        method: (lambda *_args, service=service: healthy.model_copy(update={"service": service}))
        for service, method in STATUS_COLLECTORS
    }
    collectors["_inspector_section"] = lambda: critical
    for name, collector in collectors.items():
        setattr(status_source, name, collector)

    result = status_source.collect(now=NOW)

    assert result.overall.state == "critical"


def test_status_sections_are_collected_in_parallel() -> None:
    status_source = source()
    barrier = threading.Barrier(len(STATUS_COLLECTORS))

    def collector(service: str) -> Any:
        def collect(*_args: Any) -> AdminStatusSection:
            barrier.wait(timeout=1)
            return AdminStatusSection(
                service=cast(Any, service),
                state="healthy",
                summary="正常です。",
                metrics=(),
            )

        return collect

    cast(Any, status_source)._alarm_counts = lambda: (0, 0, (), False)
    for service, name in STATUS_COLLECTORS:
        setattr(status_source, name, collector(service))

    result = status_source.collect(now=NOW)

    assert all(section.state == "healthy" for section in result.sections)
    assert result.overall.partial is False


def test_status_collection_budget_marks_unfinished_section_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_source = source()
    release = threading.Event()
    monkeypatch.setattr(status_adapters, "_STATUS_COLLECTION_TIMEOUT_SECONDS", 0.1)
    cast(Any, status_source)._alarm_counts = lambda: (0, 0, False)

    def healthy(service: str) -> AdminStatusSection:
        return AdminStatusSection(
            service=cast(Any, service),
            state="healthy",
            summary="正常です。",
            metrics=(),
        )

    def blocked_ecr() -> AdminStatusSection:
        release.wait(timeout=2)
        return healthy("ecr")

    collectors: dict[str, Any] = {
        method: (lambda *_args, service=service: healthy(service))
        for service, method in STATUS_COLLECTORS
    }
    collectors["_ecr_section"] = blocked_ecr
    for name, collector in collectors.items():
        setattr(status_source, name, collector)

    result = status_source.collect(now=NOW)
    release.set()

    states = {section.service: section.state for section in result.sections}
    assert states["ecr"] == "unknown"
    assert result.overall.partial is True
    assert result.overall.state == "warning"


def test_inspector_pagination_is_bounded() -> None:
    aggregation_pages = Paginator([{"responses": []}] * 21)

    class Inspector:
        def get_paginator(self, name: str) -> Paginator:
            assert name == "list_finding_aggregations"
            return aggregation_pages

    with pytest.raises(ValueError, match="bounded page count"):
        source(
            ecr=EcrInventory([tagged_image_detail()]),
            inspector=Inspector(),
        )._inspector_section()
