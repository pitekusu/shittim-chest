"""Sanitized read-only AWS status source for Records ADMIN."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import unquote, urlsplit

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item

from shittim_records.admin_status import AdminStatusCollection
from shittim_records.contracts import (
    AdminHealthState,
    AdminStatusMetric,
    AdminStatusOverall,
    AdminStatusSection,
)

_TABLE_LABELS = ("debate", "archive", "statistics", "session")
_BUCKET_LABELS = ("web", "media", "release")
_MAX_PAGINATOR_PAGES = 20
_STATUS_COLLECTION_TIMEOUT_SECONDS = 20.0
_PRODUCTION_ALARM_PREFIX = "shittim-chest-production-"


@dataclass(frozen=True, slots=True)
class AwsAdminStatusConfiguration:
    aws_account_id: str
    cluster_name: str
    service_name: str
    ecr_repository_name: str
    runtime_image_digest: str
    break_glass_image_digest: str
    buckets: Mapping[str, str]
    tables: Mapping[str, str]
    functions: Mapping[str, str]
    distribution_id: str
    projector_dlq_url: str
    alarm_prefix: str = _PRODUCTION_ALARM_PREFIX

    def __post_init__(self) -> None:
        if set(self.buckets) != set(_BUCKET_LABELS):
            raise ValueError("ADMIN status bucket labels are invalid")
        if set(self.tables) != set(_TABLE_LABELS):
            raise ValueError("ADMIN status table labels are invalid")
        if not self.functions or any(
            not label or not name for label, name in self.functions.items()
        ):
            raise ValueError("ADMIN status Lambda allowlist is invalid")
        required = (
            self.cluster_name,
            self.service_name,
            self.ecr_repository_name,
            self.runtime_image_digest,
            self.break_glass_image_digest,
            self.distribution_id,
            self.projector_dlq_url,
            self.alarm_prefix,
        )
        if (
            any(not value for value in required)
            or len(self.aws_account_id) != 12
            or not self.aws_account_id.isdecimal()
            or any(
                not value.startswith("sha256:") or len(value) != 71
                for value in (self.runtime_image_digest, self.break_glass_image_digest)
            )
        ):
            raise ValueError("ADMIN status configuration is incomplete")


class AwsAdminStatusSource:
    """Collect only allowlisted, content-free AWS health metadata."""

    def __init__(
        self,
        *,
        configuration: AwsAdminStatusConfiguration,
        ecs: Any,
        ecr: Any,
        inspector: Any,
        s3: Any,
        dynamodb: Any,
        lambda_client: Any,
        cloudfront: Any,
        acm: Any,
        sqs: Any,
        cloudwatch: Any,
        cloudwatch_global: Any,
    ) -> None:
        self._config = configuration
        self._ecs = ecs
        self._ecr = ecr
        self._inspector = inspector
        self._s3 = s3
        self._dynamodb = dynamodb
        self._lambda = lambda_client
        self._cloudfront = cloudfront
        self._acm = acm
        self._sqs = sqs
        self._cloudwatch = cloudwatch
        self._cloudwatch_global = cloudwatch_global

    def collect(self, *, now: datetime) -> AdminStatusCollection:
        now = now.astimezone(UTC)
        collectors: tuple[tuple[str, Callable[[], AdminStatusSection]], ...] = (
            ("ecs", lambda: self._ecs_section(now)),
            ("ecr", self._ecr_section),
            ("inspector", self._inspector_section),
            ("s3", self._s3_section),
            ("dynamodb", lambda: self._dynamodb_section(now)),
            ("lambda", lambda: self._lambda_section(now)),
            ("cloudfront", lambda: self._cloudfront_section(now)),
            ("sqs", lambda: self._sqs_section(now)),
        )
        executor = ThreadPoolExecutor(
            max_workers=len(collectors) + 1,
            thread_name_prefix="admin-status",
        )
        alarm_future = executor.submit(self._alarm_counts)
        section_futures: dict[str, Future[AdminStatusSection]] = {
            service: executor.submit(collector) for service, collector in collectors
        }
        all_futures = [
            cast(Future[Any], future) for future in (alarm_future, *section_futures.values())
        ]
        done, pending = wait(
            all_futures,
            timeout=_STATUS_COLLECTION_TIMEOUT_SECONDS,
        )
        for future in pending:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)

        critical = 0
        warning = 0
        alarms_unknown = True
        if alarm_future in done:
            with suppress(Exception):
                critical, warning, alarms_unknown = alarm_future.result()
        sections: list[AdminStatusSection] = []
        for service, _collector in collectors:
            future = section_futures[service]
            section: AdminStatusSection | None = None
            if future in done:
                with suppress(Exception):
                    section = future.result()
            if section is not None:
                sections.append(section)
                continue
            sections.append(
                AdminStatusSection(
                    service=service,
                    state="unknown",
                    summary="状態を取得できません。",
                    metrics=(),
                )
            )
        partial = alarms_unknown or any(section.state == "unknown" for section in sections)
        state: AdminHealthState = (
            "critical"
            if critical or any(section.state == "critical" for section in sections)
            else "warning"
            if warning or partial or any(section.state == "warning" for section in sections)
            else "healthy"
        )
        return AdminStatusCollection(
            overall=AdminStatusOverall(
                state=state,
                critical_alarms=critical,
                warning_alarms=warning,
                partial=partial,
            ),
            sections=tuple(sections),
        )

    def _alarm_counts(self) -> tuple[int, int, bool]:
        try:
            paginator = self._cloudwatch.get_paginator("describe_alarms")
            critical = 0
            warning = 0
            for page in _bounded_pages(
                paginator.paginate(
                    AlarmNamePrefix=self._config.alarm_prefix,
                    StateValue="ALARM",
                )
            ):
                for alarm in page.get("MetricAlarms", []):
                    name = str(alarm.get("AlarmName", "")).casefold()
                    if "critical" in name:
                        critical += 1
                    else:
                        warning += 1
                for alarm in page.get("CompositeAlarms", []):
                    name = str(alarm.get("AlarmName", "")).casefold()
                    if "critical" in name:
                        critical += 1
                    else:
                        warning += 1
            return critical, warning, False
        except Exception:
            return 0, 0, True

    def _ecs_section(self, now: datetime) -> AdminStatusSection:
        response = self._ecs.describe_services(
            cluster=self._config.cluster_name,
            services=[self._config.service_name],
        )
        services = response.get("services", [])
        if len(services) != 1 or response.get("failures"):
            raise ValueError("ECS singleton service is unavailable")
        service = services[0]
        desired = _integer(service.get("desiredCount"))
        running = _integer(service.get("runningCount"))
        pending = _integer(service.get("pendingCount"))
        deployments = service.get("deployments", [])
        if not isinstance(deployments, list):
            raise ValueError("ECS deployments are invalid")
        controls = self._runtime_controls()
        idle = desired == running == pending == 0
        state: AdminHealthState = (
            "healthy" if idle or (desired == running and pending == 0) else "warning"
        )
        metrics = [
            _metric("desired_count", desired),
            _metric("running_count", running),
            _metric("pending_count", pending),
            _metric("deployment_count", len(deployments)),
            _metric("active_debates", controls.get("active_debates")),
            _metric("outbox_pending", controls.get("outbox_pending")),
            _metric("runtime_prompt_revision", controls.get("runtime_prompt_revision")),
            _metric("heartbeat_age_seconds", self._runtime_heartbeat(now)),
        ]
        return AdminStatusSection(
            service="ecs",
            state=state,
            summary="IDLE" if idle else "稼働中" if state == "healthy" else "確認が必要です。",
            metrics=tuple(metrics),
        )

    def _runtime_controls(self) -> dict[str, str | int | bool | None]:
        table = self._config.tables["debate"]
        keys = (
            ("runtime", {"PK": "CONTROL#RUNTIME", "SK": "STATE"}),
            ("debate", {"PK": "CONTROL#DEBATE", "SK": "ACTIVE_ATTEMPT_COUNT"}),
            ("outbox", {"PK": "CONTROL#OUTBOX", "SK": "ACTIVITY"}),
        )
        result: dict[str, str | int | bool | None] = {
            "active_debates": None,
            "outbox_pending": None,
            "runtime_prompt_revision": None,
        }
        for label, key in keys:
            response = self._dynamodb.get_item(
                TableName=table,
                Key=marshal_item(key),
                ConsistentRead=True,
            )
            raw = response.get("Item")
            if raw is None:
                continue
            item = unmarshal_item(raw)
            if label == "runtime":
                revision = item.get("runtime_prompt_revision")
                result["runtime_prompt_revision"] = revision if isinstance(revision, str) else None
            elif label == "debate":
                result["active_debates"] = _first_nonnegative_integer(
                    item,
                    "active_attempt_count",
                    "count",
                )
            else:
                pending = _first_nonnegative_integer(item, "pending_count", "pending")
                claimed = _first_nonnegative_integer(item, "claimed_count", "claimed")
                result["outbox_pending"] = (
                    None if pending is None or claimed is None else pending + claimed
                )
        return result

    def _runtime_heartbeat(self, now: datetime) -> str | None:
        return self._latest_metric(
            namespace="ShittimChest/Prod",
            metric_name="HeartbeatAgeSeconds",
            dimensions=[{"Name": "Service", "Value": "runtime"}],
            now=now,
            stat="Maximum",
        )

    def _ecr_section(self) -> AdminStatusSection:
        repository = self._ecr.describe_repositories(
            repositoryNames=[self._config.ecr_repository_name]
        ).get("repositories", [])
        if len(repository) != 1:
            raise ValueError("ECR repository is unavailable")
        details = self._ecr.describe_images(
            repositoryName=self._config.ecr_repository_name,
            imageIds=[
                {"imageDigest": self._config.runtime_image_digest},
                {"imageDigest": self._config.break_glass_image_digest},
            ],
        ).get("imageDetails", [])
        by_digest = {
            digest: item for item in details if isinstance((digest := item.get("imageDigest")), str)
        }
        metrics: list[AdminStatusMetric] = [
            _metric(
                "tag_mutability",
                repository[0].get("imageTagMutability", "unknown"),
            ),
            _metric(
                "encryption_type",
                repository[0].get("encryptionConfiguration", {}).get("encryptionType", "unknown"),
            ),
        ]
        missing = False
        for label, digest in (
            ("normal", self._config.runtime_image_digest),
            ("break_glass", self._config.break_glass_image_digest),
        ):
            image = by_digest.get(digest)
            if image is None:
                missing = True
                metrics.extend(
                    (
                        _metric(f"{label}_image_present", False),
                        _metric(f"{label}_pushed_at", None),
                    )
                )
                continue
            metrics.extend(
                (
                    _metric(f"{label}_image_present", True),
                    _metric(f"{label}_pushed_at", _timestamp(image.get("imagePushedAt"))),
                    _metric(
                        f"{label}_size_bytes", _optional_integer(image.get("imageSizeInBytes"))
                    ),
                )
            )
        return AdminStatusSection(
            service="ecr",
            state="warning" if missing else "healthy",
            summary="承認済みイメージを確認しました。"
            if not missing
            else "イメージ確認が必要です。",
            metrics=tuple(metrics),
        )

    def _inspector_section(self) -> AdminStatusSection:
        counts = {severity: 0 for severity in ("critical", "high", "medium", "low")}
        paginator = self._inspector.get_paginator("list_findings")
        for page in _bounded_pages(
            paginator.paginate(
                filterCriteria={
                    "ecrImageRepositoryName": [
                        {"comparison": "EQUALS", "value": self._config.ecr_repository_name}
                    ],
                    "findingStatus": [{"comparison": "EQUALS", "value": "ACTIVE"}],
                }
            )
        ):
            for finding in page.get("findings", []):
                severity = str(finding.get("severity", "")).casefold()
                if severity in counts:
                    counts[severity] += 1
        coverage_count = 0
        active_coverage = 0
        last_scanned_at: datetime | None = None
        coverage = self._inspector.get_paginator("list_coverage")
        for page in _bounded_pages(
            coverage.paginate(
                filterCriteria={
                    "ecrRepositoryName": [
                        {"comparison": "EQUALS", "value": self._config.ecr_repository_name}
                    ]
                }
            )
        ):
            for resource in page.get("coveredResources", []):
                coverage_count += 1
                if resource.get("scanStatus", {}).get("statusCode") == "ACTIVE":
                    active_coverage += 1
                scanned = resource.get("lastScannedAt")
                if isinstance(scanned, datetime) and (
                    last_scanned_at is None or scanned > last_scanned_at
                ):
                    last_scanned_at = scanned
        state: AdminHealthState = (
            "critical"
            if counts["critical"]
            else "warning"
            if counts["high"]
            or counts["medium"]
            or coverage_count == 0
            or active_coverage != coverage_count
            else "healthy"
        )
        metrics = [
            *(_metric(f"active_{key}", value) for key, value in counts.items()),
            _metric("coverage_count", coverage_count),
            _metric("coverage_active", active_coverage),
            _metric("last_scanned_at", _timestamp(last_scanned_at)),
        ]
        return AdminStatusSection(
            service="inspector",
            state=state,
            summary="検出結果とECR scan coverageを確認しました。",
            metrics=tuple(metrics),
        )

    def _s3_section(self) -> AdminStatusSection:
        metrics: list[AdminStatusMetric] = []
        warning = False
        for label in _BUCKET_LABELS:
            name = self._config.buckets[label]
            versioning = self._s3.get_bucket_versioning(Bucket=name).get("Status")
            encryption = (
                self._s3.get_bucket_encryption(Bucket=name)
                .get(
                    "ServerSideEncryptionConfiguration",
                    {},
                )
                .get("Rules", [])
            )
            block = self._s3.get_public_access_block(Bucket=name).get(
                "PublicAccessBlockConfiguration",
                {},
            )
            public_blocked = all(
                block.get(field) is True
                for field in (
                    "BlockPublicAcls",
                    "IgnorePublicAcls",
                    "BlockPublicPolicy",
                    "RestrictPublicBuckets",
                )
            )
            encrypted = bool(encryption)
            warning = warning or versioning != "Enabled" or not encrypted or not public_blocked
            metrics.extend(
                (
                    _metric(f"{label}_versioning", versioning or "Disabled"),
                    _metric(f"{label}_encrypted", encrypted),
                    _metric(f"{label}_public_access_blocked", public_blocked),
                )
            )
        return AdminStatusSection(
            service="s3",
            state="warning" if warning else "healthy",
            summary="Bucket保護設定を確認しました。",
            metrics=tuple(metrics),
        )

    def _dynamodb_section(self, now: datetime) -> AdminStatusSection:
        metrics: list[AdminStatusMetric] = []
        warning = False
        throttles = self._dynamodb_throttles(now)
        throttles_unknown = any(value is None for value in throttles.values())
        for label in _TABLE_LABELS:
            name = self._config.tables[label]
            table = self._dynamodb.describe_table(TableName=name).get("Table", {})
            backups = self._dynamodb.describe_continuous_backups(TableName=name).get(
                "ContinuousBackupsDescription",
                {},
            )
            ttl = self._dynamodb.describe_time_to_live(TableName=name).get(
                "TimeToLiveDescription",
                {},
            )
            status = table.get("TableStatus")
            pitr = backups.get("PointInTimeRecoveryDescription", {}).get(
                "PointInTimeRecoveryStatus"
            )
            protected = table.get("DeletionProtectionEnabled") is True
            ttl_status = ttl.get("TimeToLiveStatus")
            warning = warning or status != "ACTIVE" or pitr != "ENABLED" or not protected
            stream = table.get("StreamSpecification", {})
            stream_enabled = stream.get("StreamEnabled") is True
            if label == "debate":
                warning = warning or not stream_enabled
            metrics.extend(
                (
                    _metric(f"{label}_status", status or "unknown"),
                    _metric(f"{label}_pitr", pitr or "unknown"),
                    _metric(f"{label}_deletion_protection", protected),
                    _metric(f"{label}_ttl", ttl_status or "DISABLED"),
                    _metric(f"{label}_item_count", _optional_integer(table.get("ItemCount"))),
                    _metric(f"{label}_read_throttles", throttles[(label, "read")]),
                    _metric(f"{label}_write_throttles", throttles[(label, "write")]),
                )
            )
            if label == "debate":
                metrics.extend(
                    (
                        _metric("debate_stream_enabled", stream_enabled),
                        _metric(
                            "debate_stream_view_type",
                            stream.get("StreamViewType") if stream_enabled else None,
                        ),
                    )
                )
        return AdminStatusSection(
            service="dynamodb",
            state="unknown" if throttles_unknown else "warning" if warning else "healthy",
            summary="一部の指標を取得できませんでした。"
            if throttles_unknown
            else "Table状態と保護設定を確認しました。",
            metrics=tuple(metrics),
        )

    def _dynamodb_throttles(self, now: datetime) -> dict[tuple[str, str], int | None]:
        queries: list[dict[str, object]] = []
        identities: dict[str, tuple[str, str]] = {}
        counter = 0
        for label in _TABLE_LABELS:
            table_name = self._config.tables[label]
            for kind, metric_name in (
                ("read", "ReadThrottleEvents"),
                ("write", "WriteThrottleEvents"),
            ):
                identifier = f"d{counter}"
                counter += 1
                identities[identifier] = (label, kind)
                queries.append(
                    {
                        "Id": identifier,
                        "MetricStat": {
                            "Metric": {
                                "Namespace": "AWS/DynamoDB",
                                "MetricName": metric_name,
                                "Dimensions": [{"Name": "TableName", "Value": table_name}],
                            },
                            "Period": 3600,
                            "Stat": "Sum",
                        },
                        "ReturnData": True,
                    }
                )
        response = self._cloudwatch.get_metric_data(
            MetricDataQueries=queries,
            StartTime=now - timedelta(hours=1),
            EndTime=now,
            ScanBy="TimestampDescending",
        )
        values: dict[tuple[str, str], int | None] = {
            identity: None for identity in identities.values()
        }
        for result in response.get("MetricDataResults", []):
            identity = identities.get(result.get("Id"))
            samples = result.get("Values", [])
            if (
                identity is None
                or result.get("StatusCode") != "Complete"
                or not isinstance(samples, list)
                or not samples
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                    or not float(value).is_integer()
                    for value in samples
                )
            ):
                continue
            values[identity] = sum(int(value) for value in samples)
        return values

    def _lambda_section(self, now: datetime) -> AdminStatusSection:
        metrics: list[AdminStatusMetric] = []
        warning = False
        for label, name in sorted(self._config.functions.items()):
            config = self._lambda.get_function_configuration(FunctionName=name)
            state = config.get("State")
            update = config.get("LastUpdateStatus")
            warning = warning or state != "Active" or update not in {None, "Successful"}
            metrics.extend(
                (
                    _metric(f"{label}_state", state or "unknown"),
                    _metric(f"{label}_update", update or "unknown"),
                    _metric(f"{label}_runtime", config.get("Runtime") or "unknown"),
                    _metric(
                        f"{label}_architecture",
                        ",".join(config.get("Architectures", [])) or "unknown",
                    ),
                    _metric(
                        f"{label}_reserved_concurrency",
                        self._reserved_concurrency(name),
                    ),
                )
            )
        metrics.extend(self._lambda_metrics(now))
        return AdminStatusSection(
            service="lambda",
            state="warning" if warning else "healthy",
            summary="Lambda状態と直近1時間の指標を確認しました。",
            metrics=tuple(metrics),
        )

    def _reserved_concurrency(self, name: str) -> int | None:
        try:
            value = self._lambda.get_function_concurrency(FunctionName=name).get(
                "ReservedConcurrentExecutions"
            )
        except Exception:
            return None
        return _optional_integer(value)

    def _lambda_metrics(self, now: datetime) -> tuple[AdminStatusMetric, ...]:
        queries: list[dict[str, object]] = []
        identities: dict[str, tuple[str, str]] = {}
        counter = 0
        for label, name in sorted(self._config.functions.items()):
            for metric_name, stat in (
                ("Invocations", "Sum"),
                ("Errors", "Sum"),
                ("Throttles", "Sum"),
                ("Duration", "p95"),
            ):
                identifier = f"m{counter}"
                counter += 1
                identities[identifier] = (label, metric_name.casefold())
                queries.append(
                    {
                        "Id": identifier,
                        "MetricStat": {
                            "Metric": {
                                "Namespace": "AWS/Lambda",
                                "MetricName": metric_name,
                                "Dimensions": [{"Name": "FunctionName", "Value": name}],
                            },
                            "Period": 3600,
                            "Stat": stat,
                        },
                        "ReturnData": True,
                    }
                )
        if not queries:
            return ()
        response = self._cloudwatch.get_metric_data(
            MetricDataQueries=queries,
            StartTime=now - timedelta(hours=1),
            EndTime=now,
            ScanBy="TimestampDescending",
        )
        values: dict[tuple[str, str], int | str | None] = {}
        for result in response.get("MetricDataResults", []):
            identity = identities.get(result.get("Id"))
            if identity is None:
                continue
            samples = result.get("Values", [])
            value = samples[0] if isinstance(samples, list) and samples else None
            if value is not None and identity[1] != "duration":
                value = int(value)
            elif value is not None:
                value = f"{value:.3f}"
            values[identity] = value
        return tuple(
            _metric(f"{label}_hour_{metric}", values.get((label, metric)))
            for label in sorted(self._config.functions)
            for metric in ("invocations", "errors", "throttles", "duration")
        )

    def _cloudfront_section(self, now: datetime) -> AdminStatusSection:
        distribution = self._cloudfront.get_distribution(Id=self._config.distribution_id).get(
            "Distribution", {}
        )
        config = distribution.get("DistributionConfig", {})
        certificate_arn = _distribution_certificate_arn(
            config.get("ViewerCertificate"),
            account_id=self._config.aws_account_id,
        )
        invalidations = (
            self._cloudfront.list_invalidations(
                DistributionId=self._config.distribution_id,
                MaxItems="1",
            )
            .get("InvalidationList", {})
            .get("Items", [])
        )
        certificate = self._acm.describe_certificate(CertificateArn=certificate_arn).get(
            "Certificate", {}
        )
        enabled = config.get("Enabled") is True
        deployed = distribution.get("Status") == "Deployed"
        metrics = [
            _metric("enabled", enabled),
            _metric("deployment_status", distribution.get("Status") or "unknown"),
            _metric(
                "invalidation_status", invalidations[0].get("Status") if invalidations else None
            ),
            _metric(
                "tls_policy",
                config.get("ViewerCertificate", {}).get("MinimumProtocolVersion") or "unknown",
            ),
            _metric("certificate_key_algorithm", certificate.get("KeyAlgorithm") or "unknown"),
            _metric("certificate_expires_at", _timestamp(certificate.get("NotAfter"))),
        ]
        metrics.extend(self._cloudfront_metrics(now))
        return AdminStatusSection(
            service="cloudfront",
            state="healthy" if enabled and deployed else "warning",
            summary="Distributionと証明書を確認しました。",
            metrics=tuple(metrics),
        )

    def _cloudfront_metrics(self, now: datetime) -> tuple[AdminStatusMetric, ...]:
        queries = []
        identifiers: dict[str, str] = {}
        for index, metric in enumerate(("4xxErrorRate", "5xxErrorRate", "CacheHitRate")):
            identifier = f"c{index}"
            identifiers[identifier] = metric
            queries.append(
                {
                    "Id": identifier,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/CloudFront",
                            "MetricName": metric,
                            "Dimensions": [
                                {"Name": "DistributionId", "Value": self._config.distribution_id},
                                {"Name": "Region", "Value": "Global"},
                            ],
                        },
                        "Period": 3600,
                        "Stat": "Average",
                    },
                    "ReturnData": True,
                }
            )
        response = self._cloudwatch_global.get_metric_data(
            MetricDataQueries=queries,
            StartTime=now - timedelta(hours=1),
            EndTime=now,
            ScanBy="TimestampDescending",
        )
        values: dict[str, str | None] = {value: None for value in identifiers.values()}
        for result in response.get("MetricDataResults", []):
            metric = identifiers.get(result.get("Id"))
            samples = result.get("Values", [])
            if metric is not None and isinstance(samples, list) and samples:
                values[metric] = f"{samples[0]:.3f}"
        return (
            _metric("hour_4xx_rate", values["4xxErrorRate"]),
            _metric("hour_5xx_rate", values["5xxErrorRate"]),
            _metric("hour_cache_hit_rate", values["CacheHitRate"]),
        )

    def _sqs_section(self, now: datetime) -> AdminStatusSection:
        response = self._sqs.get_queue_attributes(
            QueueUrl=self._config.projector_dlq_url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
                "ApproximateNumberOfMessagesDelayed",
                "KmsMasterKeyId",
                "SqsManagedSseEnabled",
                "MessageRetentionPeriod",
            ],
        )
        attributes = response.get("Attributes", {})
        visible = _decimal_integer(attributes.get("ApproximateNumberOfMessages"))
        inflight = _decimal_integer(attributes.get("ApproximateNumberOfMessagesNotVisible"))
        delayed = _decimal_integer(attributes.get("ApproximateNumberOfMessagesDelayed"))
        encrypted = bool(attributes.get("KmsMasterKeyId")) or (
            attributes.get("SqsManagedSseEnabled") == "true"
        )
        state: AdminHealthState = "warning" if visible or inflight or delayed else "healthy"
        oldest_age = self._latest_metric(
            namespace="AWS/SQS",
            metric_name="ApproximateAgeOfOldestMessage",
            dimensions=[
                {
                    "Name": "QueueName",
                    "Value": _queue_name(self._config.projector_dlq_url),
                }
            ],
            now=now,
            stat="Maximum",
        )
        return AdminStatusSection(
            service="sqs",
            state=state,
            summary="DLQにメッセージがあります。" if state == "warning" else "DLQは空です。",
            metrics=(
                _metric("visible_messages", visible),
                _metric("inflight_messages", inflight),
                _metric("delayed_messages", delayed),
                _metric("oldest_message_age_seconds", oldest_age),
                _metric("encrypted", encrypted),
                _metric(
                    "retention_seconds",
                    _decimal_integer(attributes.get("MessageRetentionPeriod")),
                ),
            ),
        )

    def _latest_metric(
        self,
        *,
        namespace: str,
        metric_name: str,
        dimensions: list[dict[str, str]],
        now: datetime,
        stat: str,
    ) -> str | None:
        response = self._cloudwatch.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=now - timedelta(hours=1),
            EndTime=now,
            Period=60,
            Statistics=[stat],
        )
        datapoints = response.get("Datapoints", [])
        valid = [
            point
            for point in datapoints
            if isinstance(point.get("Timestamp"), datetime)
            and isinstance(point.get(stat), (int, float))
        ]
        if not valid:
            return None
        latest = max(valid, key=lambda point: point["Timestamp"])
        return f"{latest[stat]:.3f}"


def _metric(name: str, value: object) -> AdminStatusMetric:
    if value is not None and not isinstance(value, (str, int, bool)):
        value = str(value)
    return AdminStatusMetric(name=name, value=value)


def _bounded_pages(
    pages: Iterable[Mapping[str, Any]],
    *,
    maximum: int = _MAX_PAGINATOR_PAGES,
) -> Iterator[Mapping[str, Any]]:
    """Fail the owning section closed instead of following unbounded provider pages."""

    for index, page in enumerate(pages):
        if index >= maximum:
            raise ValueError("ADMIN status paginator exceeded its bounded page count")
        if not isinstance(page, Mapping):
            raise ValueError("ADMIN status paginator returned an invalid page")
        yield page


def _integer(value: object) -> int:
    parsed = _optional_integer(value)
    if parsed is None or parsed < 0:
        raise ValueError("provider count is invalid")
    return parsed


def _optional_integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _decimal_integer(value: object) -> int:
    if not isinstance(value, str) or not value.isdecimal():
        raise ValueError("provider decimal count is invalid")
    return int(value)


def _first_nonnegative_integer(item: Mapping[str, object], *names: str) -> int | None:
    for name in names:
        value = item.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _timestamp(value: object) -> str | None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC).isoformat()


def _queue_name(queue_url: str) -> str:
    parsed = urlsplit(queue_url)
    if parsed.scheme != "https" or parsed.query or parsed.fragment:
        raise ValueError("ADMIN status queue URL is invalid")
    name = unquote(parsed.path.rsplit("/", 1)[-1])
    if not name or "/" in name:
        raise ValueError("ADMIN status queue URL is invalid")
    return name


def _distribution_certificate_arn(value: object, *, account_id: str) -> str:
    if not isinstance(value, Mapping):
        raise ValueError("ADMIN status viewer certificate is invalid")
    arn = value.get("ACMCertificateArn")
    pattern = (
        rf"arn:aws:acm:us-east-1:{re.escape(account_id)}:"
        r"certificate/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    )
    if not isinstance(arn, str) or re.fullmatch(pattern, arn) is None:
        raise ValueError("ADMIN status viewer certificate is invalid")
    return arn
