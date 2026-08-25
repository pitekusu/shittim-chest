# SPDX-License-Identifier: MIT
"""Enforce notification and production Deploy Guard workflow trust boundaries."""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIRECTORY = REPOSITORY_ROOT / ".github" / "workflows"
ALLOWED_TARGET_WORKFLOW = "discord-repository-events.yml"
DEPLOY_GUARD_WORKFLOW = "production-deploy-guard.yml"
RELEASE_WORKFLOW = "release.yml"
DRIFT_WORKFLOW = "drift.yml"
RECORDS_CI_WORKFLOW = "records-ci.yml"
RECORDS_RELEASE_WORKFLOW = "records-release.yml"
RECORDS_BACKFILL_WORKFLOW = "records-backfill.yml"
WORKFLOW_RUN_NOTIFICATION = "discord-workflow-run.yml"
PINNED_BUILDX_VERSION = "v0.35.0"
PINNED_BUILDKIT_DIGEST = "sha256:2f5adac4ecd194d9f8c10b7b5d7bceb5186853db1b26e5abd3a657af0b7e26ec"
PINNED_BUILDKIT_IMAGE = f"moby/buildkit:v0.31.2@{PINNED_BUILDKIT_DIGEST}"
RELEASE_REQUIRED_MAIN_CHECKS = frozenset(
    {
        "quality",
        "tests",
        "security",
        "package",
        "cdk",
        "docs-public-safety",
        "container-arm64",
        "grype",
        "Analyze (python)",
        "Analyze (javascript-typescript)",
        "Analyze (actions)",
    }
)
PERMISSIONS_KEY = re.compile(r"(?<![a-zA-Z0-9_-])(?:\"|')?permissions(?:\"|')?\s*:")
YAML_HEXADECIMAL_ESCAPE = re.compile(r"\\(?:x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4})|U([0-9a-fA-F]{8}))")
AWS_OR_DEPLOY_CAPABILITY = re.compile(
    r"secrets\.AWS[A-Z0-9_]*|"
    r"aws-actions/(?:configure-aws-credentials|amazon-ecr-login|amazon-ecs-deploy-task-definition)@|"
    r"\bAWS_(?:DEPLOY_GUARD_)?ROLE_ARN\b|"
    r"\baws\s+(?:cloudformation|dynamodb|ecr|ecs|ssm|sts)\b|"
    r"\bcdk\s+deploy\b|"
    r"\bgh\s+workflow\s+run\b|/(?:dispatches|repository_dispatch)\b|"
    r"\bdocker\s+(?:buildx\s+)?push\b|"
    r"^\s+environment:\s*production\s*(?:#.*)?$",
    re.MULTILINE,
)


class WorkflowPolicyError(RuntimeError):
    """Raised when the target-workflow trust boundary is widened."""


def validate_notification_workflows(directory: Path = WORKFLOW_DIRECTORY) -> int:
    """Validate every target trigger and the one approved workflow."""

    _validate_permission_syntax(directory)
    _validate_consistent_action_pins(directory)
    _validate_pinned_container_builder(directory)
    target_files: list[Path] = []
    for path in sorted((*directory.glob("*.yml"), *directory.glob("*.yaml"))):
        text = path.read_text(encoding="utf-8")
        if any(
            "pull_request_target" in line and not line.lstrip().startswith("#")
            for line in text.splitlines()
        ):
            target_files.append(path)
    names = [path.name for path in target_files]
    if names != [ALLOWED_TARGET_WORKFLOW]:
        raise WorkflowPolicyError(
            "pull_request_target is restricted to the Discord repository-events workflow"
        )
    text = target_files[0].read_text(encoding="utf-8")
    forbidden = {
        "PR head checkout": re.compile(
            r"github\.(?:head_ref|event\.pull_request\.head\.(?:sha|ref))"
        ),
        "artifact action": re.compile(r"actions/(?:download|upload)-artifact@"),
        "cache action": re.compile(r"actions/cache@|cache-from:|cache-to:"),
        "self-hosted runner": re.compile(r"runs-on:\s*self-hosted"),
    }
    for label, pattern in forbidden.items():
        if pattern.search(text):
            raise WorkflowPolicyError(f"target workflow contains forbidden {label}")
    if _contains_untrusted_run_expression(text):
        raise WorkflowPolicyError(
            "target workflow contains forbidden untrusted event expression in run"
        )
    required = (
        "permissions: {}",
        "contents: read",
        "pull-requests: read",
        "ref: ${{ github.sha }}",
        "persist-credentials: false",
        "github.event.pull_request.user.login != 'dependabot[bot]'",
    )
    for marker in required:
        if marker not in text:
            raise WorkflowPolicyError(f"target workflow lacks required policy marker: {marker}")
    expected_target_permissions = (
        (),
        (("contents", "read"), ("pull-requests", "read")),
        (("contents", "read"), ("pull-requests", "read")),
    )
    if _permission_blocks(text) != expected_target_permissions:
        raise WorkflowPolicyError("target workflow permissions must remain exactly read-only")
    if text.count("uses: actions/checkout@") != 2 or text.count("ref: ${{ github.sha }}") != 2:
        raise WorkflowPolicyError("every checkout must use the trusted github.sha ref")
    secret_references = set(re.findall(r"secrets\.([A-Z0-9_]+)", text))
    if secret_references != {"DISCORD_WEBHOOK_URL"}:
        raise WorkflowPolicyError("target workflow may use only DISCORD_WEBHOOK_URL")
    _validate_deploy_guard(directory)
    _validate_release(directory)
    _validate_ci_container_risk(directory)
    _validate_ci_path_isolation(directory)
    _validate_records_workflows(directory)
    _validate_drift(directory)
    _validate_aws_capability_boundary(directory)
    _validate_workflow_run_allowlist(directory)
    _validate_vulnerability_alerts_permission(directory)
    return 1


def _validate_pinned_container_builder(directory: Path) -> None:
    expected = (
        f"          version: {PINNED_BUILDX_VERSION}\n"
        "          driver-opts: |\n"
        f"            image={PINNED_BUILDKIT_IMAGE}"
    )
    for workflow, step_name in (
        ("ci.yml", "Set up Docker Buildx"),
        (RELEASE_WORKFLOW, "Set up Buildx"),
    ):
        text = (directory / workflow).read_text(encoding="utf-8")
        block = _workflow_step_block(text, step_name)
        if (
            text.count("uses: docker/setup-buildx-action@") != 1
            or block.count("uses: docker/setup-buildx-action@") != 1
            or block.count(expected) != 1
        ):
            raise WorkflowPolicyError(
                f"{workflow} must pin the approved Buildx client and BuildKit image digest"
            )


def _contains_untrusted_run_expression(text: str) -> bool:
    return _contains_run_expression(text, "${{ github.event.")


def _contains_run_expression(text: str, expression: str) -> bool:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)run:\s*(.*)$", line)
        if match is None:
            continue
        indentation = len(match.group(1))
        remainder = match.group(2)
        if expression in remainder:
            return True
        if remainder not in {"|", ">", "|-", ">-", "|+", ">+"}:
            continue
        for nested in lines[index + 1 :]:
            if nested.strip() and len(nested) - len(nested.lstrip()) <= indentation:
                break
            if expression in nested:
                return True
    return False


def _validate_deploy_guard(directory: Path) -> None:
    path = directory / DEPLOY_GUARD_WORKFLOW
    if not path.is_file():
        raise WorkflowPolicyError("the production Deploy Guard workflow is required")
    text = path.read_text(encoding="utf-8")
    if _top_level_triggers(text) != ("workflow_dispatch",):
        raise WorkflowPolicyError("Deploy Guard must use exactly the workflow_dispatch trigger")
    forbidden = {
        "production environment": re.compile(r"(?m)^\s+environment:\s*production\s*(?:#.*)?$"),
        "AWS secret": re.compile(r"secrets\.(?:AWS|DYNAMODB|DEPLOY)[A-Z0-9_]*"),
        "AWS CLI operation": re.compile(r"\baws\s+(?:cloudformation|dynamodb|ecr|ecs|ssm|sts)\b"),
        "deployment command": re.compile(
            r"\bcdk\s+deploy\b|\bdocker\s+(?:buildx\s+)?push\b|"
            r"aws-actions/(?:amazon-ecr-login|amazon-ecs-deploy-task-definition)@"
        ),
        "self-hosted runner": re.compile(r"runs-on:\s*self-hosted"),
        "deploy job": re.compile(r"(?m)^  deploy:\s*$|^\s+name:\s*deploy\s*$"),
    }
    for label, pattern in forbidden.items():
        if pattern.search(text):
            raise WorkflowPolicyError(f"Deploy Guard contains forbidden {label}")
    required = (
        "name: Production Deploy Guard",
        "  workflow_dispatch:",
        "      break_glass:",
        "        type: boolean",
        "      break_glass_reason:",
        "        type: choice",
        "          - none",
        "          - incident-response",
        "          - security-investigation",
        "          - service-recovery",
        "permissions: {}",
        "cancel-in-progress: false",
        "contents: read",
        "id-token: write",
        "EXPECTED_REF: refs/heads/main",
        '--actor "${GITHUB_TRIGGERING_ACTOR}"',
        "persist-credentials: false",
        "aws-actions/configure-aws-credentials@e6de054238d6b7531b4efff3b6587d9aade6a06c",
        "vars.AWS_DEPLOY_GUARD_ROLE_ARN",
        "vars.DYNAMODB_TABLE_NAME",
        "BREAK_GLASS_REASON: ${{ inputs.break_glass_reason }}",
        "python -m tools.control_records guard",
        "--audit-output",
        "if: always()",
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
        "if-no-files-found: error",
        "retention-days: 90",
    )
    for marker in required:
        if marker not in text:
            raise WorkflowPolicyError(f"Deploy Guard lacks required policy marker: {marker}")
    _validate_canonical_deploy_guard_permissions(text)
    if _contains_run_expression(text, "${{ inputs.break_glass_reason }}"):
        raise WorkflowPolicyError("Deploy Guard must pass break-glass reason through env")


