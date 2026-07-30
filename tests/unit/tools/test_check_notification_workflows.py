"""Negative policy tests for notification and Deploy Guard workflows."""

from __future__ import annotations

from pathlib import Path

import pytest
from tools.check_notification_workflows import (
    ALLOWED_TARGET_WORKFLOW,
    DEPLOY_GUARD_WORKFLOW,
    DRIFT_WORKFLOW,
    RELEASE_WORKFLOW,
    WORKFLOW_DIRECTORY,
    WORKFLOW_RUN_NOTIFICATION,
    WorkflowPolicyError,
    validate_notification_workflows,
)


def _workflow_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "workflows"
    directory.mkdir()
    source = WORKFLOW_DIRECTORY / ALLOWED_TARGET_WORKFLOW
    (directory / ALLOWED_TARGET_WORKFLOW).write_bytes(source.read_bytes())
    digest = WORKFLOW_DIRECTORY / "discord-security-digest.yml"
    (directory / digest.name).write_bytes(digest.read_bytes())
    guard = WORKFLOW_DIRECTORY / DEPLOY_GUARD_WORKFLOW
    (directory / guard.name).write_bytes(guard.read_bytes())
    release = WORKFLOW_DIRECTORY / RELEASE_WORKFLOW
    (directory / release.name).write_bytes(release.read_bytes())
    drift = WORKFLOW_DIRECTORY / DRIFT_WORKFLOW
    (directory / drift.name).write_bytes(drift.read_bytes())
    workflow_run = WORKFLOW_DIRECTORY / WORKFLOW_RUN_NOTIFICATION
    (directory / workflow_run.name).write_bytes(workflow_run.read_bytes())
    ci = WORKFLOW_DIRECTORY / "ci.yml"
    (directory / ci.name).write_bytes(ci.read_bytes())
    return directory


def _replace(directory: Path, old: str, new: str) -> None:
    path = directory / ALLOWED_TARGET_WORKFLOW
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def test_repository_target_workflow_is_accepted(tmp_path: Path) -> None:
    assert validate_notification_workflows(_workflow_directory(tmp_path)) == 1


def test_repeated_action_version_requires_one_commit_pin(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0",
            "actions/setup-node@0000000000000000000000000000000000000000 # v7.0.0",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="inconsistent action pin"):
        validate_notification_workflows(directory)


def test_release_requires_the_locked_node_version(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '          node-version: "24.18.0"',
            "          node-version-file: .node-version",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="lacks required policy marker"):
        validate_notification_workflows(directory)


def test_release_normalizes_cost_tag_metadata_before_comparison(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            ".CostAllocationTags | map({Status, TagKey, Type}) ==",
            ".CostAllocationTags ==",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="lacks required policy marker"):
        validate_notification_workflows(directory)


def test_release_requires_the_unversioned_signing_profile_arn(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "signing-profiles/shittim_chest_ecr$",
            "signing-profiles/shittim_chest_ecr/[A-Za-z0-9]{10}$",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="lacks required policy marker"):
        validate_notification_workflows(directory)


def test_release_fails_fast_when_the_enhanced_scan_query_is_denied(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '                echo "::error::enhanced ECR scan query failed" >&2\n',
            "",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="lacks required policy marker"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "login_command",
    [
        "notation login --username AWS registry.example",
        "aws ecr get-login-password --region ap-northeast-1",
    ],
)
def test_release_rejects_registry_login_tokens(tmp_path: Path, login_command: str) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '          notation verify "${NORMAL_REFERENCE}"',
            f'          {login_command}\n          notation verify "${{NORMAL_REFERENCE}}"',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="without login tokens"):
        validate_notification_workflows(directory)


def test_unapproved_target_workflow_is_rejected(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    (directory / "unsafe.yml").write_text(
        "name: unsafe\non:\n  pull_request_target:\npermissions: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(WorkflowPolicyError, match="restricted"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("contents: read", "contents: write", "contents: read|read-only"),
        ("ref: ${{ github.sha }}", "ref: ${{ github.event.pull_request.head.sha }}", "PR head"),
        (
            "run: python3 -m tools.github_discord_notifications pull-request",
            "uses: actions/download-artifact@0000000000000000000000000000000000000000",
            "artifact",
        ),
        (
            "run: python3 -m tools.github_discord_notifications pull-request",
            "uses: actions/cache@0000000000000000000000000000000000000000",
            "cache",
        ),
        (
            "run: python3 -m tools.github_discord_notifications pull-request",
            "run: echo ${{ github.event.pull_request.title }}",
            "untrusted event",
        ),
        ("runs-on: ubuntu-latest", "runs-on: self-hosted", "self-hosted"),
    ],
)
def test_forbidden_target_capability_is_rejected(
    tmp_path: Path,
    old: str,
    new: str,
    message: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    _replace(directory, old, new)
    with pytest.raises(WorkflowPolicyError, match=message):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("      contents: read", '      contents: "write"'),
        ("      pull-requests: read", "      pull-requests: 'write'"),
        ("    permissions:", "    permissions: &unsafe"),
    ],
)
def test_target_permission_obfuscation_is_rejected(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / ALLOWED_TARGET_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(old, new, 1),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match=r"non-canonical|read-only"):
        validate_notification_workflows(directory)


