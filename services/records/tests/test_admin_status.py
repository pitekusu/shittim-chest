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
        cloudwatch=cloudwatch,
        cloudwatch_global=clients.get("cloudwatch_global", cloudwatch),
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
                        "deployments": [],
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
            self.image_ids: list[dict[str, str]] = []

        def describe_repositories(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "repositories": [
                    {
                        "imageTagMutability": "IMMUTABLE",
                        "encryptionConfiguration": {"encryptionType": "KMS"},
                    }
                ]
            }

        def describe_images(self, **kwargs: Any) -> dict[str, Any]:
            self.image_ids = kwargs["imageIds"]
            return {
                "imageDetails": [
                    {
                        "imageDigest": image["imageDigest"],
                        "imagePushedAt": NOW,
                        "imageSizeInBytes": 128,
                    }
                    for image in self.image_ids
                ]
            }

    ecr = Ecr()
    cloudformation = CloudFormation()
    section = source(ecr=ecr, cloudformation=cloudformation)._ecr_section()

    assert section.state == "healthy"
    assert ecr.image_ids == [
        {"imageDigest": RUNTIME_DIGEST},
        {"imageDigest": BREAK_GLASS_DIGEST},
    ]
    assert cloudformation.stack_names == [configuration().runtime_stack_name]
    values = metrics(section)
    assert values["normal_image_present"] is True
    assert values["break_glass_image_present"] is True
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
        def describe_repositories(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "repositories": [
                    {
                        "imageTagMutability": "MUTABLE",
                        "encryptionConfiguration": {"encryptionType": "AES256"},
                    }
                ]
            }

        def describe_images(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "imageDetails": [
                    {"imageDigest": image["imageDigest"], "imagePushedAt": NOW}
                    for image in kwargs["imageIds"]
                ]
            }

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
                value = 1.0 if metric_name == failing_metric else 0.0
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


def test_status_service_reuses_warm_cache_for_sixty_seconds() -> None:
    sections = tuple(
        AdminStatusSection(
            service=cast(Any, service),
            state="healthy",
            summary="正常です。",
            metrics=(),
        )
        for service in (
            "ecs",
            "ecr",
            "inspector",
            "s3",
            "dynamodb",
            "lambda",
            "cloudfront",
            "sqs",
        )
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
    collectors = {
        "_ecs_section": lambda _now: healthy,
        "_ecr_section": lambda: healthy.model_copy(update={"service": "ecr"}),
        "_inspector_section": lambda: critical,
        "_s3_section": lambda: healthy.model_copy(update={"service": "s3"}),
        "_dynamodb_section": lambda _now: healthy.model_copy(update={"service": "dynamodb"}),
        "_lambda_section": lambda _now: healthy.model_copy(update={"service": "lambda"}),
        "_cloudfront_section": lambda _now: healthy.model_copy(update={"service": "cloudfront"}),
        "_sqs_section": lambda _now: healthy.model_copy(update={"service": "sqs"}),
    }
    for name, collector in collectors.items():
        setattr(status_source, name, collector)

    result = status_source.collect(now=NOW)

    assert result.overall.state == "critical"


def test_status_sections_are_collected_in_parallel() -> None:
    status_source = source()
    barrier = threading.Barrier(8)

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
    for service, name in (
        ("ecs", "_ecs_section"),
        ("ecr", "_ecr_section"),
        ("inspector", "_inspector_section"),
        ("s3", "_s3_section"),
        ("dynamodb", "_dynamodb_section"),
        ("lambda", "_lambda_section"),
        ("cloudfront", "_cloudfront_section"),
        ("sqs", "_sqs_section"),
    ):
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

    for name, collector in {
        "_ecs_section": lambda _now: healthy("ecs"),
        "_ecr_section": blocked_ecr,
        "_inspector_section": lambda: healthy("inspector"),
        "_s3_section": lambda: healthy("s3"),
        "_dynamodb_section": lambda _now: healthy("dynamodb"),
        "_lambda_section": lambda _now: healthy("lambda"),
        "_cloudfront_section": lambda _now: healthy("cloudfront"),
        "_sqs_section": lambda _now: healthy("sqs"),
    }.items():
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