def _validate_aws_capability_boundary(directory: Path) -> None:
    approved = {
        DEPLOY_GUARD_WORKFLOW,
        RELEASE_WORKFLOW,
        DRIFT_WORKFLOW,
        RECORDS_RELEASE_WORKFLOW,
        RECORDS_BACKFILL_WORKFLOW,
    }
    for path in sorted((*directory.glob("*.yml"), *directory.glob("*.yaml"))):
        if path.name in approved:
            continue
        text = path.read_text(encoding="utf-8")
        if _contains_forbidden_non_guard_permissions(text) or AWS_OR_DEPLOY_CAPABILITY.search(text):
            raise WorkflowPolicyError(
                f"workflow {path.name} contains AWS or deployment capability outside Deploy Guard"
            )


def _validate_release_main_checks(text: str) -> None:
    blocks = re.findall(
        r"^ {10}for check in \\\n(?P<checks>.*?)^ {10}do\s*$",
        text,
        flags=re.DOTALL | re.MULTILINE,
    )
    if len(blocks) != 1:
        raise WorkflowPolicyError(
            "Release main check set must contain exactly 8 CI checks and 3 CodeQL analyses"
        )
    try:
        checks = tuple(shlex.split(blocks[0].replace("\\\n", " ")))
    except ValueError as error:
        raise WorkflowPolicyError(
            "Release main check set must contain exactly 8 CI checks and 3 CodeQL analyses"
        ) from error
    if (
        len(checks) != len(RELEASE_REQUIRED_MAIN_CHECKS)
        or frozenset(checks) != RELEASE_REQUIRED_MAIN_CHECKS
    ):
        raise WorkflowPolicyError(
            "Release main check set must contain exactly 8 CI checks and 3 CodeQL analyses"
        )


