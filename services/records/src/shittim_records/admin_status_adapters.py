"""Sanitized read-only AWS status source for Records ADMIN."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Literal, cast
from urllib.parse import unquote, urlsplit

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item

from shittim_records.admin_status import AdminStatusCollection
from shittim_records.contracts import (
    AdminActiveAlarm,
    AdminAlarmCode,
    AdminEcrDetails,
    AdminEcrImage,
    AdminEcsDetails,
    AdminHealthState,
    AdminInspectorAffectedPackage,
    AdminInspectorDetails,
    AdminInspectorFinding,
    AdminInspectorImage,
    AdminInspectorSeverityCounts,
    AdminServiceName,
    AdminStatusMetric,
    AdminStatusOverall,
    AdminStatusSection,
)
from shittim_records.inspector_translations import (
    InspectorDescription,
    InspectorJapaneseSummary,
    InspectorTranslationStore,
    InspectorTranslationUnavailable,
    NullInspectorTranslationStore,
    inspector_description,
)

_TABLE_LABELS = ("debate", "archive", "statistics", "session")
_BUCKET_LABELS = ("web", "media", "release")
_MAX_PAGINATOR_PAGES = 20
_STATUS_COLLECTION_TIMEOUT_SECONDS = 20.0
_AFFECTION_RANKING_FRESHNESS = timedelta(minutes=35)
_AFFECTION_RANKING_PAGE_SIZE = 50
_PRODUCTION_ALARM_PREFIX = "shittim-chest-production-"
_PROJECTOR_DLQ_RETENTION_SECONDS = 14 * 24 * 60 * 60
_GLOBAL_STACK_LABELS = frozenset({"records_edge", "cost_governance"})
ADMIN_STATUS_FUNCTION_NAMES: Mapping[str, str] = MappingProxyType(
    {
        "image_admission": "shittim-chest-production-image-admission",
        "discord_status": "shittim-chest-production-discord-status-publisher",
        "runtime_reconciler": "shittim-chest-production-runtime-reconciler",
        "discord_ingress": "shittim-chest-production-discord-ingress",
        "records_projector": "shittim-chest-production-records-projector",
        "records_backfill": "shittim-chest-production-records-backfill",
        "records_auth": "shittim-chest-production-records-auth",
        "records_ranking": "shittim-chest-production-records-ranking",
        "records_cost": "shittim-chest-production-records-cost",
        "records_inspector_translation": ("shittim-chest-production-records-inspector-translation"),
        "records_read": "shittim-chest-production-records-read",
        "records_admin_config": "shittim-chest-production-records-admin-config",
        "records_admin_status": "shittim-chest-production-records-admin-status",
    }
)
ADMIN_STATUS_STACK_NAMES: Mapping[str, str] = MappingProxyType(
    {
        "stateful": "ShittimChest-Prod-Stateful",
        "release_identity": "ShittimChest-Prod-ReleaseIdentity",
        "runtime": "ShittimChest-Prod-Runtime",
        "operations": "ShittimChest-Prod-Operations",
        "cost_governance": "ShittimChest-Prod-CostGovernance",
        "records_stateful": "ShittimChest-Prod-RecordsStateful",
        "records_application": "ShittimChest-Prod-RecordsApplication",
        "records_edge": "ShittimChest-Prod-RecordsEdge",
    }
)
ADMIN_STATUS_PARAMETER_NAMES: Mapping[str, str] = MappingProxyType(
    {
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
        "records_openai_inspector_translation_key": (
            "/shittim-chest/production/records/openai/inspector-translation-api-key"
        ),
        "records_admin_user_id": ("/shittim-chest/production/records/admin/discord-user-id"),
    }
)
ADMIN_STATUS_BUDGET_NAMES: Mapping[str, str] = MappingProxyType(
    {
        "project": "shittim-chest-production-project",
        "account": "shittim-chest-production-account",
    }
)
_STACK_LABELS = tuple(ADMIN_STATUS_STACK_NAMES)
_STATIC_PARAMETER_LABELS = tuple(ADMIN_STATUS_PARAMETER_NAMES)
_EVENT_RULE_DESCRIPTIONS = {
    "ranking": "Rebuild the Records ranking snapshots every 15 minutes",
    "aws_fx": "Collect Project-tagged AWS costs and USD/JPY rates daily at 12:17 JST",
    "openai": "Collect project-scoped OpenAI organization costs hourly at minute 37",
    "inspector_translation": "Translate unseen active Inspector descriptions hourly at minute 7",
    "abnormal_stop": "Notify only abnormal singleton runtime task stops",
}
_STABLE_STACK_STATUSES = frozenset({"CREATE_COMPLETE", "IMPORT_COMPLETE", "UPDATE_COMPLETE"})
_CRITICAL_STACK_STATUS_PARTS = ("FAILED", "ROLLBACK_IN_PROGRESS", "DELETE_")
_UNKNOWN_STACK_DRIFT_STATUSES = frozenset({"NOT_CHECKED", "UNKNOWN", "CHECK_IN_PROGRESS"})
_CHECKPOINT_SOURCES = ("AWS", "OPENAI", "FRANKFURTER")
_INSPECTOR_SEVERITIES = ("critical", "high", "medium", "low", "untriaged")
_ALARM_PRESENTATIONS: Mapping[str, tuple[Literal["critical", "warning"], AdminServiceName]] = (
    MappingProxyType(
        {
            "bot-not-ready": ("critical", "ecs"),
            "heartbeat-stale": ("critical", "ecs"),
            "ingress-runtime-mismatch": ("critical", "ecs"),
            "idle-still-running": ("critical", "ecs"),
            "reconciler-failure": ("critical", "lambda"),
            "status-publish-failure": ("warning", "lambda"),
            "outbox-backlog": ("warning", "ecs"),
            "dynamo-db-throttle": ("warning", "dynamodb"),
        }
    )
)
_KNOWN_ALARM_GATES = frozenset({"runtime-active"})
_NO_ALARM_GATES: frozenset[str] = frozenset()
_ALARM_REQUIRED_GATES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "bot-not-ready": frozenset({"runtime-active"}),
        "heartbeat-stale": frozenset({"runtime-active"}),
    }
)


@dataclass(frozen=True, slots=True)
class _TaggedEcrImage:
    digest: str
    public: AdminEcrImage


@dataclass(frozen=True, slots=True)
class _InspectorFindingCandidate:
    source: InspectorDescription
    public: AdminInspectorFinding


@dataclass(frozen=True, slots=True)
class AwsAdminStatusConfiguration:
    aws_account_id: str
    cluster_name: str
    service_name: str
    container_name: str
    ecr_repository_name: str
    runtime_stack_name: str
    buckets: Mapping[str, str]
    tables: Mapping[str, str]
    functions: Mapping[str, str]
    records_public_hostname: str
    projector_dlq_url: str
    stacks: Mapping[str, str]
    static_parameters: Mapping[str, str]
    runtime_scheduler_name: str
    sns_topic_arn: str
    signing_profile_name: str
    budgets: Mapping[str, str]
    anomaly_subscription_name: str
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
        if set(self.stacks) != set(_STACK_LABELS) or any(
            not value for value in self.stacks.values()
        ):
            raise ValueError("ADMIN status stack allowlist is invalid")
        if set(self.static_parameters) != set(_STATIC_PARAMETER_LABELS) or any(
            not value.startswith("/shittim-chest/production/")
            for value in self.static_parameters.values()
        ):
            raise ValueError("ADMIN status parameter allowlist is invalid")
        if set(self.budgets) != {"project", "account"} or any(
            not value for value in self.budgets.values()
        ):
            raise ValueError("ADMIN status budget allowlist is invalid")
        required = (
            self.cluster_name,
            self.service_name,
            self.container_name,
            self.ecr_repository_name,
            self.runtime_stack_name,
            self.records_public_hostname,
            self.projector_dlq_url,
            self.runtime_scheduler_name,
            self.sns_topic_arn,
            self.signing_profile_name,
            self.anomaly_subscription_name,
            self.alarm_prefix,
        )
        if (
            any(not value for value in required)
            or len(self.aws_account_id) != 12
            or not self.aws_account_id.isdecimal()
            or re.fullmatch(
                r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
                self.records_public_hostname,
            )
            is None
        ):
            raise ValueError("ADMIN status configuration is incomplete")


class AwsAdminStatusSource:
    """Collect only allowlisted, content-free AWS health metadata."""

    def __init__(
        self,
        *,
        configuration: AwsAdminStatusConfiguration,
        ecs: Any,
        cloudformation: Any,
        ecr: Any,
        inspector: Any,
        s3: Any,
        dynamodb: Any,
        lambda_client: Any,
        cloudfront: Any,
        acm: Any,
        sqs: Any,
        apigateway: Any,
        events: Any,
        scheduler: Any,
        sns: Any,
        ssm: Any,
        budgets: Any,
        cost_explorer: Any,
        signer: Any,
        cloudwatch: Any,
        cloudwatch_global: Any,
        cloudformation_global: Any,
        translations: InspectorTranslationStore | None = None,
    ) -> None:
        self._config = configuration
        self._ecs = ecs
        self._cloudformation = cloudformation
        self._ecr = ecr
        self._inspector = inspector
        self._s3 = s3
        self._dynamodb = dynamodb
        self._lambda = lambda_client
        self._cloudfront = cloudfront
        self._acm = acm
        self._sqs = sqs
        self._apigateway = apigateway
        self._events = events
        self._scheduler = scheduler
        self._sns = sns
        self._ssm = ssm
        self._budgets = budgets
        self._cost_explorer = cost_explorer
        self._signer = signer
        self._cloudwatch = cloudwatch
        self._cloudwatch_global = cloudwatch_global
        self._cloudformation_global = cloudformation_global
        self._translations = translations or NullInspectorTranslationStore()

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
            ("apigateway", lambda: self._apigateway_section(now)),
            ("eventbridge", lambda: self._eventbridge_section(now)),
            ("cloudformation", self._cloudformation_section),
            ("sns", lambda: self._sns_section(now)),
            ("ssm", self._ssm_section),
            ("cost_governance", self._cost_governance_section),
            ("signer", self._signer_section),
            ("external", lambda: self._external_section(now)),
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
        active_alarms: tuple[AdminActiveAlarm, ...] = ()
        if alarm_future in done:
            with suppress(Exception):
                critical, warning, active_alarms, alarms_unknown = alarm_future.result()
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
                active_alarms=active_alarms,
            ),
            sections=tuple(sections),
        )

    def _alarm_counts(self) -> tuple[int, int, tuple[AdminActiveAlarm, ...], bool]:
        try:
            paginator = self._cloudwatch.get_paginator("describe_alarms")
            critical = 0
            warning = 0
            unknown = False
            alarm_candidates: list[AdminActiveAlarm] = []
            active_alarm_gates: set[str] = set()
            normalized_prefix = self._config.alarm_prefix.casefold()
            for page in _bounded_pages(
                paginator.paginate(
                    AlarmNamePrefix=self._config.alarm_prefix,
                    AlarmTypes=["CompositeAlarm", "MetricAlarm"],
                    StateValue="ALARM",
                )
            ):
                for alarm in page.get("CompositeAlarms", []):
                    name = str(alarm.get("AlarmName", "")).casefold()
                    if "critical" in name:
                        critical += 1
                    elif "warning" in name:
                        warning += 1
                    else:
                        unknown = True
                for alarm in page.get("MetricAlarms", []):
                    raw_name = str(alarm.get("AlarmName", "")).casefold()
                    if not raw_name.startswith(normalized_prefix):
                        unknown = True
                        continue
                    code = raw_name.removeprefix(normalized_prefix)
                    if code in _KNOWN_ALARM_GATES:
                        active_alarm_gates.add(code)
                        continue
                    presentation = _ALARM_PRESENTATIONS.get(code)
                    if presentation is None:
                        unknown = True
                        continue
                    severity, service = presentation
                    alarm_candidates.append(
                        AdminActiveAlarm(
                            code=cast(AdminAlarmCode, code),
                            severity=severity,
                            service=service,
                        )
                    )
            active_alarms = [
                alarm
                for alarm in alarm_candidates
                if (
                    (alarm.severity == "critical" and critical > 0)
                    or (alarm.severity == "warning" and warning > 0)
                )
                and _ALARM_REQUIRED_GATES.get(alarm.code, _NO_ALARM_GATES).issubset(
                    active_alarm_gates
                )
            ]
            active_alarms.sort(key=lambda alarm: (alarm.severity, alarm.code))
            if critical and not any(alarm.severity == "critical" for alarm in active_alarms):
                unknown = True
            if warning and not any(alarm.severity == "warning" for alarm in active_alarms):
                unknown = True
            return critical, warning, tuple(active_alarms), unknown
        except Exception:
            return 0, 0, (), True

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
        primary_deployment = _primary_ecs_deployment(deployments)
        deployment_configuration = service.get("deploymentConfiguration", {})
        if not isinstance(deployment_configuration, Mapping):
            raise ValueError("ECS deployment configuration is invalid")
        circuit_breaker = deployment_configuration.get("deploymentCircuitBreaker", {})
        if not isinstance(circuit_breaker, Mapping):
            raise ValueError("ECS deployment circuit breaker is invalid")
        service_status = _known_value(service.get("status"), {"ACTIVE", "DRAINING", "INACTIVE"})
        rollout_state = _known_value(
            primary_deployment.get("rolloutState"),
            {"COMPLETED", "FAILED", "IN_PROGRESS"},
        )
        failed_tasks = _optional_nonnegative_integer(primary_deployment.get("failedTasks"))
        task_definition_arn = _ecs_task_definition_arn(
            primary_deployment.get("taskDefinition", service.get("taskDefinition")),
            account_id=self._config.aws_account_id,
        )
        task_definition_revision = _ecs_task_definition_revision(task_definition_arn)
        next_task_image_tags = self._next_task_image_tags(task_definition_arn)
        launch_mode = _ecs_launch_mode(service, primary_deployment)
        platform_version = _ecs_platform_version(
            primary_deployment.get("platformVersion", service.get("platformVersion"))
        )
        controls = self._runtime_controls()
        heartbeat_age = self._runtime_heartbeat(now)
        idle = desired == running == pending == 0
        telemetry_complete = all(
            value is not None
            for value in (
                controls.get("active_debates"),
                controls.get("outbox_pending"),
                heartbeat_age,
            )
        )
        state: AdminHealthState
        deployment_needs_attention = (
            service_status in {"DRAINING", "INACTIVE"}
            or rollout_state == "FAILED"
            or (failed_tasks is not None and failed_tasks > 0 and rollout_state != "COMPLETED")
        )
        if deployment_needs_attention:
            state = "warning"
        elif idle:
            state = "healthy"
        elif desired != running or pending != 0:
            state = "warning"
        elif not telemetry_complete:
            state = "unknown"
        else:
            state = "healthy"
        metrics = [
            _metric("desired_count", desired),
            _metric("running_count", running),
            _metric("pending_count", pending),
            _metric("deployment_count", len(deployments)),
            _metric("service_status", service_status),
            _metric(
                "scheduling_strategy",
                _known_value(service.get("schedulingStrategy"), {"DAEMON", "REPLICA"}),
            ),
            _metric("launch_mode", launch_mode),
            _metric("platform_version", platform_version),
            _metric("task_definition_revision", task_definition_revision),
            _metric("rollout_state", rollout_state),
            _metric("failed_task_count", failed_tasks),
            _metric("deployment_updated_at", _timestamp(primary_deployment.get("updatedAt"))),
            _metric(
                "deployment_controller",
                _known_value(
                    _mapping_value(service.get("deploymentController"), "type"),
                    {"CODE_DEPLOY", "ECS", "EXTERNAL"},
                ),
            ),
            _metric(
                "minimum_healthy_percent",
                _optional_nonnegative_integer(
                    deployment_configuration.get("minimumHealthyPercent")
                ),
            ),
            _metric(
                "maximum_percent",
                _optional_nonnegative_integer(deployment_configuration.get("maximumPercent")),
            ),
            _metric("circuit_breaker_enabled", _optional_boolean(circuit_breaker.get("enable"))),
            _metric("circuit_breaker_rollback", _optional_boolean(circuit_breaker.get("rollback"))),
            _metric(
                "execute_command_enabled",
                _optional_boolean(service.get("enableExecuteCommand")),
            ),
            _metric("active_debates", controls.get("active_debates")),
            _metric("outbox_pending", controls.get("outbox_pending")),
            _metric("runtime_prompt_revision", controls.get("runtime_prompt_revision")),
            _metric("heartbeat_age_seconds", heartbeat_age),
        ]
        return AdminStatusSection(
            service="ecs",
            state=state,
            summary=(
                "デプロイ状態の確認が必要です。"
                if deployment_needs_attention
                else "IDLE"
                if idle
                else "稼働中"
                if state == "healthy"
                else "状態を取得できません。"
                if state == "unknown"
                else "確認が必要です。"
            ),
            metrics=tuple(metrics),
            details=AdminEcsDetails(
                kind="ecs",
                next_task_image_tags=next_task_image_tags,
            ),
        )

    def _next_task_image_tags(self, task_definition_arn: str) -> tuple[str, ...]:
        response = self._ecs.describe_task_definition(taskDefinition=task_definition_arn)
        task_definition = response.get("taskDefinition")
        if not isinstance(task_definition, Mapping):
            raise ValueError("ECS task definition is unavailable")
        if task_definition.get("taskDefinitionArn") != task_definition_arn:
            raise ValueError("ECS task definition response is invalid")
        containers = task_definition.get("containerDefinitions", [])
        if not isinstance(containers, list) or any(
            not isinstance(container, Mapping) for container in containers
        ):
            raise ValueError("ECS container definitions are invalid")
        application = [
            cast(Mapping[str, Any], container)
            for container in containers
            if container.get("name") == self._config.container_name
        ]
        if len(application) != 1:
            raise ValueError("ECS application container is unavailable")
        digest = _runtime_ecr_image_digest(
            application[0].get("image"),
            task_definition_arn=task_definition_arn,
            account_id=self._config.aws_account_id,
            repository_name=self._config.ecr_repository_name,
        )
        image_response = self._ecr.describe_images(
            repositoryName=self._config.ecr_repository_name,
            imageIds=[{"imageDigest": digest}],
        )
        details = image_response.get("imageDetails", [])
        if image_response.get("failures") or not isinstance(details, list) or len(details) != 1:
            raise ValueError("ECR next task image is unavailable")
        detail = details[0]
        if not isinstance(detail, Mapping) or _image_digest(detail.get("imageDigest")) != digest:
            raise ValueError("ECR next task image response is invalid")
        tags = _image_tags(detail.get("imageTags"))
        if not tags:
            raise ValueError("ECR next task image has no tag")
        return tags

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
        runtime_image_digest = self._runtime_image_digest()
        repositories = self._ecr.describe_repositories(
            repositoryNames=[self._config.ecr_repository_name]
        ).get("repositories", [])
        if len(repositories) != 1 or not isinstance(repositories[0], Mapping):
            raise ValueError("ECR repository is unavailable")
        repository = repositories[0]
        image_details = self._ecr_image_details()
        by_digest: dict[str, Mapping[str, Any]] = {}
        total_size = 0
        total_size_complete = True
        pushed_at_values: list[datetime] = []
        for item in image_details:
            digest = _image_digest(item.get("imageDigest"))
            if digest in by_digest:
                raise ValueError("ECR image inventory contains duplicate digests")
            by_digest[digest] = item
            size = _optional_nonnegative_integer(item.get("imageSizeInBytes"))
            if size is None:
                total_size_complete = False
            else:
                total_size += size
            pushed_at = item.get("imagePushedAt")
            if pushed_at is not None:
                timestamp = _aware_datetime(pushed_at)
                pushed_at_values.append(timestamp)
        scanning_configuration = repository.get("imageScanningConfiguration", {})
        if not isinstance(scanning_configuration, Mapping):
            raise ValueError("ECR scanning configuration is invalid")
        images = self._tagged_ecr_images(image_details)
        metrics: list[AdminStatusMetric] = [
            _metric(
                "tag_mutability",
                _known_value(repository.get("imageTagMutability"), {"IMMUTABLE", "MUTABLE"}),
            ),
            _metric(
                "encryption_type",
                _known_value(
                    _mapping_value(repository.get("encryptionConfiguration"), "encryptionType"),
                    {"AES256", "KMS", "KMS_DSSE"},
                ),
            ),
            _metric("repository_created_at", _timestamp(repository.get("createdAt"))),
            _metric(
                "scan_on_push",
                _optional_boolean(scanning_configuration.get("scanOnPush")),
            ),
            _metric("repository_image_count", len(image_details)),
            _metric("repository_tagged_image_count", len(images)),
            _metric("repository_untagged_image_count", len(image_details) - len(images)),
            _metric("repository_total_size_bytes", total_size if total_size_complete else None),
            _metric(
                "repository_latest_pushed_at",
                _timestamp(max(pushed_at_values)) if pushed_at_values else None,
            ),
        ]
        runtime_image = by_digest.get(runtime_image_digest)
        missing = runtime_image is None or not _image_tags(runtime_image.get("imageTags"))
        return AdminStatusSection(
            service="ecr",
            state="warning"
            if missing or repository.get("imageTagMutability") != "IMMUTABLE"
            else "healthy",
            summary="タグ付きイメージと保管庫の保護を確認しました。"
            if not missing and repository.get("imageTagMutability") == "IMMUTABLE"
            else "イメージまたは保管庫の保護の確認が必要です。",
            metrics=tuple(metrics),
            details=AdminEcrDetails(
                kind="ecr",
                images=tuple(image.public for image in images),
            ),
        )

    def _ecr_image_details(self) -> tuple[Mapping[str, Any], ...]:
        paginator = self._ecr.get_paginator("describe_images")
        image_details: list[Mapping[str, Any]] = []
        for page in _bounded_pages(
            paginator.paginate(repositoryName=self._config.ecr_repository_name)
        ):
            page_details = page.get("imageDetails", [])
            if not isinstance(page_details, list) or any(
                not isinstance(item, Mapping) for item in page_details
            ):
                raise ValueError("ECR image inventory is invalid")
            image_details.extend(cast(list[Mapping[str, Any]], page_details))
        return tuple(image_details)

    @staticmethod
    def _tagged_ecr_images(
        image_details: Iterable[Mapping[str, Any]],
    ) -> tuple[_TaggedEcrImage, ...]:
        images: list[_TaggedEcrImage] = []
        for item in image_details:
            tags = _image_tags(item.get("imageTags"))
            if not tags:
                continue
            images.append(
                _TaggedEcrImage(
                    digest=_image_digest(item.get("imageDigest")),
                    public=AdminEcrImage(
                        tags=tags,
                        media_type=_ecr_media_type(item),
                        size_bytes=_optional_nonnegative_integer(item.get("imageSizeInBytes")),
                        pushed_at=_optional_aware_datetime(item.get("imagePushedAt")),
                        last_pulled_at=_optional_aware_datetime(item.get("lastRecordedPullTime")),
                    ),
                )
            )
        return tuple(
            sorted(
                images,
                key=lambda image: (
                    image.public.pushed_at or datetime.min.replace(tzinfo=UTC),
                    image.public.tags,
                ),
                reverse=True,
            )
        )

    def _runtime_image_digest(self) -> str:
        response = self._cloudformation.describe_stacks(StackName=self._config.runtime_stack_name)
        stacks = response.get("Stacks", [])
        if not isinstance(stacks, list) or len(stacks) != 1:
            raise ValueError("Runtime stack is unavailable")
        parameters = stacks[0].get("Parameters", [])
        if not isinstance(parameters, list):
            raise ValueError("Runtime stack parameters are invalid")
        values = {
            item.get("ParameterKey"): item.get("ParameterValue")
            for item in parameters
            if isinstance(item, Mapping)
        }
        return _image_digest(values.get("RuntimeImageDigest"))

    def _inspector_section(self) -> AdminStatusSection:
        images = self._tagged_ecr_images(self._ecr_image_details())
        digests = {image.digest for image in images}
        if len(digests) != len(images):
            raise ValueError("tagged ECR image inventory contains duplicate digests")

        aggregated: dict[str, AdminInspectorSeverityCounts] = {}
        aggregation = self._inspector.get_paginator("list_finding_aggregations")
        for page in _bounded_pages(
            aggregation.paginate(
                aggregationType="AWS_ECR_CONTAINER",
                aggregationRequest={
                    "awsEcrContainerAggregation": {
                        "repositories": [
                            {
                                "comparison": "EQUALS",
                                "value": self._config.ecr_repository_name,
                            }
                        ]
                    }
                },
            )
        ):
            if page.get("aggregationType") not in (
                None,
                "AWS_ECR_CONTAINER",
                "AWS_CONTAINER",
            ):
                raise ValueError("Inspector aggregation type is invalid")
            responses = page.get("responses", [])
            if not isinstance(responses, list):
                raise ValueError("Inspector aggregation response is invalid")
            for response in responses:
                if not isinstance(response, Mapping):
                    raise ValueError("Inspector aggregation item is invalid")
                item = response.get("awsEcrContainerAggregation")
                if not isinstance(item, Mapping):
                    raise ValueError("Inspector ECR aggregation item is invalid")
                digest = _image_digest(item.get("imageSha"))
                if digest not in digests:
                    continue
                if digest in aggregated:
                    raise ValueError("Inspector aggregation contains a duplicate image")
                severity_counts = item.get("severityCounts", {})
                if not isinstance(severity_counts, Mapping):
                    raise ValueError("Inspector severity counts are invalid")
                aggregated[digest] = AdminInspectorSeverityCounts(
                    total=_nonnegative_count(severity_counts.get("all")),
                    critical=_nonnegative_count(severity_counts.get("critical")),
                    high=_nonnegative_count(severity_counts.get("high")),
                    medium=_nonnegative_count(severity_counts.get("medium")),
                    low=0,
                    untriaged=0,
                )

        supplemental_counts = {digest: {"low": 0, "untriaged": 0} for digest in digests}
        findings: dict[str, list[_InspectorFindingCandidate]] = {digest: [] for digest in digests}
        paginator = self._inspector.get_paginator("list_findings")
        for page in _bounded_pages(
            paginator.paginate(
                filterCriteria={
                    "ecrImageRepositoryName": [
                        {"comparison": "EQUALS", "value": self._config.ecr_repository_name}
                    ],
                    "findingStatus": [{"comparison": "EQUALS", "value": "ACTIVE"}],
                    "resourceType": [{"comparison": "EQUALS", "value": "AWS_ECR_CONTAINER_IMAGE"}],
                }
            )
        ):
            page_findings = page.get("findings", [])
            if not isinstance(page_findings, list):
                raise ValueError("Inspector findings response is invalid")
            for raw_finding in page_findings:
                if not isinstance(raw_finding, Mapping):
                    raise ValueError("Inspector finding is invalid")
                digest = _finding_image_digest(raw_finding)
                if digest not in digests:
                    continue
                severity = str(raw_finding.get("severity", "")).casefold()
                if severity in {"low", "untriaged"}:
                    supplemental_counts[digest][severity] += 1
                if (
                    severity in {"critical", "high"}
                    and raw_finding.get("packageVulnerabilityDetails") is not None
                ):
                    findings[digest].append(_public_inspector_finding(raw_finding, severity))

        for digest, candidates in findings.items():
            counts = aggregated.get(digest)
            critical_limit = counts.critical if counts is not None else 0
            high_limit = counts.high if counts is not None else 0
            critical_details = sum(
                candidate.public.severity == "critical" for candidate in candidates
            )
            high_details = sum(candidate.public.severity == "high" for candidate in candidates)
            if critical_details > critical_limit or high_details > high_limit:
                raise ValueError("Inspector finding details exceed severity aggregates")

        translation_keys = tuple(
            sorted({candidate.source.key for values in findings.values() for candidate in values})
        )
        translation_cache_available = True
        try:
            translations = self._translations.load(translation_keys)
        except InspectorTranslationUnavailable:
            translations = {}
            translation_cache_available = False
        if not set(translations) <= set(translation_keys):
            raise ValueError("Inspector translation cache returned an unexpected item")
        translation_last_translated_at = (
            max(summary.translated_at for summary in translations.values())
            if translations
            else None
        )

        coverage_by_digest: dict[str, tuple[str, datetime | None]] = {}
        tag_pairs = [(tag, image.digest) for image in images for tag in image.public.tags]
        tags_to_digest = dict(tag_pairs)
        if len(tags_to_digest) != len(tag_pairs):
            raise ValueError("tagged ECR image inventory contains a duplicate tag")
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
            resources = page.get("coveredResources", [])
            if not isinstance(resources, list):
                raise ValueError("Inspector coverage response is invalid")
            for resource in resources:
                if not isinstance(resource, Mapping):
                    raise ValueError("Inspector coverage item is invalid")
                digest = _coverage_image_digest(resource, tags_to_digest=tags_to_digest)
                if digest not in digests:
                    continue
                if digest in coverage_by_digest:
                    raise ValueError("Inspector coverage contains a duplicate image")
                raw_status = _mapping_value(resource.get("scanStatus"), "statusCode")
                scan_status = _known_value(raw_status, {"ACTIVE", "INACTIVE"}) or "UNKNOWN"
                coverage_by_digest[digest] = (
                    scan_status,
                    _optional_aware_datetime(resource.get("lastScannedAt")),
                )

        image_results: list[AdminInspectorImage] = []
        for image in images:
            counts = aggregated.get(
                image.digest,
                AdminInspectorSeverityCounts(
                    total=0,
                    critical=0,
                    high=0,
                    medium=0,
                    low=0,
                    untriaged=0,
                ),
            )
            low = supplemental_counts[image.digest]["low"]
            untriaged = supplemental_counts[image.digest]["untriaged"]
            if low + untriaged > counts.total - counts.critical - counts.high - counts.medium:
                raise ValueError("Inspector severity totals are inconsistent")
            scan_status, last_scanned_at = coverage_by_digest.get(
                image.digest,
                ("UNKNOWN", None),
            )
            image_results.append(
                AdminInspectorImage(
                    tags=image.public.tags,
                    scan_status=cast(Any, scan_status),
                    last_scanned_at=last_scanned_at,
                    counts=counts.model_copy(update={"low": low, "untriaged": untriaged}),
                    findings=tuple(
                        sorted(
                            (
                                _resolved_inspector_finding(candidate, translations)
                                for candidate in findings[image.digest]
                            ),
                            key=_inspector_finding_sort_key,
                        )
                    ),
                )
            )

        total_counts = {
            severity: sum(getattr(image.counts, severity) for image in image_results)
            for severity in _INSPECTOR_SEVERITIES
        }
        last_scanned_values = [
            image.last_scanned_at for image in image_results if image.last_scanned_at is not None
        ]
        active_coverage = sum(image.scan_status == "ACTIVE" for image in image_results)
        state: AdminHealthState = (
            "critical"
            if total_counts["critical"]
            else "warning"
            if any(total_counts[severity] for severity in ("high", "medium", "low", "untriaged"))
            or not image_results
            or active_coverage != len(image_results)
            else "healthy"
        )
        metrics = [
            *(_metric(f"active_{key}", value) for key, value in total_counts.items()),
            _metric("coverage_count", len(image_results)),
            _metric("coverage_active", active_coverage),
            _metric(
                "last_scanned_at",
                _timestamp(max(last_scanned_values)) if last_scanned_values else None,
            ),
            _metric(
                "translation_cache_count",
                len(translations) if translation_cache_available else None,
            ),
            _metric(
                "translation_missing_count",
                len(translation_keys) - len(translations) if translation_cache_available else None,
            ),
            _metric(
                "translation_last_translated_at",
                _timestamp(translation_last_translated_at)
                if translation_last_translated_at is not None
                else None,
            ),
        ]
        return AdminStatusSection(
            service="inspector",
            state=state,
            summary="タグ付きコンテナイメージ別の検出結果を確認しました。",
            metrics=tuple(metrics),
            details=AdminInspectorDetails(kind="inspector", images=tuple(image_results)),
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
        table_warning = False
        throttles = self._dynamodb_throttles(now)
        throttles_unknown = any(value is None for value in throttles.values())
        throttled = any(value is not None and value > 0 for value in throttles.values())
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
            table_warning = (
                table_warning or status != "ACTIVE" or pitr != "ENABLED" or not protected
            )
            if label == "session":
                table_warning = table_warning or ttl_status != "ENABLED"
            stream = table.get("StreamSpecification", {})
            stream_enabled = stream.get("StreamEnabled") is True
            stream_view_type = stream.get("StreamViewType") if stream_enabled else None
            if label == "debate":
                table_warning = (
                    table_warning or not stream_enabled or stream_view_type != "NEW_IMAGE"
                )
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
                        _metric("debate_stream_view_type", stream_view_type),
                    )
                )
        affection_metrics, affection_state = self._affection_dynamodb_metrics(now)
        metrics.extend(affection_metrics)
        affection_warning = affection_state == "warning"
        warning = table_warning or affection_warning
        return AdminStatusSection(
            service="dynamodb",
            state="unknown"
            if throttles_unknown
            else "warning"
            if warning or throttled
            else "healthy",
            summary="一部の指標を取得できませんでした。"
            if throttles_unknown
            else "直近1時間にDynamoDB throttleを検出しました。"
            if throttled
            else "Table状態、保護設定、または親愛度データを確認してください。"
            if table_warning and affection_warning
            else "Table状態または保護設定を確認してください。"
            if table_warning
            else "親愛度データの初期化またはランキング更新を確認してください。"
            if affection_warning
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
        provider_metrics, provider_metrics_complete = self._lambda_metrics(now)
        metrics.extend(provider_metrics)
        provider_warning = any(
            metric.name.endswith(("_hour_errors", "_hour_throttles"))
            and isinstance(metric.value, int)
            and not isinstance(metric.value, bool)
            and metric.value > 0
            for metric in provider_metrics
        )
        return AdminStatusSection(
            service="lambda",
            state="unknown"
            if not provider_metrics_complete
            else "warning"
            if warning or provider_warning
            else "healthy",
            summary="一部の指標を取得できませんでした。"
            if not provider_metrics_complete
            else "Lambda状態と直近1時間の指標を確認しました。",
            metrics=tuple(metrics),
        )

    def _reserved_concurrency(self, name: str) -> int | None:
        value = self._lambda.get_function_concurrency(FunctionName=name).get(
            "ReservedConcurrentExecutions"
        )
        return _optional_integer(value)

    def _lambda_metrics(self, now: datetime) -> tuple[tuple[AdminStatusMetric, ...], bool]:
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
            return (), False
        response = self._cloudwatch.get_metric_data(
            MetricDataQueries=queries,
            StartTime=now - timedelta(hours=1),
            EndTime=now,
            ScanBy="TimestampDescending",
        )
        values: dict[tuple[str, str], int | str | None] = {}
        results = response.get("MetricDataResults")
        provider_complete = (
            isinstance(results, list)
            and len(results) == len(identities)
            and not response.get("NextToken")
        )
        seen_ids: set[str] = set()
        for result in results if isinstance(results, list) else []:
            if not isinstance(result, Mapping):
                provider_complete = False
                continue
            result_id = result.get("Id")
            identity = identities.get(result_id)
            samples = result.get("Values", [])
            if (
                identity is None
                or not isinstance(result_id, str)
                or result_id in seen_ids
                or result.get("StatusCode") != "Complete"
                or not isinstance(samples, list)
            ):
                provider_complete = False
                continue
            seen_ids.add(result_id)
            if not samples:
                values[identity] = None if identity[1] == "duration" else 0
                continue
            value = samples[0]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                or (identity[1] != "duration" and not float(value).is_integer())
            ):
                provider_complete = False
                continue
            value = int(value) if identity[1] != "duration" else f"{value:.3f}"
            values[identity] = value
        for label in sorted(self._config.functions):
            invocations = values.get((label, "invocations"))
            errors = values.get((label, "errors"))
            duration = values.get((label, "duration"))
            if (invocations == 0 and duration is not None) or (
                invocations != 0 and duration is None
            ):
                provider_complete = False
            if isinstance(invocations, int) and isinstance(errors, int) and errors > invocations:
                provider_complete = False
        result_metrics = tuple(
            _metric(f"{label}_hour_{metric}", values.get((label, metric)))
            for label in sorted(self._config.functions)
            for metric in ("invocations", "errors", "throttles", "duration")
        )
        return result_metrics, provider_complete and len(values) == len(identities)

    def _cloudfront_section(self, now: datetime) -> AdminStatusSection:
        distribution_id = self._cloudfront_distribution_id()
        distribution = self._cloudfront.get_distribution(Id=distribution_id).get("Distribution", {})
        config = distribution.get("DistributionConfig", {})
        certificate_arn = _distribution_certificate_arn(
            config.get("ViewerCertificate"),
            account_id=self._config.aws_account_id,
        )
        invalidations = (
            self._cloudfront.list_invalidations(
                DistributionId=distribution_id,
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
        certificate_status = certificate.get("Status")
        certificate_expires_at = certificate.get("NotAfter")
        certificate_complete = (
            isinstance(certificate_status, str)
            and isinstance(certificate_expires_at, datetime)
            and certificate_expires_at.tzinfo is not None
            and certificate_expires_at.utcoffset() is not None
        )
        certificate_healthy = (
            certificate_complete
            and certificate_status == "ISSUED"
            and certificate_expires_at.astimezone(UTC) > now
        )
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
            _metric("certificate_status", certificate_status or "unknown"),
            _metric("certificate_expires_at", _timestamp(certificate_expires_at)),
        ]
        provider_metrics, provider_metrics_complete = self._cloudfront_metrics(
            now,
            distribution_id=distribution_id,
        )
        metrics.extend(provider_metrics)
        return AdminStatusSection(
            service="cloudfront",
            state="unknown"
            if not certificate_complete or not provider_metrics_complete
            else "healthy"
            if enabled and deployed and certificate_healthy
            else "warning",
            summary="証明書または一部の指標を取得できませんでした。"
            if not certificate_complete or not provider_metrics_complete
            else "Distributionと証明書を確認しました。CacheHitRate追加指標は未収集です。",
            metrics=tuple(metrics),
        )

    def _cloudfront_distribution_id(self) -> str:
        matches: list[str] = []
        paginator = self._cloudfront.get_paginator("list_distributions")
        for page in _bounded_pages(paginator.paginate()):
            distribution_list = page.get("DistributionList", {})
            if not isinstance(distribution_list, Mapping):
                raise ValueError("CloudFront distribution list is invalid")
            items = distribution_list.get("Items", [])
            if not isinstance(items, list):
                raise ValueError("CloudFront distribution list is invalid")
            for item in items:
                if not isinstance(item, Mapping):
                    raise ValueError("CloudFront distribution summary is invalid")
                aliases = item.get("Aliases", {})
                if not isinstance(aliases, Mapping):
                    raise ValueError("CloudFront aliases are invalid")
                alias_items = aliases.get("Items", [])
                if not isinstance(alias_items, list) or any(
                    not isinstance(alias, str) for alias in alias_items
                ):
                    raise ValueError("CloudFront aliases are invalid")
                if self._config.records_public_hostname not in alias_items:
                    continue
                distribution_id = item.get("Id")
                if (
                    not isinstance(distribution_id, str)
                    or re.fullmatch(r"[A-Z0-9]{10,30}", distribution_id) is None
                ):
                    raise ValueError("CloudFront distribution ID is invalid")
                matches.append(distribution_id)
        if len(matches) != 1:
            raise ValueError("Records CloudFront distribution is unavailable")
        return matches[0]

    def _cloudfront_metrics(
        self,
        now: datetime,
        *,
        distribution_id: str,
    ) -> tuple[tuple[AdminStatusMetric, ...], bool]:
        queries = []
        identifiers: dict[str, str] = {}
        for index, (metric, stat) in enumerate(
            (
                ("Requests", "Sum"),
                ("4xxErrorRate", "Average"),
                ("5xxErrorRate", "Average"),
            )
        ):
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
                                {"Name": "DistributionId", "Value": distribution_id},
                                {"Name": "Region", "Value": "Global"},
                            ],
                        },
                        "Period": 3600,
                        "Stat": stat,
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
        values: dict[str, int | str | None] = {value: None for value in identifiers.values()}
        results = response.get("MetricDataResults")
        provider_complete = (
            isinstance(results, list)
            and len(results) == len(identifiers)
            and not response.get("NextToken")
        )
        seen_ids: set[str] = set()
        sampled_metrics: set[str] = set()
        for result in results if isinstance(results, list) else []:
            if not isinstance(result, Mapping):
                provider_complete = False
                continue
            result_id = result.get("Id")
            metric = identifiers.get(result_id)
            samples = result.get("Values", [])
            if (
                metric is None
                or not isinstance(result_id, str)
                or result_id in seen_ids
                or result.get("StatusCode") != "Complete"
                or not isinstance(samples, list)
            ):
                provider_complete = False
                continue
            seen_ids.add(result_id)
            if not samples:
                values[metric] = 0 if metric == "Requests" else None
                continue
            sampled_metrics.add(metric)
            value = samples[0]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                or (metric == "Requests" and not float(value).is_integer())
            ):
                provider_complete = False
                continue
            values[metric] = int(value) if metric == "Requests" else f"{value:.3f}"
        requests = values["Requests"]
        if isinstance(requests, int) and not isinstance(requests, bool):
            for metric in ("4xxErrorRate", "5xxErrorRate"):
                if values[metric] is None:
                    if requests == 0:
                        values[metric] = "0.000"
                    else:
                        provider_complete = False
                elif requests == 0 and (
                    "Requests" not in sampled_metrics or values[metric] != "0.000"
                ):
                    provider_complete = False
        else:
            provider_complete = False
        metrics = (
            _metric("hour_requests", requests),
            _metric("hour_4xx_rate", values["4xxErrorRate"]),
            _metric("hour_5xx_rate", values["5xxErrorRate"]),
            _metric("hour_cache_hit_rate", "DISABLED"),
        )
        return metrics, provider_complete and len(seen_ids) == len(identifiers)

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
        retention_seconds = _decimal_integer(attributes.get("MessageRetentionPeriod"))
        state: AdminHealthState = (
            "warning"
            if visible
            or inflight
            or delayed
            or not encrypted
            or retention_seconds != _PROJECTOR_DLQ_RETENTION_SECONDS
            else "healthy"
        )
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
            summary="記録・親愛度投影DLQのメッセージまたは保護設定を確認してください。"
            if state == "warning"
            else "記録・親愛度投影DLQは空で保護設定も正常です。",
            metrics=(
                _metric("visible_messages", visible),
                _metric("inflight_messages", inflight),
                _metric("delayed_messages", delayed),
                _metric("oldest_message_age_seconds", oldest_age),
                _metric("encrypted", encrypted),
                _metric(
                    "retention_seconds",
                    retention_seconds,
                ),
            ),
        )

    def _apigateway_section(self, now: datetime) -> AdminStatusSection:
        api_definitions = (
            (
                "discord",
                "runtime",
                "shittim-chest-production-discord-interactions",
            ),
            ("records", "records_application", "shittim-chest-production-records"),
        )
        metrics: list[AdminStatusMetric] = []
        api_ids: dict[str, str] = {}
        warning = False
        for label, stack_label, expected_name in api_definitions:
            api_id = _single(self._stack_resources(stack_label, "AWS::ApiGatewayV2::Api"))
            api = self._apigateway.get_api(ApiId=api_id)
            stages = self._apigateway.get_stages(ApiId=api_id, MaxResults="50")
            stage_items = stages.get("Items", [])
            if (
                api.get("Name") != expected_name
                or api.get("ProtocolType") != "HTTP"
                or stages.get("NextToken")
                or not isinstance(stage_items, list)
                or len(stage_items) != 1
                or stage_items[0].get("StageName") != "$default"
            ):
                raise ValueError("allowlisted HTTP API is invalid")
            auto_deploy = stage_items[0].get("AutoDeploy") is True
            warning = warning or not auto_deploy
            api_ids[label] = api_id
            metrics.extend(
                (
                    _metric(f"{label}_protocol", "HTTP"),
                    _metric(f"{label}_auto_deploy", auto_deploy),
                )
            )

        queries: list[dict[str, object]] = []
        identities: dict[str, tuple[str, str]] = {}
        counter = 0
        for label, api_id in api_ids.items():
            for metric_name, stat, output_name in (
                ("Count", "Sum", "requests"),
                ("4XXError", "Sum", "4xx"),
                ("5XXError", "Sum", "5xx"),
                ("Latency", "p95", "latency"),
                ("IntegrationLatency", "p95", "integration_latency"),
            ):
                identifier = f"a{counter}"
                counter += 1
                identities[identifier] = (label, output_name)
                queries.append(
                    {
                        "Id": identifier,
                        "MetricStat": {
                            "Metric": {
                                "Namespace": "AWS/ApiGateway",
                                "MetricName": metric_name,
                                "Dimensions": [{"Name": "ApiId", "Value": api_id}],
                            },
                            "Period": 3600,
                            "Stat": stat,
                        },
                        "ReturnData": True,
                    }
                )
        samples, complete = self._metric_data_samples(queries=queries, now=now, hours=1)
        values: dict[tuple[str, str], int | str | None] = {}
        for identifier, identity in identities.items():
            metric_samples = samples.get(identifier, ())
            if identity[1] in {"requests", "4xx", "5xx"}:
                total = sum(metric_samples)
                if not total.is_integer():
                    complete = False
                    continue
                values[identity] = int(total)
            else:
                values[identity] = None if not metric_samples else f"{max(metric_samples):.3f}"
        for label in api_ids:
            requests = values.get((label, "requests"))
            errors_4xx = values.get((label, "4xx"))
            errors_5xx = values.get((label, "5xx"))
            if (
                not isinstance(requests, int)
                or not isinstance(errors_4xx, int)
                or not isinstance(errors_5xx, int)
                or errors_4xx + errors_5xx > requests
                or (
                    requests > 0
                    and (
                        values.get((label, "latency")) is None
                        or values.get((label, "integration_latency")) is None
                    )
                )
            ):
                complete = False
            warning = warning or (isinstance(errors_5xx, int) and errors_5xx > 0)
            metrics.extend(
                _metric(f"{label}_hour_{name}", values.get((label, name)))
                for name in ("requests", "4xx", "5xx", "latency", "integration_latency")
            )
        return AdminStatusSection(
            service="apigateway",
            state="unknown" if not complete else "warning" if warning else "healthy",
            summary=(
                "一部のAPI指標を取得できませんでした。"
                if not complete
                else "HTTP APIと直近1時間の応答を確認しました。"
            ),
            metrics=tuple(metrics),
        )

    def _eventbridge_section(self, now: datetime) -> AdminStatusSection:
        rules_by_description: dict[str, Mapping[str, object]] = {}
        for stack_label in ("records_application", "operations"):
            for rule_name in self._stack_resources(stack_label, "AWS::Events::Rule"):
                rule = self._events.describe_rule(Name=rule_name)
                description = rule.get("Description")
                if not isinstance(description, str) or description in rules_by_description:
                    raise ValueError("allowlisted EventBridge rule is invalid")
                rules_by_description[description] = rule
        expected_descriptions = set(_EVENT_RULE_DESCRIPTIONS.values())
        if set(rules_by_description) != expected_descriptions:
            raise ValueError("allowlisted EventBridge rules are incomplete")

        runtime_schedule = self._scheduler.get_schedule(
            GroupName="default",
            Name=self._config.runtime_scheduler_name,
        )
        if runtime_schedule.get("Name") != self._config.runtime_scheduler_name:
            raise ValueError("runtime scheduler is invalid")
        runtime_state = runtime_schedule.get("State")
        runtime_expression = runtime_schedule.get("ScheduleExpression")
        retry_attempts = (
            runtime_schedule.get("Target", {}).get("RetryPolicy", {}).get("MaximumRetryAttempts")
        )
        metrics: list[AdminStatusMetric] = [
            _metric("runtime_state", runtime_state or "unknown"),
            _metric("runtime_expression", runtime_expression or "unknown"),
            _metric("runtime_retry_attempts", _optional_integer(retry_attempts)),
        ]
        warning = runtime_state != "ENABLED"
        queries: list[dict[str, object]] = []
        identities: dict[str, tuple[str, str]] = {}
        rule_states: dict[str, object] = {}
        counter = 0
        for label, description in _EVENT_RULE_DESCRIPTIONS.items():
            rule = rules_by_description[description]
            rule_name = rule.get("Name")
            if not isinstance(rule_name, str) or not rule_name:
                raise ValueError("EventBridge rule name is invalid")
            rule_states[label] = rule.get("State")
            warning = warning or rule.get("State") != "ENABLED"
            metrics.extend(
                (
                    _metric(f"{label}_state", rule.get("State") or "unknown"),
                    _metric(
                        f"{label}_expression",
                        rule.get("ScheduleExpression") or "event pattern",
                    ),
                )
            )
            for metric_name, output_name in (
                ("Invocations", "invocations"),
                ("FailedInvocations", "failures"),
            ):
                identifier = f"e{counter}"
                counter += 1
                identities[identifier] = (label, output_name)
                queries.append(
                    {
                        "Id": identifier,
                        "MetricStat": {
                            "Metric": {
                                "Namespace": "AWS/Events",
                                "MetricName": metric_name,
                                "Dimensions": [{"Name": "RuleName", "Value": rule_name}],
                            },
                            "Period": 86400,
                            "Stat": "Sum",
                        },
                        "ReturnData": True,
                    }
                )
        samples, complete = self._metric_data_samples(queries=queries, now=now, hours=24)
        for identifier, (label, output_name) in identities.items():
            total = sum(samples.get(identifier, ()))
            value: int | None = int(total) if total.is_integer() else None
            if value is None:
                complete = False
            metrics.append(_metric(f"{label}_day_{output_name}", value))
            if output_name == "failures" and value:
                warning = True
        return AdminStatusSection(
            service="eventbridge",
            state="unknown" if not complete else "warning" if warning else "healthy",
            summary=(
                "一部の配信指標を取得できませんでした。"
                if not complete
                else "定期実行とイベント配信を確認しました。"
            ),
            metrics=tuple(metrics),
        )

    def _affection_dynamodb_metrics(
        self, now: datetime
    ) -> tuple[tuple[AdminStatusMetric, ...], AdminHealthState]:
        pointer = self._statistics_item(pk="RANKING#AFFECTION", sk="CURRENT")
        seed = self._statistics_item(pk="AFFECTION#SEED", sk="CURRENT")
        if pointer is None or seed is None:
            return (
                (
                    _metric("affection_ranking_ready", pointer is not None),
                    _metric("affection_ranking_fresh", None),
                    _metric("affection_profile_count", None),
                    _metric("affection_page_count", None),
                    _metric("affection_ranking_generated_at", None),
                    _metric("affection_seed_complete", None),
                    _metric("affection_seed_archive_count", None),
                ),
                "warning",
            )

        pointer_generated_at, profile_count, page_count = _affection_pointer(pointer)
        seed_generated_at, seed_archive_count, seed_profile_count, seed_complete = _affection_seed(
            seed
        )
        if (
            pointer_generated_at != seed_generated_at
            or profile_count != seed_profile_count
            or page_count
            != (profile_count + _AFFECTION_RANKING_PAGE_SIZE - 1) // _AFFECTION_RANKING_PAGE_SIZE
        ):
            raise ValueError("affection ranking metadata is inconsistent")
        future = pointer_generated_at > now + timedelta(minutes=5)
        fresh = not future and now - pointer_generated_at <= _AFFECTION_RANKING_FRESHNESS
        state: AdminHealthState = "healthy" if fresh and seed_complete else "warning"
        return (
            (
                _metric("affection_ranking_ready", True),
                _metric("affection_ranking_fresh", fresh),
                _metric("affection_profile_count", profile_count),
                _metric("affection_page_count", page_count),
                _metric("affection_ranking_generated_at", _timestamp(pointer_generated_at)),
                _metric("affection_seed_complete", seed_complete),
                _metric("affection_seed_archive_count", seed_archive_count),
            ),
            state,
        )

    def _statistics_item(self, *, pk: str, sk: str) -> Mapping[str, object] | None:
        response = self._dynamodb.get_item(
            TableName=self._config.tables["statistics"],
            Key=marshal_item({"PK": pk, "SK": sk}),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        return None if raw is None else unmarshal_item(raw)

    def _cloudformation_section(self) -> AdminStatusSection:
        metrics: list[AdminStatusMetric] = []
        warning = False
        critical = False
        unknown = False
        for label in _STACK_LABELS:
            client = self._stack_client(label)
            response = client.describe_stacks(StackName=self._config.stacks[label])
            stacks = response.get("Stacks", [])
            if response.get("NextToken") or not isinstance(stacks, list) or len(stacks) != 1:
                raise ValueError("allowlisted CloudFormation stack is unavailable")
            stack = stacks[0]
            status = stack.get("StackStatus")
            drift = stack.get("DriftInformation", {}).get("StackDriftStatus")
            protected = stack.get("EnableTerminationProtection") is True
            updated_at = stack.get("LastUpdatedTime") or stack.get("CreationTime")
            if not isinstance(status, str) or not isinstance(drift, str):
                unknown = True
            else:
                critical = critical or any(part in status for part in _CRITICAL_STACK_STATUS_PARTS)
                warning = warning or status not in _STABLE_STACK_STATUSES or drift == "DRIFTED"
                unknown = unknown or (
                    drift in _UNKNOWN_STACK_DRIFT_STATUSES or drift not in {"IN_SYNC", "DRIFTED"}
                )
            metrics.extend(
                (
                    _metric(f"{label}_status", status or "unknown"),
                    _metric(f"{label}_drift", drift or "unknown"),
                    _metric(f"{label}_termination_protection", protected),
                    _metric(f"{label}_updated_at", _timestamp(updated_at)),
                )
            )
        state: AdminHealthState = (
            "critical"
            if critical
            else "unknown"
            if unknown
            else "warning"
            if warning
            else "healthy"
        )
        return AdminStatusSection(
            service="cloudformation",
            state=state,
            summary=(
                "最後に記録されたdriftまたはStack状態を確認してください。"
                if state != "healthy"
                else "8 Stackの状態と最後のdrift結果を確認しました。"
            ),
            metrics=tuple(metrics),
        )

    def _sns_section(self, now: datetime) -> AdminStatusSection:
        attributes = self._sns.get_topic_attributes(TopicArn=self._config.sns_topic_arn).get(
            "Attributes", {}
        )
        confirmed = _decimal_integer(attributes.get("SubscriptionsConfirmed"))
        pending = _decimal_integer(attributes.get("SubscriptionsPending"))
        topic_name = _sns_topic_name(
            self._config.sns_topic_arn,
            account_id=self._config.aws_account_id,
        )
        queries: list[dict[str, object]] = [
            {
                "Id": identifier,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/SNS",
                        "MetricName": metric_name,
                        "Dimensions": [{"Name": "TopicName", "Value": topic_name}],
                    },
                    "Period": 86400,
                    "Stat": "Sum",
                },
                "ReturnData": True,
            }
            for identifier, metric_name in (
                ("sns_delivered", "NumberOfNotificationsDelivered"),
                ("sns_failed", "NumberOfNotificationsFailed"),
            )
        ]
        samples, complete = self._metric_data_samples(queries=queries, now=now, hours=24)
        delivered_total = sum(samples.get("sns_delivered", ()))
        failed_total = sum(samples.get("sns_failed", ()))
        delivered = int(delivered_total) if delivered_total.is_integer() else None
        failed = int(failed_total) if failed_total.is_integer() else None
        complete = complete and delivered is not None and failed is not None
        warning = confirmed < 1 or pending > 0 or bool(failed)
        return AdminStatusSection(
            service="sns",
            state="unknown" if not complete else "warning" if warning else "healthy",
            summary=(
                "一部の通知指標を取得できませんでした。"
                if not complete
                else "運用通知の購読と直近24時間の配信を確認しました。"
            ),
            metrics=(
                _metric("confirmed_subscriptions", confirmed),
                _metric("pending_subscriptions", pending),
                _metric("day_delivered", delivered),
                _metric("day_failed", failed),
            ),
        )

    def _ssm_section(self) -> AdminStatusSection:
        runtime_parameters = self._runtime_stack_parameters()
        config_version = runtime_parameters.get("RuntimeConfigVersion")
        if (
            not isinstance(config_version, str)
            or re.fullmatch(r"v[0-9]{4}", config_version) is None
        ):
            raise ValueError("runtime configuration version is invalid")
        parameters = dict(self._config.static_parameters)
        parameters.update(
            {
                "runtime_config": f"/shittim-chest/production/runtime/{config_version}",
                "persona_moderator": (
                    f"/shittim-chest/production/personas/{config_version}/moderator"
                ),
                "persona_participant_a": (
                    f"/shittim-chest/production/personas/{config_version}/participant-a"
                ),
                "persona_participant_b": (
                    f"/shittim-chest/production/personas/{config_version}/participant-b"
                ),
                "persona_participant_c": (
                    f"/shittim-chest/production/personas/{config_version}/participant-c"
                ),
            }
        )
        labels_by_name: dict[str, str] = {}
        for label, name in parameters.items():
            if name in labels_by_name:
                raise ValueError("SSM parameter configuration contains a duplicate name")
            labels_by_name[name] = label

        present: set[str] = set()
        modified: list[datetime] = []
        paginator = self._ssm.get_paginator("describe_parameters")
        pages = paginator.paginate(
            ParameterFilters=[
                {
                    "Key": "Name",
                    "Option": "Equals",
                    "Values": sorted(labels_by_name),
                }
            ],
            PaginationConfig={"PageSize": 50},
        )
        for page in _bounded_pages(pages):
            metadata = page.get("Parameters", [])
            if not isinstance(metadata, list):
                raise ValueError("SSM metadata response is invalid")
            for item in metadata:
                if not isinstance(item, Mapping):
                    raise ValueError("SSM metadata item is invalid")
                name = item.get("Name")
                label = labels_by_name.get(name) if isinstance(name, str) else None
                if label is None:
                    continue
                modified_at = item.get("LastModifiedDate")
                if (
                    item.get("Type") != "SecureString"
                    or not isinstance(item.get("Version"), int)
                    or item["Version"] < 1
                    or not isinstance(modified_at, datetime)
                    or modified_at.tzinfo is None
                    or modified_at.utcoffset() is None
                ):
                    continue
                if label in present:
                    raise ValueError("SSM metadata response contains a duplicate parameter")
                present.add(label)
                modified.append(modified_at)

        groups = {
            "discord": {
                "discord_public_key",
                "moderator_token",
                "participant_a_token",
                "participant_b_token",
                "participant_c_token",
            },
            "runtime": {
                "openai_api_key",
                "runtime_config",
                "persona_moderator",
                "persona_participant_a",
                "persona_participant_b",
                "persona_participant_c",
            },
            "records": {
                "records_identity",
                "records_presentation",
                "records_oauth",
                "records_client_secret",
                "records_session_key",
                "records_admin_user_id",
                "records_openai_inspector_translation_key",
            },
            "cost": {"records_openai_admin_key", "records_openai_project_id"},
        }
        metrics: list[AdminStatusMetric] = []
        ready = True
        for group, required in groups.items():
            ready_count = len(required & present)
            ready = ready and ready_count == len(required)
            metrics.extend(
                (
                    _metric(f"{group}_ready", ready_count),
                    _metric(f"{group}_required", len(required)),
                )
            )
        metrics.extend(
            (
                _metric(
                    "runtime_prompt_pointer_present",
                    "runtime_prompts_active" in present,
                ),
                _metric("latest_modified_at", _timestamp(max(modified) if modified else None)),
            )
        )
        return AdminStatusSection(
            service="ssm",
            state="healthy" if ready else "warning",
            summary=(
                "必要な設定metadataを確認しました。"
                if ready
                else "不足または形式不一致の設定があります。"
            ),
            metrics=tuple(metrics),
        )

    def _cost_governance_section(self) -> AdminStatusSection:
        metrics: list[AdminStatusMetric] = []
        warning = False
        critical = False
        for label in ("project", "account"):
            budget = self._budgets.describe_budget(
                AccountId=self._config.aws_account_id,
                BudgetName=self._config.budgets[label],
                ShowFilterExpression=False,
            ).get("Budget", {})
            limit = _decimal_amount(budget.get("BudgetLimit"))
            spend = budget.get("CalculatedSpend", {})
            actual = _decimal_amount(spend.get("ActualSpend"))
            forecast = (
                _decimal_amount(spend.get("ForecastedSpend"))
                if spend.get("ForecastedSpend") is not None
                else None
            )
            actual_percent = _percent(actual, limit)
            forecast_percent = _percent(forecast, limit) if forecast is not None else None
            health = budget.get("HealthStatus", {}).get("Status") or "unknown"
            critical = critical or actual_percent >= Decimal("100")
            warning = (
                warning
                or actual_percent >= Decimal("80")
                or (forecast_percent is not None and forecast_percent >= Decimal("100"))
                or health == "UNHEALTHY"
            )
            metrics.extend(
                (
                    _metric(f"{label}_actual_percent", _decimal_text(actual_percent)),
                    _metric(
                        f"{label}_forecast_percent",
                        _decimal_text(forecast_percent) if forecast_percent is not None else None,
                    ),
                    _metric(f"{label}_health", health),
                )
            )

        matches: list[Mapping[str, object]] = []
        token: str | None = None
        for _page in range(_MAX_PAGINATOR_PAGES):
            request: dict[str, object] = {"MaxResults": 100}
            if token is not None:
                request["NextPageToken"] = token
            response = self._cost_explorer.get_anomaly_subscriptions(**request)
            subscriptions = response.get("AnomalySubscriptions", [])
            if not isinstance(subscriptions, list):
                raise ValueError("cost anomaly subscriptions are invalid")
            matches.extend(
                item
                for item in subscriptions
                if isinstance(item, Mapping)
                and item.get("SubscriptionName") == self._config.anomaly_subscription_name
            )
            next_token = response.get("NextPageToken")
            if next_token is None:
                break
            if not isinstance(next_token, str) or not next_token:
                raise ValueError("cost anomaly pagination is invalid")
            token = next_token
        else:
            raise ValueError("cost anomaly pagination exceeded its bounded page count")
        subscription = _single(matches)
        subscribers = subscription.get("Subscribers", [])
        if not isinstance(subscribers, list):
            raise ValueError("cost anomaly subscribers are invalid")
        confirmed = sum(
            1
            for subscriber in subscribers
            if isinstance(subscriber, Mapping) and subscriber.get("Status") == "CONFIRMED"
        )
        warning = warning or not subscribers or confirmed != len(subscribers)
        metrics.extend(
            (
                _metric("anomaly_subscription", True),
                _metric("anomaly_frequency", subscription.get("Frequency") or "unknown"),
                _metric("anomaly_subscribers", len(subscribers)),
                _metric("anomaly_confirmed_subscribers", confirmed),
            )
        )
        return AdminStatusSection(
            service="cost_governance",
            state="critical" if critical else "warning" if warning else "healthy",
            summary="予算使用率とCost Anomaly通知を確認しました。",
            metrics=tuple(metrics),
        )

    def _signer_section(self) -> AdminStatusSection:
        profile = self._signer.get_signing_profile(
            profileName=self._config.signing_profile_name,
            profileOwner=self._config.aws_account_id,
        )
        if profile.get("profileName") != self._config.signing_profile_name:
            raise ValueError("signing profile is invalid")
        status = profile.get("status")
        platform = profile.get("platformId")
        validity = profile.get("signatureValidityPeriod", {})
        state: AdminHealthState = (
            "healthy"
            if status == "Active" and platform == "Notation-OCI-SHA384-ECDSA"
            else "warning"
        )
        return AdminStatusSection(
            service="signer",
            state=state,
            summary="コンテナ署名profileを確認しました。",
            metrics=(
                _metric("status", status or "unknown"),
                _metric("platform", platform or "unknown"),
                _metric("validity_value", _optional_integer(validity.get("value"))),
                _metric("validity_unit", validity.get("type") or "unknown"),
            ),
        )

    def _external_section(self, now: datetime) -> AdminStatusSection:
        metrics: list[AdminStatusMetric] = []
        warning = False
        for source in _CHECKPOINT_SOURCES:
            response = self._dynamodb.get_item(
                TableName=self._config.tables["statistics"],
                Key=marshal_item({"PK": "COLLECTOR#COST", "SK": source}),
                ConsistentRead=True,
            )
            raw = response.get("Item")
            label = source.casefold()
            if raw is None:
                warning = True
                metrics.extend(
                    (
                        _metric(f"{label}_initial_complete", False),
                        _metric(f"{label}_fresh", False),
                        _metric(f"{label}_last_success_at", None),
                        _metric(f"{label}_last_failure_at", None),
                        _metric(f"{label}_failure_code", None),
                    )
                )
                continue
            item = unmarshal_item(raw)
            required = {
                "PK",
                "SK",
                "schema_version",
                "record_type",
                "source",
                "next_date",
                "initial_complete",
            }
            optional = {"last_success_at", "last_failure_at", "last_failure_code"}
            next_date = item.get("next_date")
            if (
                not required <= set(item) <= required | optional
                or item.get("PK") != "COLLECTOR#COST"
                or item.get("SK") != source
                or item.get("schema_version") != 1
                or item.get("record_type") != "cost_checkpoint"
                or item.get("source") != source
                or not isinstance(item.get("initial_complete"), bool)
                or not isinstance(next_date, str)
                or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", next_date) is None
            ):
                raise ValueError("cost checkpoint metadata is invalid")
            success = _iso_timestamp(item.get("last_success_at"))
            failure = _iso_timestamp(item.get("last_failure_at"))
            failure_code = item.get("last_failure_code")
            if failure_code is not None and (
                not isinstance(failure_code, str)
                or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", failure_code) is None
                or failure is None
            ):
                raise ValueError("cost checkpoint failure metadata is invalid")
            freshness = timedelta(hours=3 if source == "OPENAI" else 36)
            fresh = success is not None and now - success <= freshness
            future_timestamp = any(
                timestamp is not None and timestamp > now + timedelta(minutes=5)
                for timestamp in (success, failure)
            )
            failed_after_success = failure is not None and (success is None or failure >= success)
            initial_complete = cast(bool, item["initial_complete"])
            warning = (
                warning
                or not fresh
                or failed_after_success
                or future_timestamp
                or not initial_complete
            )
            metrics.extend(
                (
                    _metric(f"{label}_initial_complete", initial_complete),
                    _metric(f"{label}_fresh", fresh),
                    _metric(f"{label}_last_success_at", _timestamp(success)),
                    _metric(f"{label}_last_failure_at", _timestamp(failure)),
                    _metric(f"{label}_failure_code", failure_code),
                )
            )
        return AdminStatusSection(
            service="external",
            state="warning" if warning else "healthy",
            summary=(
                "外部集計の初期取込、失敗、または鮮度を確認してください。"
                if warning
                else "OpenAIとFrankfurterを含む集計鮮度を確認しました。"
            ),
            metrics=tuple(metrics),
        )

    def _stack_client(self, label: str) -> Any:
        return (
            self._cloudformation_global if label in _GLOBAL_STACK_LABELS else self._cloudformation
        )

    def _stack_resources(self, stack_label: str, resource_type: str) -> tuple[str, ...]:
        client = self._stack_client(stack_label)
        paginator = client.get_paginator("list_stack_resources")
        resources: list[str] = []
        for page in _bounded_pages(paginator.paginate(StackName=self._config.stacks[stack_label])):
            summaries = page.get("StackResourceSummaries", [])
            if not isinstance(summaries, list):
                raise ValueError("CloudFormation stack resources are invalid")
            for item in summaries:
                if not isinstance(item, Mapping) or item.get("ResourceType") != resource_type:
                    continue
                physical_id = item.get("PhysicalResourceId")
                if not isinstance(physical_id, str) or not physical_id:
                    raise ValueError("CloudFormation physical resource ID is invalid")
                resources.append(physical_id)
        return tuple(resources)

    def _runtime_stack_parameters(self) -> Mapping[str, object]:
        response = self._cloudformation.describe_stacks(StackName=self._config.runtime_stack_name)
        stacks = response.get("Stacks", [])
        if response.get("NextToken") or not isinstance(stacks, list) or len(stacks) != 1:
            raise ValueError("Runtime stack is unavailable")
        parameters = stacks[0].get("Parameters", [])
        if not isinstance(parameters, list):
            raise ValueError("Runtime stack parameters are invalid")
        return {
            item.get("ParameterKey"): item.get("ParameterValue")
            for item in parameters
            if isinstance(item, Mapping)
        }

    def _metric_data_samples(
        self,
        *,
        queries: list[dict[str, object]],
        now: datetime,
        hours: int,
    ) -> tuple[dict[str, tuple[float, ...]], bool]:
        response = self._cloudwatch.get_metric_data(
            MetricDataQueries=queries,
            StartTime=now - timedelta(hours=hours),
            EndTime=now,
            ScanBy="TimestampDescending",
        )
        expected = {cast(str, query["Id"]) for query in queries}
        results = response.get("MetricDataResults")
        complete = (
            isinstance(results, list)
            and len(results) == len(expected)
            and not response.get("NextToken")
        )
        samples: dict[str, tuple[float, ...]] = {}
        for result in results if isinstance(results, list) else []:
            if not isinstance(result, Mapping):
                complete = False
                continue
            identifier = result.get("Id")
            values = result.get("Values", [])
            if (
                not isinstance(identifier, str)
                or identifier not in expected
                or identifier in samples
                or result.get("StatusCode") != "Complete"
                or not isinstance(values, list)
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                    for value in values
                )
            ):
                complete = False
                continue
            samples[identifier] = tuple(float(value) for value in values)
        return samples, complete and set(samples) == expected

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


def _optional_nonnegative_integer(value: object) -> int | None:
    parsed = _optional_integer(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _optional_boolean(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _nonnegative_count(value: object) -> int:
    if value is None:
        return 0
    parsed = _optional_nonnegative_integer(value)
    if parsed is None:
        raise ValueError("provider count is invalid")
    return parsed


def _known_value(value: object, allowed: set[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _mapping_value(value: object, key: str) -> object:
    return value.get(key) if isinstance(value, Mapping) else None


def _aware_datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("provider timestamp is invalid")
    return value.astimezone(UTC)


def _optional_aware_datetime(value: object) -> datetime | None:
    return None if value is None else _aware_datetime(value)


def _primary_ecs_deployment(deployments: list[object]) -> Mapping[str, Any]:
    if any(not isinstance(item, Mapping) for item in deployments):
        raise ValueError("ECS deployment is invalid")
    deployment_mappings = [cast(Mapping[str, Any], item) for item in deployments]
    primary = [item for item in deployment_mappings if item.get("status") == "PRIMARY"]
    if len(primary) > 1:
        raise ValueError("ECS primary deployment is ambiguous")
    return primary[0] if primary else {}


def _ecs_task_definition_arn(value: object, *, account_id: str) -> str:
    pattern = (
        rf"arn:aws:ecs:[a-z0-9-]+:{re.escape(account_id)}:"
        r"task-definition/[A-Za-z0-9_-]{1,255}:[1-9][0-9]*"
    )
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError("ECS task definition ARN is invalid")
    return value


def _ecs_task_definition_revision(value: str) -> int:
    return int(value.rsplit(":", 1)[-1])


def _runtime_ecr_image_digest(
    value: object,
    *,
    task_definition_arn: str,
    account_id: str,
    repository_name: str,
) -> str:
    region_match = re.fullmatch(
        rf"arn:aws:ecs:(?P<region>[a-z0-9-]+):{re.escape(account_id)}:"
        r"task-definition/[A-Za-z0-9_-]{1,255}:[1-9][0-9]*",
        task_definition_arn,
    )
    if region_match is None or not isinstance(value, str):
        raise ValueError("ECS task image is invalid")
    prefix = f"{account_id}.dkr.ecr.{region_match.group('region')}.amazonaws.com/{repository_name}@"
    if not value.startswith(prefix):
        raise ValueError("ECS task image is outside the runtime repository")
    return _image_digest(value.removeprefix(prefix))


def _ecs_launch_mode(
    service: Mapping[str, Any],
    deployment: Mapping[str, Any],
) -> str | None:
    launch_type = deployment.get("launchType", service.get("launchType"))
    known_launch_type = _known_value(
        launch_type,
        {"EC2", "EXTERNAL", "FARGATE", "MANAGED_INSTANCES"},
    )
    if known_launch_type is not None:
        return known_launch_type
    strategy = deployment.get(
        "capacityProviderStrategy",
        service.get("capacityProviderStrategy", []),
    )
    if not isinstance(strategy, list) or any(not isinstance(item, Mapping) for item in strategy):
        return None
    providers = {
        item.get("capacityProvider")
        for item in strategy
        if isinstance(item.get("capacityProvider"), str)
    }
    if not providers:
        return None
    if providers == {"FARGATE"}:
        return "FARGATE"
    if providers == {"FARGATE_SPOT"}:
        return "FARGATE_SPOT"
    if providers <= {"FARGATE", "FARGATE_SPOT"}:
        return "FARGATE_MIXED"
    return "CUSTOM_CAPACITY_PROVIDER"


def _ecs_platform_version(value: object) -> str | None:
    if not isinstance(value, str) or re.fullmatch(r"(?:LATEST|\d+(?:\.\d+){1,2})", value) is None:
        return None
    return value


def _image_tags(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if (
        not isinstance(value, list)
        or len(value) > 100
        or any(not isinstance(item, str) or not item.strip() or len(item) > 300 for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("ECR image tags are invalid")
    return tuple(sorted(value))


def _ecr_media_type(
    image: Mapping[str, Any],
) -> Literal["OCI_IMAGE", "OCI_INDEX", "DOCKER_V2", "DOCKER_LIST", "OTHER"]:
    manifest_type = image.get("imageManifestMediaType")
    if manifest_type == "application/vnd.docker.distribution.manifest.list.v2+json":
        return "DOCKER_LIST"
    if manifest_type == "application/vnd.docker.distribution.manifest.v2+json":
        return "DOCKER_V2"
    if manifest_type == "application/vnd.oci.image.index.v1+json":
        return "OCI_INDEX"
    if manifest_type == "application/vnd.oci.image.manifest.v1+json":
        return "OCI_IMAGE"
    return "OTHER"


def _finding_image_digest(finding: Mapping[str, Any]) -> str:
    resources = finding.get("resources", [])
    if not isinstance(resources, list):
        raise ValueError("Inspector finding resources are invalid")
    digests: set[str] = set()
    for resource in resources:
        if not isinstance(resource, Mapping):
            raise ValueError("Inspector finding resource is invalid")
        if resource.get("type") != "AWS_ECR_CONTAINER_IMAGE":
            continue
        details = resource.get("details", {})
        if not isinstance(details, Mapping):
            raise ValueError("Inspector finding resource details are invalid")
        image = details.get("awsEcrContainerImage", {})
        if not isinstance(image, Mapping):
            raise ValueError("Inspector ECR finding details are invalid")
        digests.add(_image_digest(image.get("imageHash")))
    if len(digests) != 1:
        raise ValueError("Inspector finding image is ambiguous")
    return next(iter(digests))


def _public_inspector_finding(
    finding: Mapping[str, Any],
    severity: str,
) -> _InspectorFindingCandidate:
    details = finding.get("packageVulnerabilityDetails")
    if not isinstance(details, Mapping):
        raise ValueError("Inspector package vulnerability details are invalid")
    vulnerability_id = _vulnerability_id(details)
    raw_packages = details.get("vulnerablePackages", [])
    if not isinstance(raw_packages, list) or not raw_packages or len(raw_packages) > 100:
        raise ValueError("Inspector vulnerable packages are invalid")
    packages: list[AdminInspectorAffectedPackage] = []
    seen: set[tuple[str, str, str | None, str | None]] = set()
    for raw_package in raw_packages:
        if not isinstance(raw_package, Mapping):
            raise ValueError("Inspector vulnerable package is invalid")
        name = _package_text(raw_package.get("name"))
        installed_version = _package_version(raw_package)
        fixed_version = _optional_package_text(raw_package.get("fixedInVersion"))
        package_manager = _optional_package_text(raw_package.get("packageManager"))
        identity = (name, installed_version, fixed_version, package_manager)
        if identity in seen:
            continue
        seen.add(identity)
        packages.append(
            AdminInspectorAffectedPackage(
                name=name,
                installed_version=installed_version,
                fixed_version=fixed_version,
                package_manager=package_manager,
            )
        )
    if not packages:
        raise ValueError("Inspector vulnerable packages are empty")
    source = inspector_description(
        vulnerability_id=vulnerability_id,
        description=finding.get("description"),
    )
    fix_available = _known_value(finding.get("fixAvailable"), {"YES", "NO", "PARTIAL"})
    return _InspectorFindingCandidate(
        source=source,
        public=AdminInspectorFinding(
            vulnerability_id=vulnerability_id,
            severity=cast(Any, severity),
            summary_ja=None,
            affected_packages=tuple(packages),
            fix_available=cast(Any, fix_available),
        ),
    )


def _resolved_inspector_finding(
    candidate: _InspectorFindingCandidate,
    translations: Mapping[str, InspectorJapaneseSummary],
) -> AdminInspectorFinding:
    summary = translations.get(candidate.source.key)
    if summary is None:
        return candidate.public
    if (
        summary.vulnerability_id != candidate.source.vulnerability_id
        or summary.source_sha256 != candidate.source.source_sha256
    ):
        raise ValueError("Inspector translation cache identity is invalid")
    return candidate.public.model_copy(update={"summary_ja": summary.summary_ja})


def _inspector_finding_sort_key(finding: AdminInspectorFinding) -> tuple[int, str]:
    return (
        0 if finding.severity == "critical" else 1,
        finding.vulnerability_id,
    )


def _vulnerability_id(details: Mapping[str, Any]) -> str:
    candidates: list[object] = [details.get("vulnerabilityId")]
    related = details.get("relatedVulnerabilities", [])
    if isinstance(related, list):
        candidates.extend(related)
    for candidate in candidates:
        if (
            isinstance(candidate, str)
            and len(candidate) <= 128
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", candidate) is not None
        ):
            return candidate
    raise ValueError("Inspector vulnerability identifier is invalid")


def _package_version(package: Mapping[str, Any]) -> str:
    version = _package_text(package.get("version"))
    release = _optional_package_text(package.get("release"))
    raw_epoch = package.get("epoch")
    if raw_epoch is None:
        epoch = None
    elif isinstance(raw_epoch, int) and not isinstance(raw_epoch, bool) and raw_epoch >= 0:
        epoch = str(raw_epoch) if raw_epoch > 0 else None
    else:
        raise ValueError("Inspector package epoch is invalid")
    combined = f"{version}-{release}" if release is not None else version
    if epoch is not None:
        combined = f"{epoch}:{combined}"
    if len(combined) > 256:
        raise ValueError("Inspector package version is too long")
    return combined


def _package_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("Inspector package metadata is invalid")
    return value


def _optional_package_text(value: object) -> str | None:
    if value in (None, ""):
        return None
    return _package_text(value)


def _coverage_image_digest(
    resource: Mapping[str, Any],
    *,
    tags_to_digest: Mapping[str, str],
) -> str | None:
    resource_id = resource.get("resourceId")
    if isinstance(resource_id, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", resource_id):
        return resource_id
    metadata = resource.get("resourceMetadata", {})
    if not isinstance(metadata, Mapping):
        return None
    ecr_image = metadata.get("ecrImage", {})
    if not isinstance(ecr_image, Mapping):
        return None
    tags = _image_tags(ecr_image.get("tags"))
    matches = {tags_to_digest[tag] for tag in tags if tag in tags_to_digest}
    if len(matches) > 1:
        raise ValueError("Inspector coverage image tags are ambiguous")
    return next(iter(matches)) if matches else None


def _decimal_integer(value: object) -> int:
    if not isinstance(value, str) or not value.isdecimal():
        raise ValueError("provider decimal count is invalid")
    return int(value)


def _decimal_amount(value: object) -> Decimal:
    if not isinstance(value, Mapping) or value.get("Unit") != "USD":
        raise ValueError("provider money is invalid")
    raw_amount = value.get("Amount")
    if not isinstance(raw_amount, str):
        raise ValueError("provider money is invalid")
    try:
        amount = Decimal(raw_amount)
    except InvalidOperation as error:
        raise ValueError("provider money is invalid") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError("provider money is invalid")
    return amount


def _percent(amount: Decimal, limit: Decimal) -> Decimal:
    if limit <= 0:
        raise ValueError("budget limit is invalid")
    return amount * Decimal("100") / limit


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite() or value < 0:
        raise ValueError("provider decimal is invalid")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _single(values: Iterable[Any]) -> Any:
    entries = tuple(values)
    if len(entries) != 1:
        raise ValueError("allowlisted provider resource is ambiguous")
    return entries[0]


def _sns_topic_name(topic_arn: str, *, account_id: str) -> str:
    pattern = rf"arn:aws:sns:[a-z0-9-]+:{re.escape(account_id)}:([A-Za-z0-9_-]{{1,256}})"
    match = re.fullmatch(pattern, topic_arn)
    if match is None:
        raise ValueError("ADMIN status SNS topic is invalid")
    return match.group(1)


def _iso_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("stored timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("stored timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored timestamp is invalid")
    return parsed.astimezone(UTC)


def _affection_pointer(item: Mapping[str, object]) -> tuple[datetime, int, int]:
    expected = {
        "PK",
        "SK",
        "schema_version",
        "record_type",
        "generation_id",
        "generated_at",
        "profile_count",
        "page_count",
        "checksum",
    }
    generation_id = item.get("generation_id")
    checksum = item.get("checksum")
    profile_count = _optional_nonnegative_integer(item.get("profile_count"))
    page_count = _optional_nonnegative_integer(item.get("page_count"))
    generated_at = _iso_timestamp(item.get("generated_at"))
    if (
        set(item) != expected
        or item.get("PK") != "RANKING#AFFECTION"
        or item.get("SK") != "CURRENT"
        or item.get("schema_version") != 1
        or item.get("record_type") != "affection_ranking_pointer"
        or not isinstance(generation_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", generation_id) is None
        or not isinstance(checksum, str)
        or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
        or not checksum.startswith(generation_id)
        or profile_count is None
        or page_count is None
        or generated_at is None
    ):
        raise ValueError("affection ranking pointer is invalid")
    return generated_at, profile_count, page_count


def _affection_seed(item: Mapping[str, object]) -> tuple[datetime, int, int, bool]:
    expected = {
        "PK",
        "SK",
        "schema_version",
        "record_type",
        "generated_at",
        "archive_count",
        "profile_count",
        "complete",
    }
    archive_count = _optional_nonnegative_integer(item.get("archive_count"))
    profile_count = _optional_nonnegative_integer(item.get("profile_count"))
    complete = item.get("complete")
    generated_at = _iso_timestamp(item.get("generated_at"))
    if (
        set(item) != expected
        or item.get("PK") != "AFFECTION#SEED"
        or item.get("SK") != "CURRENT"
        or item.get("schema_version") != 1
        or item.get("record_type") != "affection_seed_checkpoint"
        or archive_count is None
        or profile_count is None
        or not isinstance(complete, bool)
        or generated_at is None
    ):
        raise ValueError("affection seed checkpoint is invalid")
    return generated_at, archive_count, profile_count, complete


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


def _image_digest(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ValueError("Runtime image digest is invalid")
    return value


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