def test_additional_secret_is_rejected(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    _replace(
        directory,
        "DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}",
        "DISCORD_WEBHOOK_URL: ${{ secrets.EXTRA_SECRET }}",
    )
    with pytest.raises(WorkflowPolicyError, match="only DISCORD_WEBHOOK_URL"):
        validate_notification_workflows(directory)


def test_extra_checkout_without_trusted_ref_is_rejected(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    _replace(
        directory,
        "      - name: Notify pull-request lifecycle",
        "      - uses: actions/checkout@0000000000000000000000000000000000000000\n"
        "      - name: Notify pull-request lifecycle",
    )
    with pytest.raises(WorkflowPolicyError, match="every checkout"):
        validate_notification_workflows(directory)


def test_multiline_run_cannot_expand_pull_request_metadata(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    _replace(
        directory,
        "run: python3 -m tools.github_discord_notifications pull-request",
        "run: |\n          echo ${{ github.event.pull_request.title }}",
    )
    with pytest.raises(WorkflowPolicyError, match="untrusted event"):
        validate_notification_workflows(directory)


def test_vulnerability_alerts_permission_cannot_be_widened(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    digest = directory / "discord-security-digest.yml"
    digest.write_text(
        digest.read_text(encoding="utf-8").replace(
            "vulnerability-alerts: read", "vulnerability-alerts: write"
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkflowPolicyError, match="one read-only"):
        validate_notification_workflows(directory)


def test_vulnerability_alerts_permission_cannot_be_duplicated(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    extra = directory / "extra.yml"
    extra.write_text("permissions:\n  vulnerability-alerts: read\n", encoding="utf-8")
    with pytest.raises(WorkflowPolicyError, match="one read-only"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize("trigger", ["pull_request", "pull_request_target", "push", "schedule"])
def test_deploy_guard_rejects_automatic_triggers(tmp_path: Path, trigger: str) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / DEPLOY_GUARD_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "  workflow_dispatch:",
            f"  {trigger}:\n  workflow_dispatch:",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        WorkflowPolicyError,
        match=r"exactly the workflow_dispatch|restricted",
    ):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            "run: uv sync --frozen --all-groups",
            "run: cdk deploy --all",
            "deployment command",
        ),
        (
            "run: uv sync --frozen --all-groups",
            "run: aws ecs update-service --cluster production",
            "AWS CLI operation",
        ),
        ("runs-on: ubuntu-latest", "runs-on: self-hosted", "self-hosted"),
        (
            "    permissions:\n      contents: read",
            "    environment: production\n    permissions:\n      contents: read",
            "production environment",
        ),
    ],
)
def test_deploy_guard_rejects_deployment_capabilities(
    tmp_path: Path,
    old: str,
    new: str,
    message: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / DEPLOY_GUARD_WORKFLOW
    path.write_text(path.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
    with pytest.raises(WorkflowPolicyError, match=message):
        validate_notification_workflows(directory)


def test_deploy_guard_rejects_reason_expression_in_shell(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / DEPLOY_GUARD_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "uv run --frozen python -m tools.control_records guard",
            "echo ${{ inputs.break_glass_reason }}\n"
            "          uv run --frozen python -m tools.control_records guard",
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkflowPolicyError, match="through env"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "permission",
    ["actions", "checks", "contents", "packages", "pull-requests"],
)
def test_deploy_guard_rejects_every_other_write_permission(
    tmp_path: Path,
    permission: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / DEPLOY_GUARD_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "      id-token: write",
            f"      id-token: write\n      {permission}: write",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match=r"canonical|duplicated"):
        validate_notification_workflows(directory)


def test_deploy_guard_rejects_write_all_permissions(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / DEPLOY_GUARD_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "    permissions:",
            "    permissions: write-all\n    legacy-permissions:",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match=r"non-canonical|shorthand"):
        validate_notification_workflows(directory)


def test_deploy_guard_rejects_flow_style_extra_write_permission(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / DEPLOY_GUARD_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "    permissions:\n      contents: read\n      id-token: write",
            "    permissions: {contents: read, id-token: write, actions: write}",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match=r"non-canonical|shorthand"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "permission_block",
    [
        '    permissions:\n      contents: read\n      "id-token": write',
        "    permissions:\n      contents: read\n      'id-token': write",
        '    permissions:\n      contents: read\n      id-token: "write"',
        "    permissions:\n      contents: read\n      id-token: 'write'",
        "    permissions:\n      contents: read\n      id-token: !!str write",
        "    permissions:\n      contents: read\n      id-token: &oidc write",
        "    permissions:\n      contents: read\n      id-token: *oidc",
        "    permissions: {contents: read, id-token: write}",
        '    permissions:\n      contents: read\n      id-token: "wr\\u0069te"',
    ],
)
def test_deploy_guard_requires_literal_canonical_permission_block(
    tmp_path: Path,
    permission_block: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / DEPLOY_GUARD_WORKFLOW
    canonical = "    permissions:\n      contents: read\n      id-token: write"
    path.write_text(
        path.read_text(encoding="utf-8").replace(canonical, permission_block, 1),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "permissions",
    [
        "permissions:\n  id-token: read",
        'permissions:\n  "id-token": "write"',
        "permissions:\n  'id-token': 'write'",
        "permissions:\n  id-token: !!str write",
        "permissions:\n  id-token: &oidc write",
        "permission-value: &oidc write\npermissions:\n  id-token: *oidc",
        "permissions: {id-token: write}",
        'permissions:\n  "id\\u002dtoken": "wr\\u0069te"',
        'permissions:\n  actions: "write"',
        "permissions:\n  'actions': write",
        "permissions:\n  actions: !!str write",
        "permissions:\n  actions: &mode write",
        "permission-value: &mode write\npermissions:\n  actions: *mode",
        "permissions: {actions: read}",
        'permissions:\n  "act\\u0069ons": "wr\\u0069te"',
        "permission-key: &key actions\npermissions:\n  *key: write",
    ],
)
def test_non_guard_workflow_rejects_obfuscated_oidc_or_actions_permission(
    tmp_path: Path,
    permissions: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    (directory / "unsafe-permissions.yml").write_text(
        f"name: unsafe-permissions\non: [pull_request]\n{permissions}\njobs: {{}}\n",
        encoding="utf-8",
    )

    with pytest.raises(
        WorkflowPolicyError,
        match=r"non-canonical|AWS or deployment capability",
    ):
        validate_notification_workflows(directory)


def test_permission_like_comments_do_not_widen_capability(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    (directory / "comments-only.yml").write_text(
        "name: comments-only\non: [pull_request]\npermissions: {}\n"
        "# id-token: write\n# actions: write\njobs: {}\n",
        encoding="utf-8",
    )

    assert validate_notification_workflows(directory) == 1


@pytest.mark.parametrize(
    "capability",
    [
        "    permissions:\n      id-token: write\n",
        "    steps:\n"
        "      - uses: aws-actions/configure-aws-credentials@"
        "0000000000000000000000000000000000000000\n",
        "    steps:\n      - run: aws dynamodb put-item --table-name unsafe\n",
        "    steps:\n      - run: cdk deploy\n",
        "    environment: production\n",
        "    steps:\n      - env:\n          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET }}\n",
    ],
)
def test_pull_request_workflow_cannot_gain_aws_or_deploy_capability(
    tmp_path: Path,
    capability: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    unsafe = directory / "unsafe-pr.yml"
    unsafe.write_text(
        f"name: unsafe-pr\non:\n  pull_request:\npermissions: {{}}\njobs:\n  unsafe:\n{capability}",
        encoding="utf-8",
    )
    with pytest.raises(WorkflowPolicyError, match="AWS or deployment capability"):
        validate_notification_workflows(directory)


def test_inline_pull_request_trigger_cannot_bypass_aws_boundary(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    unsafe = directory / "unsafe-inline-pr.yml"
    unsafe.write_text(
        "name: unsafe-pr\non: [pull_request]\npermissions:\n  id-token: write\n",
        encoding="utf-8",
    )
    with pytest.raises(WorkflowPolicyError, match="AWS or deployment capability"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "trigger",
    [
        "on: {workflow_call: {}}",
        "on: [workflow_run]",
        "on:\n  repository_dispatch:",
        "on:\n  workflow_run:",
    ],
)
def test_indirect_or_flow_style_trigger_cannot_gain_aws_capability(
    tmp_path: Path,
    trigger: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    unsafe = directory / "unsafe-indirect.yml"
    unsafe.write_text(
        f"name: unsafe-indirect\n{trigger}\npermissions:\n  id-token: write\n",
        encoding="utf-8",
    )
    with pytest.raises(WorkflowPolicyError, match="AWS or deployment capability"):
        validate_notification_workflows(directory)


def test_deploy_guard_rejects_flow_style_trigger_list(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / DEPLOY_GUARD_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "on:\n",
            "on: [workflow_dispatch, push]\nlegacy-trigger-config:\n",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkflowPolicyError, match="exactly the workflow_dispatch"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "marker",
    [
        "          - security-investigation\n",
        '            --actor "${GITHUB_TRIGGERING_ACTOR}"\n',
        '            --audit-output "${RUNNER_TEMP}/deploy-guard-audit.json"\n',
        "          if-no-files-found: error\n",
    ],
)
def test_deploy_guard_requires_break_glass_policy_and_audit_artifact(
    tmp_path: Path,
    marker: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / DEPLOY_GUARD_WORKFLOW
    path.write_text(path.read_text(encoding="utf-8").replace(marker, ""), encoding="utf-8")
    with pytest.raises(WorkflowPolicyError, match="lacks required policy marker"):
        validate_notification_workflows(directory)


def test_notification_allowlist_must_include_deploy_guard(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / WORKFLOW_RUN_NOTIFICATION
    path.write_text(
        path.read_text(encoding="utf-8").replace("      - Production Deploy Guard\n", ""),
        encoding="utf-8",
    )
    with pytest.raises(WorkflowPolicyError, match="notification allowlist"):
        validate_notification_workflows(directory)