def _validate_release(directory: Path) -> None:
    path = directory / RELEASE_WORKFLOW
    if not path.is_file():
        raise WorkflowPolicyError("the production Release workflow is required")
    text = path.read_text(encoding="utf-8")
    if _top_level_triggers(text) != ("workflow_dispatch",):
        raise WorkflowPolicyError("Release must use exactly workflow_dispatch")
    if _permission_blocks(text) != (
        (),
        (
            ("attestations", "write"),
            ("checks", "read"),
            ("contents", "read"),
            ("id-token", "write"),
        ),
        (("attestations", "read"), ("contents", "read"), ("id-token", "write")),
        (("actions", "read"), ("contents", "read"), ("id-token", "write")),
    ):
        raise WorkflowPolicyError(
            "Release permissions are not the canonical plan/deploy/cleanup split"
        )
    _validate_release_main_checks(text)
    required = (
        "name: Production Release",
        "group: production-release",
        "cancel-in-progress: false",
        "runs-on: ubuntu-24.04-arm",
        'node-version: "24.18.0"',
        'EXPECTED_REPOSITORY_ID: "1302516701"',
        ".use_immutable_subject == true",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "repository_owner_id",
        "'Analyze (python)'",
        "map({Status, TagKey, Type})",
        "signing-profiles/shittim_chest_ecr$",
        "tools/wait_release_image_evidence.sh",
        "vars.AWS_RELEASE_PLAN_ROLE_ARN",
        "vars.AWS_RELEASE_DEPLOY_ROLE_ARN",
        "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6",
        "create-storage-record: false",
        "target: production",
        "target: break-glass",
        'SOURCE_DATE_EPOCH: "0"',
        "tools/install_aws_signer_notation.sh",
        "tools/install_ecr_credential_helper.sh",
        'AWS_ECR_DISABLE_CACHE: "true"',
        "notation verify",
        "describe-images",
        "list-image-referrers",
        "--bundle-from-oci",
        "--deny-self-hosted-runners",
        "--signer-digest",
        "tools/check_container_risk_acceptance.py",
        "--image-config-digest-file",
        "--config-digest-only",
        "Validate release image configs before push",
        "Push the prevalidated images once",
        "docker image inspect",
        "docker image push --quiet",
        "aws ecr batch-get-image",
        'test "${actual_config}" = "${expected_config}"',
        "steps.push-images.outputs.normal_digest",
        "steps.push-images.outputs.break_glass_digest",
        "--normal-config-digest-file",
        "--break-glass-config-digest-file",
        "Prepare pinned vulnerability data before image push",
        "create-cdk-assets",
        "bind-cdk-asset-checksums",
        "validate-cdk-assets",
        "publish-assets",
        "--unstable=publish-assets",
        "--exclusively",
        "--force",
        "jq --compact-output '.files[]'",
        ".s3_checksum_sha256",
        "--checksum-mode ENABLED",
        "Fail fast on unstable stacks, stale plans, and unavailable AWS APIs",
        "Recover stale unexecuted release change sets before planning",
        "--stale-before-plan",
        "aws ssm describe-parameters",
        "/discord/moderator/public-key",
        "aws cloudformation describe-events --generate-cli-skeleton input",
        "Remove this failed plan's unexecuted change sets",
        "Remove this release's unexecuted change sets",
        "tools/cleanup_release_change_sets.sh",
        "Capture bounded CloudFormation failure diagnostics",
        "tools.control_records validate",
        "tools.control_records guard",
        "--lock-seconds 3600",
        "validate-change-set",
        "grep --fixed-strings '(ValidationError)'",
        "REVIEW_IN_PROGRESS) type=CREATE",
        "REVIEW_IN_PROGRESS) create_stack=true",
        "tools/release_supply_chain.py create-manifest",
        "--if-none-match '*'",
        "head-object",
        "environment: production",
        "tools.control_records acquire",
        "tools.control_records release",
        "execute-change-set",
        "describe-task-definition",
        "if: always() && steps.acquire.outputs.acquired == 'true'",
        "evidence_name: ${{ steps.evidence.outputs.artifact_name }}",
        "plan_attempt: ${{ steps.evidence.outputs.run_attempt }}",
        "name: ${{ steps.evidence.outputs.artifact_name }}",
        "name: ${{ needs.plan.outputs.evidence_name }}",
        "if: ${{ needs.plan.result == 'success' && "
        "fromJSON(needs.plan.outputs.plan_attempt) == github.run_attempt }}",
        "needs: [plan, deploy]",
        "Acquire plan-role cleanup credentials",
        "Confirm this release has no unexecuted change sets",
        "EVIDENCE_RESULT: ${{ steps.cleanup_evidence.outcome }}",
        "continue-on-error: true",
        '[[ ! "${PLAN_ATTEMPT}" =~ ^[1-9][0-9]*$ ]]',
        'change_set_name="release-${GITHUB_SHA}-${GITHUB_RUN_ID}-${PLAN_ATTEMPT}"',
        "--attempt-name",
        "A failed-jobs-only rerun cannot reuse an earlier release plan",
        'contains(fromJSON(\'["success","failure","cancelled"]\'), steps.prepare_changes.outcome)',
        "guard_id=$(uv run --frozen python -c 'import uuid; print(uuid.uuid7())')",
        "retention-days: 90",
    )
    if 'guard_id="release-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"' in text:
        raise WorkflowPolicyError("Release deployment lock must use a canonical UUIDv7 guard ID")
    for marker in required:
        if marker not in text:
            raise WorkflowPolicyError(f"Release lacks required policy marker: {marker}")
    forbidden_same_sha_config_markers = (
        "Resolve the successful same-SHA main CI run",
        "Download the same-SHA CI image evidence",
        "main-ci-image/production-image-config-digest.txt",
        "main-ci-image/break-glass-image-config-digest.txt",
        'test "${NORMAL_CONFIG_DIGEST}" = "${ci_normal_config}"',
        'test "${BREAK_GLASS_CONFIG_DIGEST}" = "${ci_break_glass_config}"',
    )
    if any(marker in text for marker in forbidden_same_sha_config_markers):
        raise WorkflowPolicyError(
            "Release must not require a cross-run same-SHA config digest comparison"
        )
    if text.count("name: ${{ needs.plan.outputs.evidence_name }}") != 2:
        raise WorkflowPolicyError(
            "Release deploy and cleanup must consume the exact planned artifact"
        )
    if text.count('SOURCE_DATE_EPOCH: "0"') != 2:
        raise WorkflowPolicyError(
            "Release must make both image builds reproducible with the Unix epoch"
        )
    bundle_checksum_conversion = (
        "bundle_code_sha256=$(printf '%s' \"${bundle_hash}\" | xxd -r -p | base64 -w 0)"
    )
    if (
        text.count(bundle_checksum_conversion) != 2
        or text.count('"ParameterKey=LambdaBundleCodeSha256,ParameterValue=${bundle_code_sha256}"')
        != 1
        or text.count('--expected-parameter "LambdaBundleCodeSha256=${bundle_code_sha256}"') != 2
    ):
        raise WorkflowPolicyError(
            "Release must bind the exact Lambda bundle checksum to the published version"
        )
    if (
        text.count("PYTHONDONTWRITEBYTECODE") != 1
        or '  PYTHONDONTWRITEBYTECODE: "1"' not in text[: text.index("\njobs:")]
    ):
        raise WorkflowPolicyError(
            "Release pytest must inherit PYTHONDONTWRITEBYTECODE=1 from workflow env"
        )
    reproducible_docker_exporter = (
        "outputs: type=docker,rewrite-timestamp=true,compression=gzip,"
        "compression-level=6,force-compression=true"
    )
    if text.count(reproducible_docker_exporter) != 2:
        raise WorkflowPolicyError(
            "Release must use the CI-identical deterministic Docker exporter for both images"
        )
    if "outputs: type=registry" in text:
        raise WorkflowPolicyError(
            "Release must not rebuild risk-bound images with another exporter"
        )
    required_cache_scopes = (
        "cache-from: type=gha,scope=container-arm64-production",
        "cache-to: type=gha,mode=max,scope=container-arm64-production,ignore-error=true",
        "cache-from: type=gha,scope=container-arm64-break-glass",
        "cache-to: type=gha,mode=max,scope=container-arm64-break-glass,ignore-error=true",
    )
    if any(text.count(marker) != 1 for marker in required_cache_scopes):
        raise WorkflowPolicyError("Release must reuse the exact main CI image cache scopes")
    if text.count("--config-digest-only") != 2:
        raise WorkflowPolicyError("Release must validate both local configs before either push")
    release_risk_block = _workflow_step_block(
        text, "Apply signed vendor VEX and the time-bounded risk policy"
    )
    required_fixable_gate = (
        'GRYPE_DB_AUTO_UPDATE: "false"',
        "for mode in normal break-glass",
        '--name "${mode}-image-actionable"',
        "--only-fixed",
        "--fail-on high || fixable_status=$?",
        'if [ "${fixable_status}" -ne 0 ]',
        'exit "${fixable_status}"',
    )
    if any(marker not in release_risk_block for marker in required_fixable_gate):
        raise WorkflowPolicyError(
            "Release must gate both rebuilt images on fixable High/Critical findings"
        )
    if text.count("docker image push --quiet") != 2:
        raise WorkflowPolicyError("Release must push each prevalidated loaded image exactly once")
    if re.search(r"steps\.build-(?:normal|break-glass)\.outputs\.digest", text):
        raise WorkflowPolicyError("Release must use only registry-confirmed manifest digests")
    if (
        text.count("steps.push-images.outputs.normal_digest") != 4
        or text.count("steps.push-images.outputs.break_glass_digest") != 4
    ):
        raise WorkflowPolicyError(
            "Release manifest, evidence, and attestations must share registry-confirmed digests"
        )
    if text.count("environment: production") != 1:
        raise WorkflowPolicyError("Release must use production Environment only for deploy")
    if re.search(r"secrets\.AWS[A-Z0-9_]*|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY", text):
        raise WorkflowPolicyError("Release must not use static AWS credentials")
    if re.search(r"runs-on:\s*self-hosted|\b(?:git\s+push|gh\s+pr\s+merge)\b", text):
        raise WorkflowPolicyError("Release contains a forbidden runner or repository mutation")
    if re.search(r"\bnotation\s+login\b", text):
        raise WorkflowPolicyError("Release must use the ECR credential helper for Notation")
    if text.count("aws ecr get-login-password") != 2:
        raise WorkflowPolicyError(
            "Release must authenticate exactly two OCI client contexts to ECR"
        )
    _validate_release_referrer_delta(text)
    _validate_release_attestation_summary(text)
    if text.count("${RUNNER_TEMP}/release/cdk.out/${artifact}.template.json") != 2:
        raise WorkflowPolicyError(
            "Release must revalidate downloaded CDK templates at their preserved artifact paths"
        )
    if text.count('--slurpfile actual "${RUNNER_TEMP}/${mode}.verification.json"') != 1:
        raise WorkflowPolicyError(
            "Release must load the regenerated image verification document before comparison"
        )
    deploy_reverification = _workflow_step_block(
        text, "Reverify AWS evidence and immutable change sets"
    )
    forbidden_scan_comparisons = (
        ".images[$key].scan == $actual[0].scan",
        ".images[$key].scan.scanned_at == $actual[0].scan.scanned_at",
    )
    if any(marker in deploy_reverification for marker in forbidden_scan_comparisons):
        raise WorkflowPolicyError(
            "Release deploy must not compare mutable scan evidence across verification times"
        )
    normalized_scan_comparison = (
        "(.images[$key].scan | del(.scanned_at)) ==\n"
        "                 ($actual[0].scan | del(.scanned_at)) and"
    )
    if deploy_reverification.count(normalized_scan_comparison) != 1:
        raise WorkflowPolicyError(
            "Release deploy must compare all current scan evidence except its mutable timestamp"
        )
    try:
        synth_index = text.index("name: Synthesize and publish the complete CDK asset closure")
        publish_index = text.index("npm run cdk -- publish-assets")
        tool_install_index = text.index("name: Install pinned Syft, Grype, and Docker Scout")
        notation_install_index = text.index(
            "name: Install and cryptographically verify AWS Signer Notation"
        )
        helper_install_index = text.index(
            "name: Install and configure the pinned ECR credential helper"
        )
        vulnerability_data_index = text.index(
            "name: Prepare pinned vulnerability data before image push"
        )
        build_index = text.index("name: Build and load the production image once")
        config_preflight_index = text.index("name: Validate release image configs before push")
        push_index = text.index("name: Push the prevalidated images once")
        change_set_index = text.index("name: Prepare immutable CloudFormation change sets")
    except ValueError as error:
        raise WorkflowPolicyError("Release CDK asset publication steps are incomplete") from error
    if not (
        synth_index
        < publish_index
        < tool_install_index
        < notation_install_index
        < helper_install_index
        < vulnerability_data_index
        < build_index
        < config_preflight_index
        < push_index
        < change_set_index
    ):
        raise WorkflowPolicyError(
            "Release must finish asset and verifier preflights before image build and change sets"
        )
    try:
        image_checkout_index = text.index("name: Check out the immutable image build context")
        image_context_check_index = text.index(
            "name: Require the immutable clean image build context"
        )
        buildx_index = text.index("name: Set up Buildx", vulnerability_data_index)
        break_glass_build_index = text.index(
            "name: Build and load the isolated break-glass image once"
        )
    except ValueError as error:
        raise WorkflowPolicyError("Release immutable image build context is incomplete") from error
    if not (
        vulnerability_data_index
        < image_checkout_index
        < image_context_check_index
        < buildx_index
        < build_index
        < break_glass_build_index
        < config_preflight_index
    ):
        raise WorkflowPolicyError(
            "Release must create and verify the immutable image context after all pre-build gates"
        )
    image_checkout_block = _workflow_step_block(text, "Check out the immutable image build context")
    required_checkout_markers = (
        "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "ref: ${{ github.sha }}",
        "path: release-image-context",
        "persist-credentials: false",
    )
    if any(marker not in image_checkout_block for marker in required_checkout_markers):
        raise WorkflowPolicyError(
            "Release immutable image checkout must pin github.sha without persisted credentials"
        )
    image_context_check_block = _workflow_step_block(
        text, "Require the immutable clean image build context"
    )
    required_context_check_markers = (
        'git -C "${RELEASE_IMAGE_CONTEXT}" rev-parse HEAD',
        '"${GITHUB_SHA}"',
        'git -C "${RELEASE_IMAGE_CONTEXT}" rev-parse --show-toplevel',
        'git -C "${RELEASE_IMAGE_CONTEXT}" status',
        "--porcelain=v1 --untracked-files=all",
    )
    if any(marker not in image_context_check_block for marker in required_context_check_markers):
        raise WorkflowPolicyError(
            "Release must verify the dedicated image checkout SHA and clean worktree"
        )
    context_env = "${{ env.RELEASE_IMAGE_CONTEXT }}"
    if (
        text.count("  RELEASE_IMAGE_CONTEXT: ${{ github.workspace }}/release-image-context") != 1
        or f"working-directory: {context_env}" in text
    ):
        raise WorkflowPolicyError(
            "Release must reserve one dedicated image context exclusively for Docker builds"
        )
    production_build_block = _workflow_step_block(text, "Build and load the production image once")
    break_glass_build_block = _workflow_step_block(
        text, "Build and load the isolated break-glass image once"
    )
    expected_context = f"context: {context_env}"
    expected_dockerfile = f"file: {context_env}/Dockerfile"
    for block in (production_build_block, break_glass_build_block):
        if (
            block.count(expected_context) != 1
            or block.count(expected_dockerfile) != 1
            or re.search(r"(?m)^\s+context:\s*\.\s*$", block)
        ):
            raise WorkflowPolicyError(
                "Release production and break-glass builds must share the immutable image context"
            )
        if "BUILDKIT_MULTI_PLATFORM" in block:
            raise WorkflowPolicyError(
                "Release Docker image exports must not request a manifest-list result"
            )
    if production_build_block.count("no-cache-filters: builder,runtime-base") != 1:
        raise WorkflowPolicyError(
            "Release production build must regenerate the builder snapshot and final runtime stage"
        )
    if break_glass_build_block.count("no-cache-filters: builder,break-glass") != 1:
        raise WorkflowPolicyError(
            "Release break-glass build must regenerate the builder snapshot and final stage"
        )
    immutable_context_region = text[image_checkout_index:build_index]
    forbidden_context_gate = re.compile(
        r"\b(?:pytest|uv\s+build|npm\s+run\s+check:infra|cdk\s+synth)\b"
    )
    if forbidden_context_gate.search(immutable_context_region):
        raise WorkflowPolicyError(
            "Release must not run test, synth, or package gates after the image checkout"
        )
    publish_end = text.find("\n      - name:", publish_index)
    publish_block = text[publish_index : None if publish_end == -1 else publish_end]
    required_publish_markers = (
        "Stateful Runtime Operations CostGovernance",
        '--app "${RUNNER_TEMP}/cdk.out"',
        "--exclusively",
        "--unstable=publish-assets",
        "--force",
        "--ci",
    )
    if any(marker not in publish_block for marker in required_publish_markers):
        raise WorkflowPolicyError("Release CDK asset publisher is incomplete")
    if text.count("tools/release_supply_chain.py validate-cdk-assets") != 2:
        raise WorkflowPolicyError(
            "Release must revalidate CDK assets before and after AWS approval"
        )
    try:
        create_change_set_index = text.index("arn=$(aws cloudformation create-change-set")
        record_change_set_index = text.index(
            'jq --arg stack "${stack}" --arg arn "${arn}"',
            create_change_set_index,
        )
        poll_change_set_index = text.index("for attempt in $(seq 1 60)", create_change_set_index)
    except ValueError as error:
        raise WorkflowPolicyError("Release change set recording is incomplete") from error
    if not create_change_set_index < record_change_set_index < poll_change_set_index:
        raise WorkflowPolicyError("Release must record each change set before polling it")
    prepare_block = _workflow_step_block(text, "Prepare immutable CloudFormation change sets")
    failure_markers = (
        '"${RUNNER_TEMP}/${artifact}.failed-change-set.raw.json"',
        '"${RUNNER_TEMP}/${artifact}.change-set-failure.json"',
        'status_reason: ((.StatusReason // "") | scrub)',
        "jq --compact-output .",
        "reason=$(jq --raw-output .status_reason",
    )
    if any(marker not in prepare_block for marker in failure_markers):
        raise WorkflowPolicyError(
            "Release must retain a bounded Change Set failure reason before cleanup"
        )
    failed_status_index = prepare_block.index('if [ "${status}" = "FAILED" ]')
    evidence_index = prepare_block.index(
        '"${RUNNER_TEMP}/${artifact}.change-set-failure.json"',
        failed_status_index,
    )
    exit_index = prepare_block.index("*) exit 1 ;;", failed_status_index)
    if not failed_status_index < evidence_index < exit_index:
        raise WorkflowPolicyError("Release must record Change Set failure evidence before exiting")
    failure_upload = _workflow_step_block(text, "Retain bounded Change Set failure evidence")
    required_failure_upload = (
        "failure()",
        "steps.prepare_changes.outcome == 'failure'",
        "*.change-set-failure.json",
        "if-no-files-found: error",
    )
    if any(marker not in failure_upload for marker in required_failure_upload):
        raise WorkflowPolicyError(
            "Release must upload bounded Change Set failure evidence before cleanup"
        )
    if (
        text.count('change_set_name="release-${GITHUB_SHA}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"')
        != 4
    ):
        raise WorkflowPolicyError(
            "Release recovery, plan, and both partial-plan cleanup paths must use "
            "only this run's exact name"
        )
    cleanup_call = "bash tools/cleanup_release_change_sets.sh"
    plan_job_start = text.index("\n  plan:\n")
    deploy_job_start = text.index("\n  deploy:\n")
    plan_job = text[plan_job_start:deploy_job_start]
    stale_cleanup_start = plan_job.index(
        "name: Recover stale unexecuted release change sets before planning"
    )
    fail_fast_start = plan_job.index(
        "name: Fail fast on unstable stacks, stale plans, and unavailable AWS APIs"
    )
    stale_cleanup = plan_job[stale_cleanup_start:fail_fast_start]
    if (
        cleanup_call not in stale_cleanup
        or "--stale-before-plan" not in stale_cleanup
        or 'change_set_name="release-${GITHUB_SHA}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"'
        not in stale_cleanup
    ):
        raise WorkflowPolicyError("Release stale-plan recovery is not bound to this run")
    plan_cleanup_start = plan_job.index("name: Remove this failed plan's unexecuted change sets")
    plan_cleanup = plan_job[plan_cleanup_start:]
    required_plan_cleanup = (
        "always()",
        "steps.plan_aws.outcome == 'success'",
        "steps.prepare_changes.outcome",
        "job.status != 'success'",
        cleanup_call,
        "--change-set-name",
    )
    if any(marker not in plan_cleanup for marker in required_plan_cleanup):
        raise WorkflowPolicyError("Release partial-plan cleanup is not fail-safe")
    deploy_cleanup_start = text.index("name: Remove this release's unexecuted change sets")
    deploy_cleanup_end = text.index("name: Release the exact deployment fence")
    deploy_cleanup = text[deploy_cleanup_start:deploy_cleanup_end]
    cleanup_job_start = text.index("\n  cleanup:\n")
    cleanup_job = text[cleanup_job_start:]
    if (
        cleanup_call not in deploy_cleanup
        or cleanup_call not in cleanup_job
        or "--manifest" not in deploy_cleanup
        or "--manifest" not in cleanup_job
    ):
        raise WorkflowPolicyError(
            "Release cleanup must use the attested change set ARN across failed-job reruns"
        )
    if "GITHUB_RUN_ATTEMPT" in deploy_cleanup or "change_set_name=" in deploy_cleanup:
        raise WorkflowPolicyError("Deploy cleanup must not reconstruct a rerun change set name")
    diagnostics_start = text.index("name: Capture bounded CloudFormation failure diagnostics")
    diagnostics_end = text.index("name: Remove this release's unexecuted change sets")
    diagnostics = text[diagnostics_start:diagnostics_end]
    if "--change-set-name" in diagnostics:
        raise WorkflowPolicyError(
            "Release failure diagnostics must query the surviving stack, not an executed change set"
        )
    required_diagnostic_call = (
        "aws cloudformation describe-events",
        '--stack-name "${stack}"',
        "--filters FailedEvents=true",
        "--max-items 100",
    )
    if any(marker not in diagnostics for marker in required_diagnostic_call):
        raise WorkflowPolicyError(
            "Release failure diagnostics must preserve the DescribeEvents call shape "
            "for the surviving stack"
        )
    required_diagnostic_isolation = (
        "id: failure_diagnostics",
        "if: failure() && steps.acquire.outputs.acquired == 'true'",
        "continue-on-error: true",
    )
    if any(marker not in diagnostics for marker in required_diagnostic_isolation):
        raise WorkflowPolicyError(
            "Release diagnostic failure must remain distinct from the original deploy failure"
        )
    cleanup_checkout_end = cleanup_job.index(
        "name: Download the exact planned release evidence for cleanup"
    )
    cleanup_checkout = cleanup_job[:cleanup_checkout_end]
    required_cleanup_checkout = (
        "if: ${{ always() }}",
        "actions/checkout@",
        "ref: ${{ github.sha }}",
        "persist-credentials: false",
    )
    if any(marker not in cleanup_checkout for marker in required_cleanup_checkout):
        raise WorkflowPolicyError("Release independent cleanup is not always available")
    required_independent_cleanup = (
        "if: ${{ needs.plan.result == 'success' }}",
        "id: cleanup_evidence",
        "continue-on-error: true",
        "PLAN_RESULT: ${{ needs.plan.result }}",
        "EVIDENCE_RESULT: ${{ steps.cleanup_evidence.outcome }}",
        'if [ "${PLAN_RESULT}" = success ] && [ "${EVIDENCE_RESULT}" = success ]',
        '[[ ! "${PLAN_ATTEMPT}" =~ ^[1-9][0-9]*$ ]]',
        'change_set_name="release-${GITHUB_SHA}-${GITHUB_RUN_ID}-${PLAN_ATTEMPT}"',
        "--manifest",
        "--attempt-name",
        "--change-set-name",
        "Release evidence download failed",
        "Release planning did not succeed",
        "DEPLOY_RESULT: ${{ needs.deploy.result }}",
        'if [ "${DEPLOY_RESULT}" != success ]',
        "Deployment did not succeed. Change sets were cleaned",
    )
    if any(marker not in cleanup_job for marker in required_independent_cleanup):
        raise WorkflowPolicyError(
            "Release cleanup must preserve the original plan or deploy failure"
        )
    try:
        planned_name = cleanup_job.index(
            'change_set_name="release-${GITHUB_SHA}-${GITHUB_RUN_ID}-${PLAN_ATTEMPT}"'
        )
        attempt_cleanup = cleanup_job.index("--attempt-name", planned_name)
        current_name = cleanup_job.index(
            'change_set_name="release-${GITHUB_SHA}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"',
            attempt_cleanup,
        )
        partial_cleanup = cleanup_job.index("--change-set-name", current_name)
    except ValueError as error:
        raise WorkflowPolicyError("Release cleanup fallback identity is incomplete") from error
    if not planned_name < attempt_cleanup < current_name < partial_cleanup:
        raise WorkflowPolicyError("Release cleanup fallback is not bound to the planned attempt")
    rerun_rejection = cleanup_job.index('if [ "${PLAN_ATTEMPT}" != "${GITHUB_RUN_ATTEMPT}" ]')
    if (
        cleanup_job[:rerun_rejection].count(cleanup_call) != 3
        or cleanup_job.count(cleanup_call) != 3
    ):
        raise WorkflowPolicyError("Release cleanup must precede failed-rerun rejection")
    secrets = set(re.findall(r"secrets\.([A-Z0-9_]+)", text))
    if secrets != {"DHI_TOKEN", "DHI_USERNAME", "OPERATOR_NOTIFICATION_EMAIL"}:
        raise WorkflowPolicyError("Release secret allowlist changed")
    _require_full_action_pins(text, "Release")


