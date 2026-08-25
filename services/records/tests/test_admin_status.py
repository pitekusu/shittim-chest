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


def configuration() -> AwsAdminStatusConfiguration:
    return AwsAdminStatusConfiguration(
        aws_account_id=AWS_ACCOUNT_ID,
        cluster_name="private-cluster-name",
        service_name="private-service-name",
        ecr_repository_name="private-repository-name",
        runtime_image_digest="sha256:" + "a" * 64,
        break_glass_image_digest="sha256:" + "b" * 64,
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
        distribution_id="distribution",
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
                {"Id": query["Id"], "Values": [1.0]} for query in kwargs["MetricDataQueries"]
            ]
        }


def source(**clients: Any) -> AwsAdminStatusSource:
    empty = Empty()
    cloudwatch = clients.get("cloudwatch", CloudWatch())
    return AwsAdminStatusSource(
        configuration=configuration(),
        ecs=clients.get("ecs", empty),
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

    assert values["debate_stream_enabled"] is True
    assert values["debate_stream_view_type"] == "NEW_IMAGE"
    for label in ("debate", "archive", "statistics", "session"):
        assert values[f"{label}_read_throttles"] == 1
        assert values[f"{label}_write_throttles"] == 1
        assert configuration().tables[label] not in section.model_dump_json()


def test_ecr_resolves_release_approved_digests_instead_of_tags() -> None:
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
    section = source(ecr=ecr)._ecr_section()

    assert section.state == "healthy"
    assert ecr.image_ids == [
        {"imageDigest": configuration().runtime_image_digest},
        {"imageDigest": configuration().break_glass_image_digest},
    ]
    values = metrics(section)
    assert values["normal_image_present"] is True
    assert values["break_glass_image_present"] is True
    assert "sha256:" not in section.model_dump_json()
    assert configuration().runtime_image_digest[:19] not in section.model_dump_json()


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
            "StateValue": "ALARM",
        }
    ]


def test_cloudfront_derives_the_current_certificate_from_the_exact_distribution() -> None:
    certificate_arn = (
        f"arn:aws:acm:us-east-1:{AWS_ACCOUNT_ID}:certificate/12345678-1234-1234-1234-123456789abc"
    )

    class CloudFront:
        def get_distribution(self, **_kwargs: Any) -> dict[str, Any]:
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
                    "KeyAlgorithm": "EC_prime256v1",
                    "NotAfter": NOW + timedelta(days=300),
                }
            }

    acm = Acm()
    section = source(cloudfront=CloudFront(), acm=acm)._cloudfront_section(NOW)

    assert section.state == "healthy"
    assert acm.requested_arn == certificate_arn
    assert certificate_arn not in section.model_dump_json()


class Paginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        return self.pages


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
