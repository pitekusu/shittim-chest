# SPDX-License-Identifier: MIT
"""Enforce notification and production Deploy Guard workflow trust boundaries."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIRECTORY = REPOSITORY_ROOT / ".github" / "workflows"
ALLOWED_TARGET_WORKFLOW = "discord-repository-events.yml"
DEPLOY_GUARD_WORKFLOW = "production-deploy-guard.yml"
RELEASE_WORKFLOW = "release.yml"
DRIFT_WORKFLOW = "drift.yml"
WORKFLOW_RUN_NOTIFICATION = "discord-workflow-run.yml"
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
    _validate_drift(directory)
    _validate_aws_capability_boundary(directory)
    _validate_workflow_run_allowlist(directory)
    _validate_vulnerability_alerts_permission(directory)
    return 1


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
    approved = {DEPLOY_GUARD_WORKFLOW, RELEASE_WORKFLOW, DRIFT_WORKFLOW}
    for path in sorted((*directory.glob("*.yml"), *directory.glob("*.yaml"))):
        if path.name in approved:
            continue
        text = path.read_text(encoding="utf-8")
        if _contains_forbidden_non_guard_permissions(text) or AWS_OR_DEPLOY_CAPABILITY.search(text):
            raise WorkflowPolicyError(
                f"workflow {path.name} contains AWS or deployment capability outside Deploy Guard"
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
            ("actions", "read"),
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
        "actions/attest@f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6",
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
        "Require CI-identical image configs before push",
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
    if text.count("name: ${{ needs.plan.outputs.evidence_name }}") != 2:
        raise WorkflowPolicyError(
            "Release deploy and cleanup must consume the exact planned artifact"
        )
    if text.count('SOURCE_DATE_EPOCH: "0"') != 2:
        raise WorkflowPolicyError(
            "Release must make both image builds reproducible with the Unix epoch"
        )
    if (
        text.count("PYTHONDONTWRITEBYTECODE") != 1
        or '  PYTHONDONTWRITEBYTECODE: "1"' not in text[: text.index("\njobs:")]
    ):
        raise WorkflowPolicyError(
            "Release pytest must inherit PYTHONDONTWRITEBYTECODE=1 from workflow env"
        )
    if text.count("outputs: type=docker,rewrite-timestamp=true") != 2:
        raise WorkflowPolicyError(
            "Release must use the CI-identical Docker exporter for both loaded images"
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
        config_preflight_index = text.index("name: Require CI-identical image configs before push")
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
    if production_build_block.count("no-cache-filters: runtime-base") != 1:
        raise WorkflowPolicyError(
            "Release production build must regenerate the cache-sensitive runtime stage"
        )
    if break_glass_build_block.count("no-cache-filters: break-glass") != 1:
        raise WorkflowPolicyError(
            "Release break-glass build must regenerate the cache-sensitive final stage"
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
        '--output "${RUNNER_TEMP}/${mode}.referrers.json"',
        '--referrers "${RUNNER_TEMP}/${mode}.referrers.json"',
    )
    required_deploy = (
        '"${RUNNER_TEMP}/${mode}.referrers-current.json"',
        "select-release-referrers",
        '"${RUNNER_TEMP}/release/${mode}.referrers-before.json"',
        '--after-referrers "${RUNNER_TEMP}/${mode}.referrers-current.json"',
        '--output "${RUNNER_TEMP}/${mode}.referrers.json"',
        '--referrers "${RUNNER_TEMP}/${mode}.referrers.json"',
    )
    if any(marker not in baseline for marker in required_baseline):
        raise WorkflowPolicyError("Release pre-attestation referrer baseline is incomplete")
    if any(marker not in plan_verify for marker in required_plan):
        raise WorkflowPolicyError("Release plan referrer delta verification is incomplete")
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
    action = "actions/attest@f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6"
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
    if text.count("outputs: type=docker,rewrite-timestamp=true") != 3:
        raise WorkflowPolicyError(
            "CI must rewrite file timestamps for all three loaded image exports"
        )
    production_build_block = _workflow_step_block(text, "Build and load the production image")
    break_glass_build_block = _workflow_step_block(
        text, "Build and load the break-glass image for risk validation"
    )
    if production_build_block.count("no-cache-filters: runtime-base") != 1:
        raise WorkflowPolicyError(
            "CI production build must regenerate the cache-sensitive runtime stage"
        )
    if break_glass_build_block.count("no-cache-filters: break-glass") != 1:
        raise WorkflowPolicyError(
            "CI break-glass build must regenerate the cache-sensitive final stage"
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