def _validate_release_referrer_delta(text: str) -> None:
    if text.count("aws ecr list-image-referrers") != 3:
        raise WorkflowPolicyError(
            "Release must capture pre-attestation, post-attestation, and deploy referrers"
        )
    if "--no-paginate" in text or "--max-results" in text:
        raise WorkflowPolicyError("Release referrer snapshots must use complete AWS pagination")
    if (
        text.count("select-release-referrers") != 2
        or text.count("--before-referrers") != 2
        or text.count("--after-referrers") != 2
        or text.count("--notation-inspection") != 2
        or text.count('--referrers "${RUNNER_TEMP}/${mode}.referrers.json"') != 2
    ):
        raise WorkflowPolicyError(
            "Release must pass only the selected current-run referrers to verify-image"
        )

    try:
        baseline = _workflow_step_block(
            text, "Capture ACTIVE referrer baselines before attestations"
        )
        plan_verify = _workflow_step_block(
            text, "Strictly verify both Signer identities and four referrers"
        )
        deploy_identity = _workflow_step_block(
            text, "Cryptographically reverify both image identities"
        )
        deploy_verify = _workflow_step_block(
            text, "Reverify AWS evidence and immutable change sets"
        )
    except ValueError as error:
        raise WorkflowPolicyError("Release referrer delta steps are incomplete") from error
    required_baseline = (
        "aws ecr list-image-referrers",
        '"${RUNNER_TEMP}/${mode}.referrers-before.json"',
    )
    required_plan = (
        '"${RUNNER_TEMP}/${mode}.referrers-after.json"',
        "select-release-referrers",
        '--before-referrers "${RUNNER_TEMP}/${mode}.referrers-before.json"',
        '--after-referrers "${RUNNER_TEMP}/${mode}.referrers-after.json"',
        '--notation-inspection "${RUNNER_TEMP}/${mode}.notation.json"',
        '--profile-arn "${SIGNING_PROFILE_ARN}"',
        '--output "${RUNNER_TEMP}/${mode}.referrers.json"',
        '--referrers "${RUNNER_TEMP}/${mode}.referrers.json"',
    )
    required_deploy_identity = (
        'notation inspect --output json "${reference}"',
        '> "${RUNNER_TEMP}/${mode}.notation.json"',
    )
    required_deploy = (
        '"${RUNNER_TEMP}/${mode}.referrers-current.json"',
        "select-release-referrers",
        '"${RUNNER_TEMP}/release/${mode}.referrers-before.json"',
        '--after-referrers "${RUNNER_TEMP}/${mode}.referrers-current.json"',
        '--notation-inspection "${RUNNER_TEMP}/${mode}.notation.json"',
        '--profile-arn "${SIGNING_PROFILE_ARN}"',
        '--output "${RUNNER_TEMP}/${mode}.referrers.json"',
        '--referrers "${RUNNER_TEMP}/${mode}.referrers.json"',
    )
    if any(marker not in baseline for marker in required_baseline):
        raise WorkflowPolicyError("Release pre-attestation referrer baseline is incomplete")
    if any(marker not in plan_verify for marker in required_plan):
        raise WorkflowPolicyError("Release plan referrer delta verification is incomplete")
    if any(marker not in deploy_identity for marker in required_deploy_identity):
        raise WorkflowPolicyError("Release deploy Notation identity inspection is incomplete")
    if any(marker not in deploy_verify for marker in required_deploy):
        raise WorkflowPolicyError("Release deploy referrer delta verification is incomplete")

    try:
        wait_index = text.index("name: Wait for managed signing and enhanced ECR scans")
        baseline_index = text.index("name: Capture ACTIVE referrer baselines before attestations")
        first_attestation_index = text.index("name: Attest normal image provenance")
        last_attestation_index = text.index("name: Attest break-glass vulnerability assessment")
        verify_index = text.index("name: Strictly verify both Signer identities and four referrers")
    except ValueError as error:
        raise WorkflowPolicyError("Release referrer delta step order is incomplete") from error
    if not (
        wait_index
        < baseline_index
        < first_attestation_index
        < last_attestation_index
        < verify_index
    ):
        raise WorkflowPolicyError(
            "Release must capture referrers before creating and verifying attestations"
        )


