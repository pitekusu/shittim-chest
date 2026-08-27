"""Sanitized AWS status aggregation and cache tests."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

import shittim_records.admin_status_adapters as status_adapters
from shittim_records.admin_status import AdminStatusCollection, AdminStatusService
from shittim_records.admin_status_adapters import (
    AwsAdminStatusConfiguration,
    AwsAdminStatusSource,
)
from shittim_records.contracts import AdminStatusOverall, AdminStatusSection

NOW = datetime(2026, 8, 24, 3, 0, tzinfo=UTC)
AWS_ACCOUNT_ID = "123456" + "789012"
RUNTIME_DIGEST = "sha256:" + "a" * 64
BREAK_GLASS_DIGEST = "sha256:" + "b" * 64
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
        ecr_repository_name="private-repository-name",
        runtime_stack_name="ShittimChest-Prod-Runtime",
        buckets={
            "web": "private-web-bucket",
            "media": "private-media-bucket",
            "release": "private-release-bucket",
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
        stacks={
            "stateful": "ShittimChest-Prod-Stateful",
            "release_identity": "ShittimChest-Prod-ReleaseIdentity",
            "runtime": "ShittimChest-Prod-Runtime",
            "operations": "ShittimChest-Prod-Operations",
            "cost_governance": "ShittimChest-Prod-CostGovernance",
            "records_stateful": "ShittimChest-Prod-RecordsStateful",
            "records_application": "ShittimChest-Prod-RecordsApplication",
            "records_edge": "ShittimChest-Prod-RecordsEdge",
        },
        static_parameters={
            "discord_public_key": "/shittim-chest/production/discord/moderator/public-key",
            "moderator_token": "/shittim-chest/production/discord/moderator/token",
            "participant_a_token": "/shittim-chest/production/discord/participant-a/token",
            "participant_b_token": "/shittim-chest/production/discord/participant-b/token",
            "participant_c_token": "/shittim-chest/production/discord/participant-c/token",
            "openai_api_key": "/shittim-chest/production/openai/api-key",
            "runtime_prompts_active": "/shittim-chest/production/runtime-prompts/active",
            "records_identity": "/shittim-chest/production/records/identity-hmac-key",
            "records_presentation": "/shittim-chest/production/records/presentation/v0001",
            "records_oauth": "/shittim-chest/production/records/discord/oauth/v0001",
            "records_client_secret": "/shittim-chest/production/records/discord/client-secret",
            "records_session_key": "/shittim-chest/production/records/session-key",
            "records_openai_admin_key": "/shittim-chest/production/records/openai/admin-key",
            "records_openai_project_id": "/shittim-chest/production/records/openai/project-id",
            "records_admin_user_id": "/shittim-chest/production/records/admin/discord-user-id",
        },
        runtime_scheduler_name="shittim-chest-production-runtime-reconciler",
        sns_topic_arn=(
            f"arn:aws:sns:ap-northeast-1:{AWS_ACCOUNT_ID}:shittim-chest-production-operations"
        ),
        signing_profile_name="shittim_chest_ecr",
        budgets={
            "project": "shittim-chest-production-project",
            "account": "shittim-chest-production-account",
        },
        anomaly_subscription_name="shittim-chest-production-cost-anomalies",
    )


class Empty:
    pass


class CloudWatch:
    def __init__(self) -> None:
        self.statistics_calls: list[dict[str, Any]] = []

    def get_metric_statistics(self, **kwargs: Any) -> dict[str, Any]:
        self.statistics_calls.append(kwargs)
        return {"Datapoints": [{"Timestamp": NOW, "Maximum": 42.0}]}

    def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "MetricDataResults": [
                {"Id": query["Id"], "StatusCode": "Complete", "Values": [1.0]}
                for query in kwargs["MetricDataQueries"]
            ]
        }


class Paginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        return self.pages


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
    )


def metrics(section: AdminStatusSection) -> dict[str, str | int | bool | None]:
    return {metric.name: metric.value for metric in section.metrics}


def test_ecs_includes_runtime_heartbeat_without_exposing_resource_names() -> None:
    class Ecs:
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
                        "taskDefinition": (
                            f"arn:aws:ecs:ap-northeast-1:{AWS_ACCOUNT_ID}:"
                            "task-definition/private-runtime:42"
                        ),
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
    section = source(ecs=Ecs(), dynamodb=Dynamo(), cloudwatch=cloudwatch)._ecs_section(NOW)

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
    assert cloudwatch.statistics_calls[0]["Namespace"] == "ShittimChest/Prod"
    assert cloudwatch.statistics_calls[0]["MetricName"] == "HeartbeatAgeSeconds"
    assert configuration().cluster_name not in section.model_dump_json()
    assert configuration().service_name not in section.model_dump_json()


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
    class Ecs:
        def describe_services(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "services": [
                    {
                        "desiredCount": 1,
                        "runningCount": 1,
                        "pendingCount": 0,
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
        dynamodb=Dynamo(),
        cloudwatch=RuntimeCloudWatch(),
    )._ecs_section(NOW)

    assert section.state == expected_state
    assert section.summary == (
        "稼働中" if expected_state == "healthy" else "状態を取得できません。"
    )


def test_ecs_marks_failed_rollout_without_exposing_provider_reason() -> None:
    private_reason = "deployment failed for private-service-name"

    class Ecs:
        def describe_services(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "services": [
                    {
                        "desiredCount": 0,
                        "runningCount": 0,
                        "pendingCount": 0,
                        "status": "ACTIVE",
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

    section = source(ecs=Ecs(), dynamodb=Dynamo())._ecs_section(NOW)

    assert section.state == "warning"
    assert section.summary == "デプロイ状態の確認が必要です。"
    assert metrics(section)["rollout_state"] == "FAILED"
    assert metrics(section)["failed_task_count"] == 1
    assert private_reason not in section.model_dump_json()


def test_dynamodb_includes_stream_and_one_hour_throttles() -> None:
    class Dynamo:
        def describe_table(self, *, TableName: str) -> dict[str, Any]:
            table: dict[str, Any] = {
                "TableStatus": "ACTIVE",
                "DeletionProtectionEnabled": True,
                "ItemCount": 4,
            }
            if TableName == configuration().tables["debate"]:
                table["StreamSpecification"] = {
                    "StreamEnabled": True,
                    "StreamViewType": "NEW_IMAGE",
                }
            return {"Table": table}

        def describe_continuous_backups(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "ContinuousBackupsDescription": {
                    "PointInTimeRecoveryDescription": {"PointInTimeRecoveryStatus": "ENABLED"}
                }
            }

        def describe_time_to_live(self, **_kwargs: Any) -> dict[str, Any]:
            return {"TimeToLiveDescription": {"TimeToLiveStatus": "ENABLED"}}

    section = source(dynamodb=Dynamo())._dynamodb_section(NOW)
    values = metrics(section)

    assert section.state == "warning"
    assert values["debate_stream_enabled"] is True
    assert values["debate_stream_view_type"] == "NEW_IMAGE"
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
    class Dynamo:
        def describe_table(self, *, TableName: str) -> dict[str, Any]:
            stream = (
                {"StreamEnabled": True, "StreamViewType": stream_view_type}
                if TableName == configuration().tables["debate"]
                else {}
            )
            return {
                "Table": {
                    "TableStatus": "ACTIVE",
                    "DeletionProtectionEnabled": True,
                    "ItemCount": 0,
                    "StreamSpecification": stream,
                }
            }

        def describe_continuous_backups(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "ContinuousBackupsDescription": {
                    "PointInTimeRecoveryDescription": {"PointInTimeRecoveryStatus": "ENABLED"}
                }
            }

        def describe_time_to_live(self, **_kwargs: Any) -> dict[str, Any]:
            return {"TimeToLiveDescription": {"TimeToLiveStatus": "ENABLED"}}

    class ZeroCloudWatch(CloudWatch):
        def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "MetricDataResults": [
                    {"Id": query["Id"], "StatusCode": "Complete", "Values": [0.0]}
                    for query in kwargs["MetricDataQueries"]
                ]
            }

    section = source(dynamodb=Dynamo(), cloudwatch=ZeroCloudWatch())._dynamodb_section(NOW)

    assert section.state == expected_state
    assert metrics(section)["debate_stream_view_type"] == stream_view_type


def test_dynamodb_requires_session_ttl_to_be_enabled() -> None:
    class Dynamo:
        def describe_table(self, *, TableName: str) -> dict[str, Any]:
            stream = (
                {"StreamEnabled": True, "StreamViewType": "NEW_IMAGE"}
                if TableName == configuration().tables["debate"]
                else {}
            )
            return {
                "Table": {
                    "TableStatus": "ACTIVE",
                    "DeletionProtectionEnabled": True,
                    "ItemCount": 0,
                    "StreamSpecification": stream,
                }
            }

        def describe_continuous_backups(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "ContinuousBackupsDescription": {
                    "PointInTimeRecoveryDescription": {"PointInTimeRecoveryStatus": "ENABLED"}
                }
            }

        def describe_time_to_live(self, *, TableName: str) -> dict[str, Any]:
            status = "DISABLED" if TableName == configuration().tables["session"] else "ENABLED"
            return {"TimeToLiveDescription": {"TimeToLiveStatus": status}}

    section = source(dynamodb=Dynamo())._dynamodb_section(NOW)

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
                            {
                                "ParameterKey": "BreakGlassImageDigest",
                                "ParameterValue": BREAK_GLASS_DIGEST,
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
                                "imageManifestMediaType": (
                                    "application/vnd.oci.image.manifest.v1+json"
                                ),
                            },
                            {
                                "imageDigest": BREAK_GLASS_DIGEST,
                                "imageTags": ["break-glass"],
                                "imagePushedAt": NOW - timedelta(minutes=1),
                                "imageSizeInBytes": 256,
                                "imageManifestMediaType": (
                                    "application/vnd.docker.distribution.manifest.v2+json"
                                ),
                            },
                            {
                                "imageDigest": "sha256:" + "c" * 64,
                                "imagePushedAt": NOW - timedelta(minutes=2),
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
    assert values["normal_image_present"] is True
    assert values["break_glass_image_present"] is True
    assert values["repository_image_count"] == 3
    assert values["repository_tagged_image_count"] == 2
    assert values["repository_untagged_image_count"] == 1
    assert values["repository_total_size_bytes"] == 448
    assert values["normal_tag_count"] == 1
    assert values["normal_media_type"] == "OCI_IMAGE"
    assert values["normal_last_pulled_at"] == NOW.isoformat()
    assert values["break_glass_media_type"] == "DOCKER_V2"
    assert "sha256:" not in section.model_dump_json()
    assert RUNTIME_DIGEST[:19] not in section.model_dump_json()


def test_ecr_rejects_missing_runtime_stack_digest_without_using_stale_configuration() -> None:
    class CloudFormation:
        def describe_stacks(self, **_kwargs: Any) -> dict[str, Any]:
            return {"Stacks": [{"Parameters": []}]}

    with pytest.raises(ValueError, match="Runtime image digest is invalid"):
        source(cloudformation=CloudFormation())._runtime_image_digests()


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
                            {
                                "ParameterKey": "BreakGlassImageDigest",
                                "ParameterValue": BREAK_GLASS_DIGEST,
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
                                "imageDigest": digest,
                                "imagePushedAt": NOW,
                                "imageSizeInBytes": 128,
                            }
                            for digest in (RUNTIME_DIGEST, BREAK_GLASS_DIGEST)
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

    assert result == (0, 0, False)
    assert alarms.calls == [
        {
            "AlarmNamePrefix": "shittim-chest-production-",
            "AlarmTypes": ["CompositeAlarm"],
            "StateValue": "ALARM",
        }
    ]


def test_alarm_counts_only_severity_bearing_composites() -> None:
    alarms = Paginator(
        [
            {
                "MetricAlarms": [{"AlarmName": "shittim-chest-production-heartbeat-stale"}],
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

    assert source(cloudwatch=Alarms())._alarm_counts() == (1, 1, False)


def test_cloudfront_derives_the_current_certificate_from_the_exact_distribution() -> None:
    certificate_arn = (
        f"arn:aws:acm:us-east-1:{AWS_ACCOUNT_ID}:certificate/12345678-1234-1234-1234-123456789abc"
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
                            "ACMCertificateArn": certificate_arn,
                            "MinimumProtocolVersion": "TLSv1.3_2025",
                        },
                    },
                }
            }

        def list_invalidations(self, **_kwargs: Any) -> dict[str, Any]:
            return {"InvalidationList": {"Items": []}}

    class Acm:
        requested_arn: str | None = None

        def describe_certificate(self, *, CertificateArn: str) -> dict[str, Any]:
            self.requested_arn = CertificateArn
            return {
                "Certificate": {
                    "Status": "ISSUED",
                    "KeyAlgorithm": "EC_prime256v1",
                    "NotAfter": NOW + timedelta(days=300),
                }
            }

    acm = Acm()
    cloudfront = CloudFront()
    section = source(cloudfront=cloudfront, acm=acm)._cloudfront_section(NOW)

    assert section.state == "healthy"
    assert cloudfront.requested_distribution == "E123456789AB"
    assert acm.requested_arn == certificate_arn
    assert certificate_arn not in section.model_dump_json()


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
    certificate_arn = (
        f"arn:aws:acm:us-east-1:{AWS_ACCOUNT_ID}:certificate/12345678-1234-1234-1234-123456789abc"
    )

    class CloudFront:
        def get_paginator(self, _name: str) -> Paginator:
            return distribution_pages()

        def get_distribution(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "Distribution": {
                    "Status": "Deployed",
                    "DistributionConfig": {
                        "Enabled": True,
                        "ViewerCertificate": {"ACMCertificateArn": certificate_arn},
                    },
                }
            }

        def list_invalidations(self, **_kwargs: Any) -> dict[str, Any]:
            return {"InvalidationList": {"Items": []}}

    class Acm:
        def describe_certificate(self, **_kwargs: Any) -> dict[str, Any]:
            return {"Certificate": certificate}

    section = source(cloudfront=CloudFront(), acm=Acm())._cloudfront_section(NOW)

    assert section.state == expected_state


def test_inspector_includes_repository_coverage_and_last_scan() -> None:
    finding_pages = Paginator([{"findings": [{"severity": "HIGH"}]}])
    coverage_pages = Paginator(
        [
            {
                "coveredResources": [
                    {
                        "resourceId": "private-resource",
                        "accountId": AWS_ACCOUNT_ID,
                        "scanStatus": {"statusCode": "ACTIVE"},
                        "lastScannedAt": NOW,
                    }
                ]
            }
        ]
    )

    class Inspector:
        def get_paginator(self, name: str) -> Paginator:
            return finding_pages if name == "list_findings" else coverage_pages

    section = source(inspector=Inspector())._inspector_section()
    values = metrics(section)

    assert section.state == "warning"
    assert values["coverage_count"] == 1
    assert values["coverage_active"] == 1
    assert values["last_scanned_at"] == NOW.isoformat()
    assert "private-resource" not in section.model_dump_json()
    assert AWS_ACCOUNT_ID not in section.model_dump_json()


def test_inspector_treats_untriaged_findings_as_warning() -> None:
    finding_pages = Paginator([{"findings": [{"severity": "UNTRIAGED"}]}])
    coverage_pages = Paginator(
        [{"coveredResources": [{"scanStatus": {"statusCode": "ACTIVE"}, "lastScannedAt": NOW}]}]
    )

    class Inspector:
        def get_paginator(self, name: str) -> Paginator:
            return finding_pages if name == "list_findings" else coverage_pages

    section = source(inspector=Inspector())._inspector_section()

    assert section.state == "warning"
    assert metrics(section)["active_untriaged"] == 1


@pytest.mark.parametrize("failure", ("partial", "missing"))
def test_lambda_section_is_unknown_for_incomplete_provider_metrics(failure: str) -> None:
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

    class IdleCloudWatch(CloudWatch):
        def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "MetricDataResults": [
                    {"Id": query["Id"], "StatusCode": "Complete", "Values": []}
                    for query in kwargs["MetricDataQueries"]
                ]
            }

    section = source(lambda_client=Lambda(), cloudwatch=IdleCloudWatch())._lambda_section(NOW)
    values = metrics(section)

    assert section.state == "healthy"
    assert values["auth_hour_invocations"] == 0
    assert values["auth_hour_errors"] == 0
    assert values["auth_hour_throttles"] == 0
    assert values["auth_hour_duration"] is None


def test_lambda_section_requires_duration_when_invocations_exist() -> None:
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

    class MissingDurationCloudWatch(CloudWatch):
        def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
            results = []
            for query in kwargs["MetricDataQueries"]:
                metric_name = query["MetricStat"]["Metric"]["MetricName"]
                values = [] if metric_name == "Duration" else [1.0]
                results.append({"Id": query["Id"], "StatusCode": "Complete", "Values": values})
            return {"MetricDataResults": results}

    section = source(
        lambda_client=Lambda(),
        cloudwatch=MissingDurationCloudWatch(),
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

    class MismatchedCloudWatch(CloudWatch):
        def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
            results = []
            for query in kwargs["MetricDataQueries"]:
                metric_name = query["MetricStat"]["Metric"]["MetricName"]
                values = [sample_value] if metric_name == sampled_metric else []
                results.append({"Id": query["Id"], "StatusCode": "Complete", "Values": values})
            return {"MetricDataResults": results}

    section = source(
        lambda_client=Lambda(),
        cloudwatch=MismatchedCloudWatch(),
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

    class LambdaCloudWatch(CloudWatch):
        def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
            results = []
            for query in kwargs["MetricDataQueries"]:
                metric_name = query["MetricStat"]["Metric"]["MetricName"]
                value = 1.0 if metric_name in {"Invocations", failing_metric} else 0.0
                if metric_name == "Duration":
                    value = 12.5
                results.append({"Id": query["Id"], "StatusCode": "Complete", "Values": [value]})
            return {"MetricDataResults": results}

    section = source(
        lambda_client=Lambda(),
        cloudwatch=LambdaCloudWatch(),
    )._lambda_section(NOW)

    assert section.state == expected_state


def test_lambda_section_propagates_reserved_concurrency_provider_failure() -> None:
    class Lambda:
        def get_function_configuration(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "State": "Active",
                "LastUpdateStatus": "Successful",
                "Runtime": "python3.14",
                "Architectures": ["arm64"],
            }

        def get_function_concurrency(self, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("provider failure")

    with pytest.raises(RuntimeError, match="provider failure"):
        source(lambda_client=Lambda())._lambda_section(NOW)


def test_cloudfront_section_is_unknown_for_incomplete_provider_metrics() -> None:
    certificate_arn = (
        f"arn:aws:acm:us-east-1:{AWS_ACCOUNT_ID}:certificate/12345678-1234-1234-1234-123456789abc"
    )

    class CloudFront:
        def get_paginator(self, _name: str) -> Paginator:
            return distribution_pages()

        def get_distribution(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "Distribution": {
                    "Status": "Deployed",
                    "DistributionConfig": {
                        "Enabled": True,
                        "ViewerCertificate": {"ACMCertificateArn": certificate_arn},
                    },
                }
            }

        def list_invalidations(self, **_kwargs: Any) -> dict[str, Any]:
            return {"InvalidationList": {"Items": []}}

    class Acm:
        def describe_certificate(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "Certificate": {
                    "Status": "ISSUED",
                    "KeyAlgorithm": "EC_prime256v1",
                    "NotAfter": NOW + timedelta(days=30),
                }
            }

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
    certificate_arn = (
        f"arn:aws:acm:us-east-1:{AWS_ACCOUNT_ID}:certificate/12345678-1234-1234-1234-123456789abc"
    )

    class CloudFront:
        def get_paginator(self, _name: str) -> Paginator:
            return distribution_pages()

        def get_distribution(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "Distribution": {
                    "Status": "Deployed",
                    "DistributionConfig": {
                        "Enabled": True,
                        "ViewerCertificate": {"ACMCertificateArn": certificate_arn},
                    },
                }
            }

        def list_invalidations(self, **_kwargs: Any) -> dict[str, Any]:
            return {"InvalidationList": {"Items": []}}

    class Acm:
        def describe_certificate(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "Certificate": {
                    "Status": "ISSUED",
                    "KeyAlgorithm": "EC_prime256v1",
                    "NotAfter": NOW + timedelta(days=30),
                }
            }

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
    class MissingRatesCloudWatch(CloudWatch):
        def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
            results = []
            for query in kwargs["MetricDataQueries"]:
                metric_name = query["MetricStat"]["Metric"]["MetricName"]
                samples = [5.0] if metric_name == "Requests" else []
                results.append({"Id": query["Id"], "StatusCode": "Complete", "Values": samples})
            return {"MetricDataResults": results}

    provider_metrics, provider_complete = source(
        cloudwatch_global=MissingRatesCloudWatch()
    )._cloudfront_metrics(NOW, distribution_id="E123456789AB")
    values = {metric.name: metric.value for metric in provider_metrics}

    assert provider_complete is False
    assert values["hour_requests"] == 5
    assert values["hour_4xx_rate"] is None
    assert values["hour_5xx_rate"] is None


def test_cloudfront_metrics_reject_rate_samples_when_requests_are_empty() -> None:
    class MismatchedCloudWatch(CloudWatch):
        def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
            results = []
            for query in kwargs["MetricDataQueries"]:
                metric_name = query["MetricStat"]["Metric"]["MetricName"]
                samples = [1.5] if metric_name == "4xxErrorRate" else []
                results.append({"Id": query["Id"], "StatusCode": "Complete", "Values": samples})
            return {"MetricDataResults": results}

    provider_metrics, provider_complete = source(
        cloudwatch_global=MismatchedCloudWatch()
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
        ("ranking-rule", "aws-rule", "openai-rule")
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

    class Ssm:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def describe_parameters(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            name = kwargs["ParameterFilters"][0]["Values"][0]
            if name.endswith("runtime-prompts/active"):
                return {"Parameters": []}
            return {
                "Parameters": [
                    {
                        "Name": name,
                        "Type": "SecureString",
                        "Version": 1,
                        "LastModifiedDate": NOW,
                    }
                ]
            }

    ssm = Ssm()
    section = source(cloudformation=CloudFormation(), ssm=ssm)._ssm_section()
    values = metrics(section)

    assert section.state == "healthy"
    assert values["discord_ready"] == 5
    assert values["runtime_ready"] == 6
    assert values["records_ready"] == 6
    assert values["cost_ready"] == 2
    assert values["runtime_prompt_pointer_present"] is False
    assert len(ssm.calls) == 20
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

    cast(Any, status_source)._alarm_counts = lambda: (0, 0, False)
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
    finding_pages = Paginator([{"findings": []}] * 21)

    class Inspector:
        def get_paginator(self, _name: str) -> Paginator:
            return finding_pages

    with pytest.raises(ValueError, match="bounded page count"):
        source(inspector=Inspector())._inspector_section()