def _validate_release_attestation_summary(text: str) -> None:
    action = "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6"
    attestation_steps = (
        ("Attest normal image provenance", "attest_normal_provenance"),
        ("Attest normal image SBOM", "attest_normal_sbom"),
        ("Attest normal image vulnerability assessment", "attest_normal_vulnerability"),
        ("Attest break-glass image provenance", "attest_break_glass_provenance"),
        ("Attest break-glass image SBOM", "attest_break_glass_sbom"),
        ("Attest break-glass vulnerability assessment", "attest_break_glass_vulnerability"),
        ("Attest the release manifest", "attest_release_manifest"),
    )
    if text.count(f"uses: {action}") != len(attestation_steps):
        raise WorkflowPolicyError("Release must create exactly seven attestations")
    try:
        for name, step_id in attestation_steps:
            block = _workflow_step_block(text, name)
            if f"id: {step_id}" not in block or "show-summary: false" not in block:
                raise WorkflowPolicyError(
                    "Release attestation actions must defer to the canonical summary"
                )
        summary = _workflow_step_block(text, "Record canonical attestation links")
    except ValueError as error:
        raise WorkflowPolicyError("Release canonical attestation summary is incomplete") from error

    if "https://github.com/pitekusu/shittim-chest/attestations/" in summary:
        raise WorkflowPolicyError("Release attestation summary URL is vulnerable to owner masking")
    required_summary_markers = (
        "steps.attest_normal_provenance.outputs.attestation-id",
        "steps.attest_normal_sbom.outputs.attestation-id",
        "steps.attest_normal_vulnerability.outputs.attestation-id",
        "steps.attest_break_glass_provenance.outputs.attestation-id",
        "steps.attest_break_glass_sbom.outputs.attestation-id",
        "steps.attest_break_glass_vulnerability.outputs.attestation-id",
        "steps.attest_release_manifest.outputs.attestation-id",
        '[[ ! "${attestation_id}" =~ ^[0-9]+$ ]]',
        "https://github.com/pitek&#117;su/shittim-chest/attestations/",
        "https://github.com/pitek%75su/shittim-chest/attestations/",
        '>> "${GITHUB_STEP_SUMMARY}"',
    )
    if any(marker not in summary for marker in required_summary_markers):
        raise WorkflowPolicyError("Release canonical attestation summary is incomplete")
    if summary.count('write_attestation_link "') != len(attestation_steps):
        raise WorkflowPolicyError("Release must summarize every created attestation exactly once")


def _validate_ci_container_risk(directory: Path) -> None:
    path = directory / "ci.yml"
    if not path.is_file():
        raise WorkflowPolicyError("CI workflow is required")
    text = path.read_text(encoding="utf-8")
    required = (
        "name: Build and load the production image",
        "name: Build and load the break-glass image for risk validation",
        "steps.build-production.outputs.imageid",
        "steps.build-break-glass.outputs.imageid",
        "break-glass-image-sbom-arm64.spdx.json",
        "dhi-python-builder.openvex.json",
        "--image-kind production",
        "--image-kind break-glass",
        "--image-config-digest-file",
    )
    for marker in required:
        if marker not in text:
            raise WorkflowPolicyError(f"CI container risk gate lacks required marker: {marker}")
    if text.count('SOURCE_DATE_EPOCH: "0"') != 3:
        raise WorkflowPolicyError(
            "CI must make production, fault, and break-glass image builds reproducible"
        )
    reproducible_docker_exporter = (
        "outputs: type=docker,rewrite-timestamp=true,compression=gzip,"
        "compression-level=6,force-compression=true"
    )
    if text.count(reproducible_docker_exporter) != 3:
        raise WorkflowPolicyError(
            "CI must deterministically compress all three timestamp-normalized image exports"
        )
    production_build_block = _workflow_step_block(text, "Build and load the production image")
    fault_build_block = _workflow_step_block(text, "Build and load the CI-only fault image")
    break_glass_build_block = _workflow_step_block(
        text, "Build and load the break-glass image for risk validation"
    )
    for block in (production_build_block, fault_build_block, break_glass_build_block):
        if "BUILDKIT_MULTI_PLATFORM" in block:
            raise WorkflowPolicyError(
                "CI Docker image exports must not request a manifest-list result"
            )
    rootfs_evidence = (
        "production-image-rootfs-diffids.json",
        "break-glass-image-rootfs-diffids.json",
    )
    if any(text.count(name) != 2 for name in rootfs_evidence):
        raise WorkflowPolicyError(
            "CI must record and retain both risk-bound image rootfs diff ID lists"
        )
    if production_build_block.count("no-cache-filters: builder,runtime-base") != 1:
        raise WorkflowPolicyError(
            "CI production build must regenerate the builder snapshot and final runtime stage"
        )
    if break_glass_build_block.count("no-cache-filters: builder,break-glass") != 1:
        raise WorkflowPolicyError(
            "CI break-glass build must regenerate the builder snapshot and final stage"
        )
    try:
        buildx_index = text.index("name: Set up Docker Buildx")
        proof_index = text.index("name: Prove Docker ignores generated source bytecode")
        production_index = text.index("name: Build and load the production image")
    except ValueError as error:
        raise WorkflowPolicyError("CI Docker context bytecode proof is required") from error
    if not buildx_index < proof_index < production_index:
        raise WorkflowPolicyError(
            "CI Docker context bytecode proof must precede the production image build"
        )
    proof_block = _workflow_step_block(text, "Prove Docker ignores generated source bytecode")
    proof_markers = (
        'git archive --format=tar "${GITHUB_SHA}" src',
        "python3 -m py_compile src/shittim_chest/__init__.py",
        "'FROM scratch' 'COPY src /src'",
        "docker buildx build",
        '--file "${proof_root}/Dockerfile"',
        '--output "type=local,dest=${proof_root}/actual"',
        'diff --recursive --brief "${proof_root}/clean/src" "${proof_root}/actual/src"',
        "__pycache__",
        "*.py[cod]",
        "\n            .\n",
    )
    if any(marker not in proof_block for marker in proof_markers) or "--ignorefile" in proof_block:
        raise WorkflowPolicyError(
            "CI Docker context proof must compare clean src with actual .dockerignore output"
        )


def _validate_ci_path_isolation(directory: Path) -> None:
    ci_text = (directory / "ci.yml").read_text(encoding="utf-8")
    records_path = directory / RECORDS_CI_WORKFLOW
    if not records_path.is_file():
        raise WorkflowPolicyError("Records CI workflow is required")
    records_text = records_path.read_text(encoding="utf-8")
    _require_full_action_pins(records_text, "Records CI")

    for workflow, text in (("ci.yml", ci_text), (RECORDS_CI_WORKFLOW, records_text)):
        if text.count("python3 tools/classify_ci_paths.py") != 1:
            raise WorkflowPolicyError(f"{workflow} must use the canonical path classifier once")

    records_triggers = _top_level_triggers(records_text)
    if records_triggers != ("pull_request", "push", "workflow_dispatch"):
        raise WorkflowPolicyError(
            "Records CI must always create its required result for PR, main, and manual runs"
        )
    trigger_end = records_text.index("\npermissions:")
    if re.search(r"(?m)^\s{4,}(?:paths|paths-ignore):", records_text[:trigger_end]):
        raise WorkflowPolicyError("Records CI triggers must not use path filters")

    runtime_condition = "if: needs.changes.outputs.runtime_container == 'true'"
    for job in ("container-arm64-build", "grype-build"):
        block = _workflow_job_block(ci_text, job)
        if block.count(runtime_condition) != 1:
            raise WorkflowPolicyError(
                f"CI {job} must run only for canonical Runtime image or risk inputs"
            )

    runtime_gates = {
        "container-arm64": (
            "- container-arm64-build",
            "BUILD_RESULT: ${{ needs.container-arm64-build.result }}",
        ),
        "grype": (
            "- grype-build",
            "SCAN_RESULT: ${{ needs.grype-build.result }}",
        ),
    }
    for job, markers in runtime_gates.items():
        block = _workflow_job_block(ci_text, job)
        required = (
            "if: always()",
            "- changes",
            "CHANGES_RESULT: ${{ needs.changes.result }}",
            "REQUIRED: ${{ needs.changes.outputs.runtime_container }}",
            'test "${CHANGES_RESULT}" = success',
            'if [ "${REQUIRED}" = true ]',
            'elif [ "${REQUIRED}" = false ]',
            "= success",
            "= skipped",
            *markers,
        )
        if any(marker not in block for marker in required):
            raise WorkflowPolicyError(
                f"CI {job} must preserve one required result around its conditional heavy job"
            )

    records_condition = "if: needs.records-changes.outputs.records == 'true'"
    for job in ("records-python", "records-contract", "records-web", "records-infra"):
        block = _workflow_job_block(records_text, job)
        if block.count(records_condition) != 1:
            raise WorkflowPolicyError(f"Records CI {job} must use the canonical path decision")

    records_python = _workflow_job_block(records_text, "records-python")
    required_records_audit = (
        "uv export --quiet --frozen --all-groups --no-emit-local --no-annotate",
        "records-audit-requirements.txt",
        "uv run --frozen pip-audit --strict --require-hashes",
    )
    if any(marker not in records_python for marker in required_records_audit):
        raise WorkflowPolicyError("Records CI must audit the frozen Records Python lock")
    required_records_dynamodb = (
        "tools/run_dynamodb_local.py",
        "tests/test_dynamodb_integration.py",
    )
    if any(marker not in records_python for marker in required_records_dynamodb):
        raise WorkflowPolicyError("Records CI must run the pinned DynamoDB Local integration test")

    records_web = _workflow_job_block(records_text, "records-web")
    if "voidzero-dev/setup-vp@" in records_web:
        raise WorkflowPolicyError(
            "Records CI must use the allowlisted GitHub-owned Node setup action"
        )
    required_records_web = (
        "uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0",
        "corepack enable pnpm",
        'test "$(pnpm --version)" = "11.21.0"',
        "pnpm install --frozen-lockfile",
        "pnpm exec vp check",
        "pnpm exec vp test",
        "pnpm exec vp build",
        "pnpm audit --audit-level=low",
    )
    if any(marker not in records_web for marker in required_records_web):
        raise WorkflowPolicyError("Records CI must use the pinned pnpm and Vite+ toolchain")
    if "npm ci" in records_web or "package-lock.json" in records_web:
        raise WorkflowPolicyError("Records CI must not fall back to the retired npm lock")

    records_gate = _workflow_job_block(records_text, "records-gate")
    required_records_gate = (
        "if: always()",
        "- records-changes",
        "- records-python",
        "- records-contract",
        "- records-web",
        "- records-infra",
        "CHANGES_RESULT: ${{ needs.records-changes.result }}",
        "REQUIRED: ${{ needs.records-changes.outputs.records }}",
        "INFRA_RESULT: ${{ needs.records-infra.result }}",
        'test "${CHANGES_RESULT}" = success',
        'elif [ "${REQUIRED}" = false ]',
        'test "${PYTHON_RESULT}" = success',
        'test "${CONTRACT_RESULT}" = success',
        'test "${WEB_RESULT}" = success',
        'test "${INFRA_RESULT}" = success',
        'test "${PYTHON_RESULT}" = skipped',
        'test "${CONTRACT_RESULT}" = skipped',
        'test "${WEB_RESULT}" = skipped',
        'test "${INFRA_RESULT}" = skipped',
    )
    if any(marker not in records_gate for marker in required_records_gate):
        raise WorkflowPolicyError(
            "Records CI must preserve one required result around all conditional Records jobs"
        )


def _validate_records_workflows(directory: Path) -> None:
    release_path = directory / RECORDS_RELEASE_WORKFLOW
    backfill_path = directory / RECORDS_BACKFILL_WORKFLOW
    if not release_path.is_file() or not backfill_path.is_file():
        raise WorkflowPolicyError("Records release and backfill workflows are required")
    release = release_path.read_text(encoding="utf-8")
    backfill = backfill_path.read_text(encoding="utf-8")
    _require_full_action_pins(release, "Records Release")
    _require_full_action_pins(backfill, "Records Backfill")
    if _top_level_triggers(release) != ("workflow_dispatch",):
        raise WorkflowPolicyError("Records Release must use exactly workflow_dispatch")
    if _top_level_triggers(backfill) != ("workflow_dispatch",):
        raise WorkflowPolicyError("Records Backfill must use exactly workflow_dispatch")
    if "secrets." in release or "secrets." in backfill:
        raise WorkflowPolicyError("Records workflows must consume only pre-registered handles")

    install_step = _workflow_step_block(release, "Install frozen build environments")
    app_local_pnpm = (
        "          (\n"
        "            cd apps/records-web\n"
        "            corepack enable pnpm\n"
        '            test "$(pnpm --version)" = "11.21.0"\n'
        "            pnpm install --frozen-lockfile\n"
        "          )"
    )
    if install_step.count(app_local_pnpm) != 1:
        raise WorkflowPolicyError(
            "Records Release must resolve pinned pnpm from the Records Web package boundary"
        )
    if "pnpm --dir apps/records-web" in install_step:
        raise WorkflowPolicyError("Records Release must not resolve pnpm from the repository root")

    release_gate_step = _workflow_step_block(release, "Re-run Records release-critical gates")
    app_local_web_gates = (
        "          (\n"
        "            cd apps/records-web\n"
        "            pnpm exec vp check\n"
        "            pnpm exec vp test\n"
        "            pnpm exec vp build\n"
        "            pnpm audit --audit-level=low\n"
        "          )"
    )
    if release_gate_step.count(app_local_web_gates) != 1:
        raise WorkflowPolicyError(
            "Records Release must run all web gates from the Records Web package boundary"
        )
    if "pnpm --dir apps/records-web" in release_gate_step:
        raise WorkflowPolicyError(
            "Records Release web gates must not resolve pnpm from the repository root"
        )

    executable_filter = (
        '| if type == "boolean" then tostring else error("executable must be boolean") end'
    )
    if release.count(executable_filter) != 2:
        raise WorkflowPolicyError(
            "Records Release must extract executable as a boolean-safe string"
        )

    safety_command = "tools/records_release_manifest.py validate-change-set-safety"
    edge_migration_markers = (
        "--include-property-values",
        '--expected-edge-hostname "${PUBLIC_HOSTNAME}"',
        '--expected-edge-zone-id "${HOSTED_ZONE_ID}"',
        '--expected-edge-zone-name "${HOSTED_ZONE_NAME}"',
    )
    if (
        release.count(safety_command) != 2
        or release.count('--logical-name "${logical_name}"') != 2
        or any(release.count(marker) != 1 for marker in edge_migration_markers)
    ):
        raise WorkflowPolicyError(
            "Records Release must scope replacements to immutable application resources "
            "and the exact edge alias migration"
        )
    change_set_calls = (
        "create_plan stateful ShittimChest-Prod-RecordsStateful",
        "create_plan application ShittimChest-Prod-RecordsApplication",
        "create_plan edge ShittimChest-Prod-RecordsEdge",
    )
    if any(release.count(call) != 1 for call in change_set_calls):
        raise WorkflowPolicyError("Records Release must propagate each create_plan safety failure")
    plan_step = _workflow_step_block(release, "Create and validate the three Records Change Sets")
    if (
        "          set -e\n          create_plan stateful" not in plan_step
        or "|| true" in plan_step
    ):
        raise WorkflowPolicyError("Records Release must propagate each create_plan safety failure")
    stack_status_markers = (
        'error("expected exactly one stack")',
        ".[0].StackStatus",
        '""|REVIEW_IN_PROGRESS) type=CREATE ;;',
    )
    if "DeletionTime" in plan_step:
        raise WorkflowPolicyError("Records Release must not infer stack absence from DeletionTime")
    if "REVIEW_IN_PROGRESS|ROLLBACK_COMPLETE) type=CREATE ;;" in plan_step:
        raise WorkflowPolicyError("Records Release must reject an active ROLLBACK_COMPLETE stack")
    if any(marker not in plan_step for marker in stack_status_markers):
        raise WorkflowPolicyError("Records Release must classify a named stack by StackStatus")

    release_markers = (
        "name: Records Release",
        "group: production-release",
        "runs-on: ubuntu-24.04-arm",
        "source_stream_arn:",
        "records-gate",
        "gh api --paginate --slurp",
        "npm run synth:records",
        "tools/build_records_bundle.py",
        "tools/build_records_web_artifact.py",
        "--format cyclonedx1.5",
        "vars.AWS_RECORDS_PLAN_ROLE_ARN",
        "vars.AWS_RECORDS_DEPLOY_ROLE_ARN",
        "Verify Records deployment account and parameter metadata",
        "aws sts get-caller-identity --query Account --output text",
        'test "${stream_account}" = "${account}"',
        "aws ssm describe-parameters",
        "/shittim-chest/production/records/identity-hmac-key",
        "/shittim-chest/production/records/presentation/v0001",
        "/shittim-chest/production/records/discord/oauth/v0001",
        "/shittim-chest/production/records/discord/client-secret",
        "/shittim-chest/production/records/session-key",
        "/shittim-chest/production/records/openai/admin-key",
        "/shittim-chest/production/records/openai/project-id",
        "/shittim-chest/production/records/admin/discord-user-id",
        '.[0].Name == $name and .[0].Type == "SecureString" and',
        '.[0].Tier == "Standard" and (.[0].Version | type == "number" and . >= 1)',
        "records-release-${{ github.run_id }}-${{ github.run_attempt }}-stateful",
        "records-release-${{ github.run_id }}-${{ github.run_attempt }}-application",
        "records-release-${{ github.run_id }}-${{ github.run_attempt }}-edge",
        "ShittimChest-Prod-RecordsStateful",
        "ShittimChest-Prod-RecordsApplication",
        "ShittimChest-Prod-RecordsEdge",
        "RecordsPublicHostname",
        "RecordsHostedZoneId",
        "--include-property-values",
        "--expected-edge-hostname",
        "--expected-edge-zone-id",
        "--expected-edge-zone-name",
        "RecordsApiOriginDomain",
        "RecordsMediaOriginDomain",
        "records-web.zip",
        "web_artifact_sha256",
        "records-web-sbom.cdx.json",
        "web_sbom_sha256",
        "cloudfront create-invalidation",
        "Restore the previous Records entry point after a post-publish failure",
        "records-previous-index-version",
        "s3api copy-object",
        "s3api delete-object",
        "tools/records_release_manifest.py create-entry",
        "tools/records_release_manifest.py create-manifest",
        "tools/records_release_manifest.py validate-manifest",
        "--signer-workflow pitekusu/shittim-chest/.github/workflows/records-release.yml",
        "--source-ref refs/heads/main",
        '--type "${type}"',
        'if [ "${executable}" = false ]',
        "Remove attested no-op Records Change Sets before approval",
        "uses: actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6 # v4",
        "environment: production",
        "Execute only the attested Records Change Sets",
        'case "${type}" in',
        "stack-create-complete",
        "stack-update-complete",
        "cloudformation update-termination-protection",
        "--enable-termination-protection",
        "EnableTerminationProtection",
        ".Stacks[0].EnableTerminationProtection == true",
        "Clean up only unexecuted Records Change Sets",
        "Confirm this Records release has no unexecuted Change Sets",
        "RecordsBundleCodeSha256",
        "RuntimeImageDigest",
        "BreakGlassImageDigest",
        "RecordsDistributionId",
        "RecordsCertificateArn",
        'ParameterKey=RuntimeImageDigest,ParameterValue="${runtime_image_digest}"',
        'ParameterKey=BreakGlassImageDigest,ParameterValue="${break_glass_image_digest}"',
        'ParameterKey=RecordsDistributionId,ParameterValue="${records_distribution_id}"',
        'ParameterKey=RecordsCertificateArn,ParameterValue="${records_certificate_arn}"',
        "bundle_code_sha256=$(printf '%s' \"${bundle_hash}\" | xxd -r -p | base64 -w0)",
        '--expected-parameter "RecordsBundleCodeSha256=${bundle_code_sha256}"',
        "Verify anonymous and protected Records API boundaries",
        '"${endpoint}/api/v1/session"',
        '"${endpoint}/api/v1/records"',
        '"${endpoint}/api/v1/admin/status"',
        ".isAdmin == false",
        '.error.code == "AUTHENTICATION_REQUIRED"',
    )
    if any(marker not in release for marker in release_markers):
        raise WorkflowPolicyError("Records Release is missing its immutable plan/deploy boundary")
    if (
        release.count(
            "--signer-workflow pitekusu/shittim-chest/.github/workflows/records-release.yml"
        )
        != 2
        or release.count("--source-ref refs/heads/main") != 2
    ):
        raise WorkflowPolicyError("Records Release is missing its immutable plan/deploy boundary")
    if release.count('--expected-parameter "RecordsBundleCodeSha256=') != 2:
        raise WorkflowPolicyError(
            "Records Release must verify the Lambda code checksum during plan and deploy"
        )
    if release.count('--type "${type}"') != 2:
        raise WorkflowPolicyError("Records Release must attest and revalidate each Change Set type")
    deploy_block = _workflow_job_block(release, "deploy")
    if deploy_block.count("environment: production") != 1:
        raise WorkflowPolicyError("Records deploy requires one production Environment approval")
    if "ChangeSetType" in release:
        raise WorkflowPolicyError("Records Release must use only the attested Change Set type")
    if release.index("execute stateful") >= release.index("execute application"):
        raise WorkflowPolicyError("Records Release must preserve Stateful before Application")
    if release.index("execute application") >= release.index("execute edge"):
        raise WorkflowPolicyError("Records Release must preserve Application before Edge")
    execute_start = release.index("          execute() {")
    execute_end = release.index("          manifest=", execute_start)
    execute_step = release[execute_start:execute_end]
    if 'if [ "${executable}" = false ]; then\n              return 0' in execute_step:
        raise WorkflowPolicyError(
            "Records Release must protect stable no-op stacks before continuing"
        )
    if execute_step.index("cloudformation update-termination-protection") <= execute_step.index(
        "cloudformation execute-change-set"
    ):
        raise WorkflowPolicyError(
            "Records Release must enable termination protection after execution"
        )
    if release.count(".Stacks[0].EnableTerminationProtection == true") != 2:
        raise WorkflowPolicyError(
            "Records Release must verify termination protection during and after deployment"
        )
    stable_noop_statuses = (
        "CREATE_COMPLETE|UPDATE_COMPLETE|UPDATE_ROLLBACK_COMPLETE|IMPORT_COMPLETE) ;;"
    )
    if release.count(stable_noop_statuses) != 2:
        raise WorkflowPolicyError(
            "Records Release must preserve all stable statuses accepted by its no-op plan"
        )
    if "docker build" in release or "docker push" in release:
        raise WorkflowPolicyError("Records Release must not build or push a Fargate image")
    web_publish_block = _workflow_step_block(release, "Publish the attested Records Web artifact")
    asset_upload = 'aws s3 cp "${web_stage}/assets" "s3://${web_bucket}/assets"'
    entry_upload = 'aws s3 cp "${web_stage}/index.html" "s3://${web_bucket}/index.html"'
    if (
        web_publish_block.count(asset_upload) != 1
        or web_publish_block.count(entry_upload) != 1
        or web_publish_block.count("public,max-age=31536000,immutable") != 1
        or web_publish_block.index(asset_upload) >= web_publish_block.index(entry_upload)
        or "--delete" in web_publish_block
    ):
        raise WorkflowPolicyError(
            "Records Release must publish immutable assets before index without deleting old hashes"
        )

    backfill_markers = (
        "name: Records Backfill",
        "group: records-backfill",
        "environment: production",
        "timeout-minutes: 60",
        "vars.AWS_RECORDS_BACKFILL_ROLE_ARN",
        "role-duration-seconds: 3600",
        'test "${MODE}" = dry-run || test "${MODE}" = apply',
        'test "${PAGE_LIMIT}" -ge 1',
        'test "${PAGE_LIMIT}" -le 100',
        'BACKFILL_MAX_PAGES: "25"',
        "lambda get-function-configuration",
        "lambda invoke",
        "shittim-chest-production-records-backfill",
        'keys == ["candidates","complete","mode","projected","skipped","validated"]',
        ".validated == .candidates",
        "for ((page = 1; page <= BACKFILL_MAX_PAGES; page++)); do",
        'test "${complete}" = true',
    )
    if any(marker not in backfill for marker in backfill_markers):
        raise WorkflowPolicyError("Records Backfill is not bounded and content-free")
    if "while " in backfill:
        raise WorkflowPolicyError("Records Backfill completion loop must remain bounded")


def _workflow_job_block(text: str, job_id: str) -> str:
    """Return one literal top-level workflow job for strict policy checks."""

    marker = f"  {job_id}:\n"
    start = text.index(marker)
    next_job = re.search(r"(?m)^  [a-zA-Z0-9_-]+:\s*$", text[start + len(marker) :])
    if next_job is None:
        return text[start:]
    end = start + len(marker) + next_job.start()
    return text[start:end]


def _workflow_step_block(text: str, name: str) -> str:
    """Return one literal top-level workflow step for strict policy checks."""

    marker = f"      - name: {name}"
    start = text.index(marker)
    end = text.find("\n      - name:", start + len(marker))
    return text[start : len(text) if end == -1 else end]


def _validate_drift(directory: Path) -> None:
    path = directory / DRIFT_WORKFLOW
    if not path.is_file():
        raise WorkflowPolicyError("the infrastructure Drift workflow is required")
    text = path.read_text(encoding="utf-8")
    if _top_level_triggers(text) != ("schedule", "workflow_dispatch"):
        raise WorkflowPolicyError("Drift must use only schedule and workflow_dispatch")
    if _permission_blocks(text) != (
        (),
        (("contents", "read"), ("id-token", "write"), ("issues", "write")),
    ):
        raise WorkflowPolicyError("Drift permissions are not canonical")
    required = (
        "name: Infrastructure Drift",
        "cancel-in-progress: false",
        "vars.AWS_RELEASE_DRIFT_ROLE_ARN",
        "detect-stack-drift",
        "describe-stack-drift-detection-status",
        "--label infrastructure-drift",
        "This workflow never remediates drift.",
    )
    for marker in required:
        if marker not in text:
            raise WorkflowPolicyError(f"Drift lacks required policy marker: {marker}")
    if re.search(
        r"secrets\.|environment:\s*production|execute-change-set|create-change-set|"
        r"\b(?:update|delete)-stack\b|\bcdk\s+deploy\b|runs-on:\s*self-hosted",
        text,
    ):
        raise WorkflowPolicyError("Drift contains mutation beyond its single Issue")
    _require_full_action_pins(text, "Drift")


def _require_full_action_pins(text: str, label: str) -> None:
    for action in re.findall(r"(?m)^\s*uses:\s*([^\s#]+)", text):
        _, separator, revision = action.rpartition("@")
        if not separator or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            raise WorkflowPolicyError(f"{label} action is not pinned to a full commit SHA")


def _validate_consistent_action_pins(directory: Path) -> None:
    """Require one commit for every repeated action version across workflows."""

    pins: dict[tuple[str, str], str] = {}
    pattern = re.compile(r"(?m)^\s*uses:\s*([^@\s#]+)@([0-9a-f]{40})\s+#\s*([^\s#]+)\s*$")
    for path in sorted((*directory.glob("*.yml"), *directory.glob("*.yaml"))):
        for action, revision, version in pattern.findall(path.read_text(encoding="utf-8")):
            key = (action, version)
            previous = pins.setdefault(key, revision)
            if previous != revision:
                raise WorkflowPolicyError(f"inconsistent action pin for {action} {version}")


def _validate_canonical_deploy_guard_permissions(text: str) -> None:
    """Require the only OIDC-capable workflow to use one literal permission block."""

    expected = ((), (("contents", "read"), ("id-token", "write")))
    if _permission_blocks(text) != expected:
        raise WorkflowPolicyError(
            "Deploy Guard must grant exactly canonical contents: read and id-token: write"
        )


def _contains_forbidden_non_guard_permissions(text: str) -> bool:
    """Allow only literal actions: read and categorically reserve id-token for Deploy Guard."""

    return any(
        permission == "id-token" or (permission == "actions" and access != "read")
        for block in _permission_blocks(text)
        for permission, access in block
    )


def _validate_permission_syntax(directory: Path) -> None:
    """Reject permission obfuscation before applying workflow-specific policy."""

    for path in sorted((*directory.glob("*.yml"), *directory.glob("*.yaml"))):
        try:
            _permission_blocks(path.read_text(encoding="utf-8"))
        except WorkflowPolicyError as error:
            raise WorkflowPolicyError(
                f"workflow {path.name} contains non-canonical permissions: {error}"
            ) from None


def _permission_blocks(text: str) -> tuple[tuple[tuple[str, str], ...], ...]:
    """Parse canonical GitHub permission mappings and reject YAML indirection."""

    uncommented = tuple(_strip_yaml_comment(line) for line in text.splitlines())
    normalized = tuple(_decode_yaml_hexadecimal_escapes(line) for line in uncommented)
    headers: list[tuple[int, int, str]] = []
    permission_key_count = 0
    for index, (raw, decoded) in enumerate(zip(uncommented, normalized, strict=True)):
        matches = PERMISSIONS_KEY.findall(decoded)
        permission_key_count += len(matches)
        if not matches:
            continue
        match = re.fullmatch(r"(?P<indent> *)permissions:(?P<tail>.*)", raw)
        if match is None or decoded != raw or len(matches) != 1:
            raise WorkflowPolicyError("permissions keys must use literal canonical YAML")
        headers.append((index, len(match.group("indent")), match.group("tail").strip()))
    if len(headers) != permission_key_count:
        raise WorkflowPolicyError("permissions mappings must not use duplicate syntax")

    blocks: list[tuple[tuple[str, str], ...]] = []
    for index, indentation, tail in headers:
        if tail == "{}":
            blocks.append(())
            continue
        if tail:
            raise WorkflowPolicyError("permissions mappings must not use shorthand or flow syntax")
        entries: list[tuple[str, str]] = []
        for raw in uncommented[index + 1 :]:
            if not raw.strip():
                continue
            nested_indentation = len(raw) - len(raw.lstrip())
            if nested_indentation <= indentation:
                break
            if nested_indentation != indentation + 2:
                raise WorkflowPolicyError("permission entries must use canonical indentation")
            match = re.fullmatch(
                rf" {{{indentation + 2}}}([a-z][a-z-]*): (read|write|none)",
                raw,
            )
            if match is None:
                raise WorkflowPolicyError("permission entries must use literal read/write/none")
            entry = (match.group(1), match.group(2))
            if any(existing[0] == entry[0] for existing in entries):
                raise WorkflowPolicyError("permission entries must not be duplicated")
            entries.append(entry)
        blocks.append(tuple(entries))
    return tuple(blocks)


def _decode_yaml_hexadecimal_escapes(value: str) -> str:
    def replacement(match: re.Match[str]) -> str:
        raw = next(group for group in match.groups() if group is not None)
        try:
            return chr(int(raw, 16))
        except ValueError:  # pragma: no cover - the regular expression proves hexadecimal input
            return match.group(0)

    return YAML_HEXADECIMAL_ESCAPE.sub(replacement, value)


def _strip_yaml_comment(line: str) -> str:
    """Remove YAML comments while retaining hashes inside quoted scalars."""

    single_quoted = False
    double_quoted = False
    escaped = False
    for index, character in enumerate(line):
        if double_quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                double_quoted = False
            continue
        if single_quoted:
            if character != "'":
                continue
            if index + 1 < len(line) and line[index + 1] == "'":
                continue
            single_quoted = False
            continue
        if character == '"':
            double_quoted = True
        elif character == "'":
            single_quoted = True
        elif character == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index].rstrip()
    return line.rstrip()


def _top_level_triggers(text: str) -> tuple[str, ...]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line == "on:":
            triggers: list[str] = []
            for nested in lines[index + 1 :]:
                if nested and not nested[0].isspace() and not nested.lstrip().startswith("#"):
                    break
                match = re.match(r"^  ([a-zA-Z_][a-zA-Z0-9_-]*):(?:\s|$)", nested)
                if match is not None:
                    triggers.append(match.group(1))
            return tuple(triggers)
        if line.startswith("on:"):
            return ()
    return ()


def _validate_workflow_run_allowlist(directory: Path) -> None:
    path = directory / WORKFLOW_RUN_NOTIFICATION
    if not path.is_file():
        raise WorkflowPolicyError("the workflow-run Discord notification is required")
    text = path.read_text(encoding="utf-8")
    for workflow in (
        "Production Deploy Guard",
        "Production Release",
        "Infrastructure Drift",
    ):
        marker = f"      - {workflow}"
        if text.count(marker) != 1:
            raise WorkflowPolicyError(f"{workflow} must be in the notification allowlist")
    if "pull-requests: read" not in text:
        raise WorkflowPolicyError("workflow-run PR metadata lookup requires pull-requests: read")


def _validate_vulnerability_alerts_permission(directory: Path) -> None:
    uses: list[tuple[str, str]] = []
    for path in sorted((*directory.glob("*.yml"), *directory.glob("*.yaml"))):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "vulnerability-alerts:" in line and not line.lstrip().startswith("#"):
                uses.append((path.name, line.strip()))
    expected = [("discord-security-digest.yml", "vulnerability-alerts: read")]
    if uses != expected:
        raise WorkflowPolicyError(
            "vulnerability-alerts is restricted to one read-only Security Digest permission"
        )


def main() -> int:
    try:
        count = validate_notification_workflows()
    except (OSError, UnicodeDecodeError, WorkflowPolicyError) as error:
        print(error, file=sys.stderr)
        return 1
    print(f"notification workflow policy is valid: {count} target workflow")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
