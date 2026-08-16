"""Negative policy tests for notification and Deploy Guard workflows."""

from __future__ import annotations

from pathlib import Path

import pytest
from tools.check_notification_workflows import (
    ALLOWED_TARGET_WORKFLOW,
    DEPLOY_GUARD_WORKFLOW,
    DRIFT_WORKFLOW,
    RECORDS_BACKFILL_WORKFLOW,
    RECORDS_CI_WORKFLOW,
    RECORDS_RELEASE_WORKFLOW,
    RELEASE_REQUIRED_MAIN_CHECKS,
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
    records_ci = WORKFLOW_DIRECTORY / RECORDS_CI_WORKFLOW
    (directory / records_ci.name).write_bytes(records_ci.read_bytes())
    records_release = WORKFLOW_DIRECTORY / RECORDS_RELEASE_WORKFLOW
    (directory / records_release.name).write_bytes(records_release.read_bytes())
    records_backfill = WORKFLOW_DIRECTORY / RECORDS_BACKFILL_WORKFLOW
    (directory / records_backfill.name).write_bytes(records_backfill.read_bytes())
    return directory


def _replace(directory: Path, old: str, new: str) -> None:
    path = directory / ALLOWED_TARGET_WORKFLOW
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def test_repository_target_workflow_is_accepted(tmp_path: Path) -> None:
    assert validate_notification_workflows(_workflow_directory(tmp_path)) == 1


def test_ci_requires_runtime_image_path_isolation(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / "ci.yml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "    if: needs.changes.outputs.runtime_container == 'true'\n",
            "    if: always()\n",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="run only for canonical Runtime"):
        validate_notification_workflows(directory)


def test_records_ci_rejects_trigger_path_filters(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_CI_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "  pull_request:\n",
            "  pull_request:\n    paths:\n      - 'apps/records-web/**'\n",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="must not use path filters"):
        validate_notification_workflows(directory)


def test_records_ci_requires_the_canonical_classifier_decision(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_CI_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "    if: needs.records-changes.outputs.records == 'true'\n",
            "    if: always()\n",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="canonical path decision"):
        validate_notification_workflows(directory)


def test_records_ci_gate_requires_the_classifier_job_to_succeed(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_CI_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "CHANGES_RESULT: ${{ needs.records-changes.result }}",
            "CHANGES_RESULT: ignored",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="preserve one required result"):
        validate_notification_workflows(directory)


def test_records_ci_requires_a_frozen_python_dependency_audit(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_CI_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "uv run --frozen pip-audit --strict --require-hashes",
            "echo audit-disabled",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="audit the frozen Records Python lock"):
        validate_notification_workflows(directory)


def test_records_ci_excludes_local_workspace_paths_from_hashed_audit(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_CI_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "--no-emit-local",
            "--no-emit-project",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="audit the frozen Records Python lock"):
        validate_notification_workflows(directory)


def test_records_ci_requires_the_pinned_pnpm_vite_plus_toolchain(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_CI_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "pnpm exec vp check",
            "npm run check",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match=r"pinned pnpm and Vite\+"):
        validate_notification_workflows(directory)


def test_records_ci_rejects_the_non_allowlisted_vite_plus_action(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_CI_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0",
            "voidzero-dev/setup-vp@313600b80b104eadebb9111787d37a2e83e014ca # v1.17.0",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="allowlisted GitHub-owned"):
        validate_notification_workflows(directory)


def test_records_release_requires_production_approval(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace("    environment: production\n", "", 1),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_requires_complete_check_run_pagination(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "gh api --paginate --slurp",
            "gh api",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_binds_the_stream_to_the_deployment_account(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'test "${stream_account}" = "${account}"',
            "true",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_requires_secure_parameter_metadata(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "aws ssm describe-parameters",
            "echo parameter-check-disabled",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_requires_a_path_only_attestation_signer(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "--signer-workflow pitekusu/shittim-chest/.github/workflows/records-release.yml",
            '--signer-workflow "${GITHUB_WORKFLOW_REF}"',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_binds_attestation_to_main_source_ref(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "--source-ref refs/heads/main",
            "--source-ref refs/heads/other",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_waits_for_the_attested_change_set_type(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '--type "${type}"',
            "--type UPDATE",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="attest and revalidate"):
        validate_notification_workflows(directory)


def test_records_release_scopes_conditional_replacement_to_cdk_metadata(
    tmp_path: Path,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '.ResourceType == "AWS::CDK::Metadata"',
            'true or .ResourceType == "AWS::CDK::Metadata"',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="conditional replacement"):
        validate_notification_workflows(directory)


def test_records_release_rejects_a_true_prefix_before_the_safety_predicate(
    tmp_path: Path,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "               all(.[];",
            "               true or all(.[];",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="conditional replacement"):
        validate_notification_workflows(directory)


def test_records_release_propagates_change_set_safety_failure(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'change-set.json" >/dev/null\n          }',
            'change-set.json" >/dev/null || true\n          }',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="conditional replacement"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "call_end",
    [
        '"${STATEFUL_CHANGE_SET}" "${stateful_key}"',
        'ParameterKey=RecordsBundleObjectVersion,ParameterValue="${BUNDLE_VERSION}"',
    ],
)
def test_records_release_propagates_each_create_plan_failure(
    tmp_path: Path,
    call_end: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            call_end,
            f"{call_end} || true",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="create_plan safety failure"):
        validate_notification_workflows(directory)


def test_records_release_rejects_true_cdk_metadata_replacement(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '.Replacement == "Conditional"',
            '.Replacement != "False"',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="conditional replacement"):
        validate_notification_workflows(directory)


def test_records_release_rejects_conditional_cdk_metadata_addition(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '(.Action == "Add" and\n                    (.Replacement // "False") == "False")',
            '(.Action == "Add" and .Replacement == "Conditional")',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="conditional replacement"):
        validate_notification_workflows(directory)


def test_records_release_rejects_runtime_change_set_type_lookup(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '            aws cloudformation execute-change-set --change-set-name "${arn}"\n',
            "            aws cloudformation describe-change-set --query ChangeSetType\n"
            '            aws cloudformation execute-change-set --change-set-name "${arn}"\n',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="attested Change Set type"):
        validate_notification_workflows(directory)


def test_records_release_requires_noop_cleanup_before_approval(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "Remove attested no-op Records Change Sets before approval",
            "Ignore attested no-op Records Change Sets",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_requires_boolean_safe_executable_extraction(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '| if type == "boolean" then tostring else error("executable must be boolean") end',
            "",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="boolean-safe"):
        validate_notification_workflows(directory)


def test_records_release_requires_final_unexecuted_change_set_check(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "Confirm this Records release has no unexecuted Change Sets",
            "Skip the final Records Change Set inventory",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_requires_termination_protection_update(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "aws cloudformation update-termination-protection",
            "aws cloudformation describe-stacks",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_verifies_termination_protection_twice(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            ".Stacks[0].EnableTerminationProtection == true",
            ".Stacks[0].EnableTerminationProtection != true",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="during and after deployment"):
        validate_notification_workflows(directory)


def test_records_release_protects_noop_stacks(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_RELEASE_WORKFLOW
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        '            if [ "${executable}" = true ]; then\n',
        '            if [ "${executable}" = false ]; then\n'
        "              return 0\n"
        "            fi\n"
        '            if [ "${executable}" = true ]; then\n',
        1,
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(WorkflowPolicyError, match="protect stable no-op stacks"):
        validate_notification_workflows(directory)


def test_records_release_accepts_all_attested_stable_noop_statuses(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "CREATE_COMPLETE|UPDATE_COMPLETE|UPDATE_ROLLBACK_COMPLETE|IMPORT_COMPLETE) ;;",
            "CREATE_COMPLETE|UPDATE_COMPLETE) ;;",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="all stable statuses"):
        validate_notification_workflows(directory)


def test_records_backfill_rejects_unbounded_page_limit(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_BACKFILL_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '          test "${PAGE_LIMIT}" -le 100\n',
            "          true\n",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="bounded and content-free"):
        validate_notification_workflows(directory)


def test_records_backfill_requires_every_candidate_to_be_validated(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_BACKFILL_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "             .validated == .candidates and\n",
            "             .validated <= .candidates and\n",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="bounded and content-free"):
        validate_notification_workflows(directory)


def test_records_backfill_rejects_a_widened_completion_page_bound(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_BACKFILL_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '  BACKFILL_MAX_PAGES: "25"\n',
            '  BACKFILL_MAX_PAGES: "250"\n',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="bounded and content-free"):
        validate_notification_workflows(directory)


def test_records_backfill_requires_terminal_completion(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_BACKFILL_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '          test "${complete}" = true\n',
            "          true\n",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="bounded and content-free"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("    timeout-minutes: 60\n", "    timeout-minutes: 20\n"),
        ("          role-duration-seconds: 3600\n", "          role-duration-seconds: 1200\n"),
    ],
)
def test_records_backfill_requires_the_bounded_run_time_budget(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RECORDS_BACKFILL_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(old, new, 1),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="bounded and content-free"):
        validate_notification_workflows(directory)


def test_runtime_required_gates_require_the_classifier_job_to_succeed(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / "ci.yml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "CHANGES_RESULT: ${{ needs.changes.result }}",
            "CHANGES_RESULT: ignored",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="preserve one required result"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    ("workflow", "old", "new"),
    [
        ("ci.yml", "version: v0.35.0", "version: latest"),
        (
            RELEASE_WORKFLOW,
            "image=moby/buildkit:v0.31.2@sha256:2f5adac4ecd194d9f8c10b7b5d7bceb5186853db1b26e5abd3a657af0b7e26ec",
            "image=moby/buildkit:buildx-stable-1",
        ),
    ],
)
def test_container_builds_require_pinned_buildx_and_buildkit(
    tmp_path: Path,
    workflow: str,
    old: str,
    new: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / workflow
    path.write_text(path.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")

    with pytest.raises(WorkflowPolicyError, match="Buildx client and BuildKit image digest"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize("workflow", ["ci.yml", RELEASE_WORKFLOW])
def test_container_builds_reject_an_extra_buildx_setup(
    tmp_path: Path,
    workflow: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / workflow
    text = path.read_text(encoding="utf-8")
    extra_step = (
        "      - name: Replace the active builder\n"
        "        uses: docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c\n"
        "        with:\n"
        "          version: latest\n"
    )
    path.write_text(f"{text.rstrip()}\n{extra_step}", encoding="utf-8")

    with pytest.raises(WorkflowPolicyError, match="Buildx client and BuildKit image digest"):
        validate_notification_workflows(directory)


def test_release_requires_pre_attestation_referrer_baselines(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "Capture ACTIVE referrer baselines before attestations",
            "Capture referrers after attestations",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="referrer"):
        validate_notification_workflows(directory)


def test_release_requires_current_run_referrer_selection(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "select-release-referrers",
            "verify-image",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="selected current-run referrers"):
        validate_notification_workflows(directory)


def test_release_binds_selected_referrers_to_notation_inspection(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '--notation-inspection "${RUNNER_TEMP}/${mode}.notation.json"',
            '--notation-inspection "${RUNNER_TEMP}/${mode}.referrers-after.json"',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="referrer delta verification"):
        validate_notification_workflows(directory)


def test_release_referrer_snapshots_must_keep_aws_pagination(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "aws ecr list-image-referrers \\",
            "aws ecr list-image-referrers --no-paginate \\",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="complete AWS pagination"):
        validate_notification_workflows(directory)


def test_release_attestations_defer_to_the_canonical_summary(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace("          show-summary: false\n", "", 1),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="canonical summary"):
        validate_notification_workflows(directory)


def test_release_attestation_summary_rejects_maskable_owner_url(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "https://github.com/pitek%75su/shittim-chest/attestations/",
            "https://github.com/pitekusu/shittim-chest/attestations/",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="owner masking"):
        validate_notification_workflows(directory)


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


@pytest.mark.parametrize("check_name", sorted(RELEASE_REQUIRED_MAIN_CHECKS))
def test_release_requires_every_main_check(
    tmp_path: Path,
    check_name: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    text = path.read_text(encoding="utf-8")
    start = text.index("          for check in \\\n")
    end = text.index("\n          do", start)
    block = text[start:end]
    token = f"'{check_name}'" if " " in check_name else check_name
    changed = block.replace(token, "", 1)
    assert changed != block
    path.write_text(text[:start] + changed + text[end:], encoding="utf-8")

    with pytest.raises(WorkflowPolicyError, match="exactly 8 CI checks and 3 CodeQL"):
        validate_notification_workflows(directory)


def test_release_rejects_an_extra_main_check(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "quality tests security package cdk docs-public-safety",
            "quality tests security package cdk unexpected docs-public-safety",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="exactly 8 CI checks and 3 CodeQL"):
        validate_notification_workflows(directory)


def test_release_requires_reproducible_registry_images(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '          SOURCE_DATE_EPOCH: "0"',
            '          SOURCE_DATE_EPOCH: "1"',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="reproducible"):
        validate_notification_workflows(directory)


def test_release_requires_ci_identical_docker_exporters(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "outputs: type=docker,rewrite-timestamp=true,compression=gzip,"
            "compression-level=6,force-compression=true",
            "outputs: type=registry,rewrite-timestamp=true,compression=gzip,"
            "compression-level=6,force-compression=true",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match=r"CI-identical|another exporter"):
        validate_notification_workflows(directory)


def test_release_image_builds_reject_the_mutated_workspace_context(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "context: ${{ env.RELEASE_IMAGE_CONTEXT }}",
            "context: .",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="share the immutable image context"):
        validate_notification_workflows(directory)


def test_release_image_checkout_is_pinned_to_github_sha(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    text = path.read_text(encoding="utf-8")
    start = text.index("      - name: Check out the immutable image build context")
    end = text.index("\n      - name:", start + 1)
    checkout = text[start:end].replace("ref: ${{ github.sha }}", "ref: main", 1)
    path.write_text(text[:start] + checkout + text[end:], encoding="utf-8")

    with pytest.raises(WorkflowPolicyError, match=r"pin github\.sha"):
        validate_notification_workflows(directory)


def test_release_images_share_one_dedicated_context(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    text = path.read_text(encoding="utf-8")
    start = text.index("      - name: Build and load the isolated break-glass image once")
    end = text.index("\n      - name:", start + 1)
    build = text[start:end].replace(
        "context: ${{ env.RELEASE_IMAGE_CONTEXT }}",
        "context: ${{ github.workspace }}/another-context",
        1,
    )
    path.write_text(text[:start] + build + text[end:], encoding="utf-8")

    with pytest.raises(WorkflowPolicyError, match="share the immutable image context"):
        validate_notification_workflows(directory)


def test_release_pytest_disables_checkout_bytecode_writes(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '  PYTHONDONTWRITEBYTECODE: "1"\n',
            "",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="PYTHONDONTWRITEBYTECODE"):
        validate_notification_workflows(directory)


def test_release_runs_no_gates_after_the_image_checkout(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "      - name: Set up Buildx\n",
            "      - name: Re-run an unsafe test in the image checkout\n"
            "        run: uv run --frozen pytest\n"
            "      - name: Set up Buildx\n",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="must not run test"):
        validate_notification_workflows(directory)


def test_release_reuses_the_exact_main_ci_image_cache_scopes(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "cache-from: type=gha,scope=container-arm64-production",
            "cache-from: type=gha,scope=release-production-arm64",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="exact main CI image cache"):
        validate_notification_workflows(directory)


def test_release_checks_both_config_digests_before_push(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace("--config-digest-only", "", 1),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="both local configs"):
        validate_notification_workflows(directory)


def test_release_gates_both_rebuilt_images_on_fixable_high_findings(
    tmp_path: Path,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "              --only-fixed \\\n",
            "",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="fixable High/Critical"):
        validate_notification_workflows(directory)


def test_release_rejects_cross_run_same_sha_config_comparison(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "      - name: Validate release image configs before push\n",
            "      - name: Resolve the successful same-SHA main CI run\n"
            "        run: echo forbidden\n"
            "      - name: Validate release image configs before push\n",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="cross-run same-SHA"):
        validate_notification_workflows(directory)


def test_release_attests_only_registry_confirmed_manifest_digests(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "steps.push-images.outputs.normal_digest",
            "steps.build-normal.outputs.digest",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="registry-confirmed"):
        validate_notification_workflows(directory)


def test_ci_requires_reproducible_production_and_break_glass_images(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / "ci.yml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '          SOURCE_DATE_EPOCH: "0"',
            '          SOURCE_DATE_EPOCH: "1"',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="reproducible"):
        validate_notification_workflows(directory)


def test_ci_requires_loaded_image_file_timestamp_rewrite(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / "ci.yml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "outputs: type=docker,rewrite-timestamp=true,compression=gzip,"
            "compression-level=6,force-compression=true",
            "load: true",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="deterministically compress"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize("workflow", ["ci.yml", RELEASE_WORKFLOW])
def test_image_builds_require_forced_canonical_compression(
    tmp_path: Path,
    workflow: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / workflow
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            ",force-compression=true",
            "",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match=r"deterministic|deterministically"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    ("workflow", "step_name"),
    [
        ("ci.yml", "Build and load the production image"),
        ("ci.yml", "Build and load the CI-only fault image"),
        ("ci.yml", "Build and load the break-glass image for risk validation"),
        (RELEASE_WORKFLOW, "Build and load the production image once"),
        (RELEASE_WORKFLOW, "Build and load the isolated break-glass image once"),
    ],
)
def test_docker_image_builds_reject_manifest_list_output(
    tmp_path: Path,
    workflow: str,
    step_name: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / workflow
    text = path.read_text(encoding="utf-8")
    start = text.index(f"name: {step_name}")
    end = text.find("\n      - name:", start + 1)
    assert end != -1
    block = text[start:end]
    unsafe = block.replace(
        "          target:",
        "          build-args: |\n            BUILDKIT_MULTI_PLATFORM=1\n          target:",
        1,
    )
    path.write_text(text[:start] + unsafe + text[end:], encoding="utf-8")

    with pytest.raises(WorkflowPolicyError, match="manifest-list"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "name",
    [
        "production-image-rootfs-diffids.json",
        "break-glass-image-rootfs-diffids.json",
    ],
)
def test_ci_requires_rootfs_diff_id_evidence(tmp_path: Path, name: str) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / "ci.yml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(name, "missing-rootfs-evidence.json"),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="rootfs diff ID"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize("filters", ["builder,runtime-base", "builder,break-glass"])
def test_ci_regenerates_cache_sensitive_final_image_stages(
    tmp_path: Path,
    filters: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / "ci.yml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            f"          no-cache-filters: {filters}\n",
            "",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match=r"builder snapshot|final"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize("filters", ["builder,runtime-base", "builder,break-glass"])
def test_release_regenerates_cache_sensitive_final_image_stages(
    tmp_path: Path,
    filters: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            f"          no-cache-filters: {filters}\n",
            "",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match=r"builder snapshot|final"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize("workflow", ["ci.yml", RELEASE_WORKFLOW])
@pytest.mark.parametrize("stage", ["runtime-base", "break-glass"])
def test_risk_bound_images_reject_cached_builder_snapshots(
    tmp_path: Path,
    workflow: str,
    stage: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / workflow
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            f"          no-cache-filters: builder,{stage}\n",
            f"          no-cache-filters: {stage}\n",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="builder snapshot"):
        validate_notification_workflows(directory)


def test_ci_requires_actual_docker_context_bytecode_proof(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / "ci.yml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "python3 -m py_compile src/shittim_chest/__init__.py",
            "true",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match=r"actual \.dockerignore output"):
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


def test_release_requires_the_fail_fast_image_evidence_waiter(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "tools/wait_release_image_evidence.sh",
            "",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="lacks required policy marker"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "marker",
    [
        '"ParameterKey=LambdaBundleCodeSha256,ParameterValue=${bundle_code_sha256}"',
        '--expected-parameter "LambdaBundleCodeSha256=${bundle_code_sha256}"',
        "bundle_code_sha256=$(printf '%s' \"${bundle_hash}\" | xxd -r -p | base64 -w 0)",
    ],
)
def test_release_binds_lambda_version_to_exact_bundle_checksum(
    tmp_path: Path,
    marker: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(marker, "", 1),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="exact Lambda bundle checksum"):
        validate_notification_workflows(directory)


def test_release_rejects_notation_login(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '          notation verify "${NORMAL_REFERENCE}"',
            "          notation login --username AWS registry.example\n"
            '          notation verify "${NORMAL_REFERENCE}"',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="for Notation"):
        validate_notification_workflows(directory)


def test_release_requires_two_ephemeral_ecr_logins(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '          aws ecr get-login-password --region "${AWS_REGION}" | docker login \\\n'
            '            --username AWS --password-stdin "${registry}"\n',
            "",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="exactly two OCI client contexts"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "marker",
    [
        "              grep --fixed-strings '(ValidationError)' \\\n",
        '              ""|REVIEW_IN_PROGRESS) type=CREATE ;;\n',
        "              REVIEW_IN_PROGRESS) create_stack=true ;;\n",
    ],
)
def test_release_uses_stack_status_for_change_set_execution(
    tmp_path: Path,
    marker: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(path.read_text(encoding="utf-8").replace(marker, ""), encoding="utf-8")

    with pytest.raises(WorkflowPolicyError, match="lacks required policy marker"):
        validate_notification_workflows(directory)


def test_release_revalidates_preserved_cdk_artifact_paths(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "${RUNNER_TEMP}/release/cdk.out/${artifact}.template.json",
            "${RUNNER_TEMP}/release/${artifact}.template.json",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="preserved artifact paths"):
        validate_notification_workflows(directory)


def test_release_loads_regenerated_image_verification_for_comparison(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '--slurpfile actual "${RUNNER_TEMP}/${mode}.verification.json"',
            "--slurpfile actual",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="image verification document"):
        validate_notification_workflows(directory)


def test_release_requires_all_current_scan_evidence_except_timestamp(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "($actual[0].scan | del(.scanned_at))",
            "($actual[0].scan | del(.scanned_at, .severity_counts))",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="all current scan evidence"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "mutable_comparison",
    [
        ".images[$key].scan == $actual[0].scan",
        ".images[$key].scan.scanned_at == $actual[0].scan.scanned_at",
    ],
)
def test_release_rejects_mutable_scan_evidence_comparisons(
    tmp_path: Path,
    mutable_comparison: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "(.images[$key].scan | del(.scanned_at)) ==",
            f"{mutable_comparison} and\n               (.images[$key].scan | del(.scanned_at)) ==",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="mutable scan evidence"):
        validate_notification_workflows(directory)


def test_release_passes_the_planned_artifact_name_to_deploy(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "name: ${{ needs.plan.outputs.evidence_name }}",
            "name: production-release-${{ github.run_id }}-${{ github.run_attempt }}",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="exact planned artifact"):
        validate_notification_workflows(directory)


def test_release_uses_uuidv7_for_the_deployment_guard(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "guard_id=$(uv run --frozen python -c 'import uuid; print(uuid.uuid7())')",
            'guard_id="release-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="UUIDv7 guard ID"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "marker",
    [
        "tools/release_supply_chain.py create-cdk-assets",
        "tools/release_supply_chain.py bind-cdk-asset-checksums",
        "Stateful Runtime Operations CostGovernance",
        '--app "${RUNNER_TEMP}/cdk.out"',
        "--exclusively",
        "--unstable=publish-assets",
        "--force",
    ],
)
def test_release_requires_the_complete_cdk_asset_publisher(
    tmp_path: Path,
    marker: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(marker, "", 1),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match=r"CDK asset|policy marker"):
        validate_notification_workflows(directory)


def test_release_publishes_cdk_assets_before_building_images(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    text = path.read_text(encoding="utf-8")
    text = (
        text.replace(
            "name: Synthesize and publish the complete CDK asset closure",
            "name: temporary-step-name",
            1,
        )
        .replace(
            "name: Build and load the production image once",
            "name: Synthesize and publish the complete CDK asset closure",
            1,
        )
        .replace(
            "name: temporary-step-name",
            "name: Build and load the production image once",
            1,
        )
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(WorkflowPolicyError, match="before image build"):
        validate_notification_workflows(directory)


def test_release_cdk_asset_publisher_must_force_republish(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "            --force \\\n",
            "",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="incomplete"):
        validate_notification_workflows(directory)


def test_release_records_a_change_set_before_it_starts_polling(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '            jq --arg stack "${stack}" --arg arn "${arn}"',
            '            jq --arg recorded_stack "${stack}" --arg arn "${arn}"',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="change set recording"):
        validate_notification_workflows(directory)


def test_release_cleanup_uses_only_the_current_change_set_name(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'change_set_name="release-${GITHUB_SHA}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"',
            'change_set_name="release-${GITHUB_SHA}-${GITHUB_RUN_ID}-stale"',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="exact name"):
        validate_notification_workflows(directory)


def test_release_deploy_cleanup_uses_the_attested_change_set_arn(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace("--manifest", "--change-set-name", 2),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="attested change set ARN"):
        validate_notification_workflows(directory)


def test_release_independent_cleanup_requires_its_trusted_checkout(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    checkout = """      - name: Check out the exact release cleanup implementation
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: ${{ github.sha }}
          persist-credentials: false
"""
    path.write_text(
        path.read_text(encoding="utf-8").replace(checkout, "", 1),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="independent cleanup"):
        validate_notification_workflows(directory)


def test_release_independent_cleanup_also_runs_after_a_failed_plan(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    text = path.read_text(encoding="utf-8")
    cleanup_start = text.index("\n  cleanup:\n")
    cleanup = text[cleanup_start:].replace(
        "    if: ${{ always() }}",
        "    if: ${{ always() && needs.plan.result == 'success' }}",
        1,
    )
    path.write_text(text[:cleanup_start] + cleanup, encoding="utf-8")

    with pytest.raises(WorkflowPolicyError, match="independent cleanup"):
        validate_notification_workflows(directory)


def test_release_partial_plan_cleanup_requires_the_always_guard(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "always() && steps.plan_aws.outcome == 'success' && ",
            "",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="partial-plan cleanup"):
        validate_notification_workflows(directory)


def test_release_partial_plan_cleanup_must_invoke_the_helper(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    call = """          bash tools/cleanup_release_change_sets.sh \\
            --change-set-name "${change_set_name}"
"""
    path.write_text(
        path.read_text(encoding="utf-8").replace(call, "", 1),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="partial-plan cleanup"):
        validate_notification_workflows(directory)


def test_release_independent_cleanup_runs_before_rerun_rejection(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    text = path.read_text(encoding="utf-8")
    cleanup_start = text.index("\n  cleanup:\n")
    prefix = text[:cleanup_start]
    cleanup = text[cleanup_start:]
    cleanup = cleanup.replace(
        '            bash tools/cleanup_release_change_sets.sh --manifest "${manifest}"\n',
        "            true\n",
        1,
    ).replace(
        """            bash tools/cleanup_release_change_sets.sh \\
              --change-set-name "${change_set_name}"
""",
        "            true\n",
        1,
    )
    cleanup += """
          bash tools/cleanup_release_change_sets.sh --manifest "${manifest}"
          bash tools/cleanup_release_change_sets.sh --change-set-name "${change_set_name}"
"""
    path.write_text(prefix + cleanup, encoding="utf-8")

    with pytest.raises(WorkflowPolicyError, match="precede failed-rerun"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "marker",
    [
        "aws ssm describe-parameters",
        "aws cloudformation describe-events --generate-cli-skeleton input",
        "Recover stale unexecuted release change sets before planning",
        "--stale-before-plan",
        "needs: [plan, deploy]",
        "Acquire plan-role cleanup credentials",
        "continue-on-error: true",
        "EVIDENCE_RESULT: ${{ steps.cleanup_evidence.outcome }}",
        '[[ ! "${PLAN_ATTEMPT}" =~ ^[1-9][0-9]*$ ]]',
        'change_set_name="release-${GITHUB_SHA}-${GITHUB_RUN_ID}-${PLAN_ATTEMPT}"',
        "--attempt-name",
        "group: production-release",
        "if: ${{ needs.plan.result == 'success' && "
        "fromJSON(needs.plan.outputs.plan_attempt) == github.run_attempt }}",
        "A failed-jobs-only rerun cannot reuse an earlier release plan",
        'contains(fromJSON(\'["success","failure","cancelled"]\'), steps.prepare_changes.outcome)',
    ],
)
def test_release_requires_preflight_and_independent_cleanup(
    tmp_path: Path,
    marker: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(marker, "", 1),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match=r"policy marker|cleanup|diagnostic failure"):
        validate_notification_workflows(directory)


def test_release_failure_diagnostics_use_the_surviving_stack(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    text = path.read_text(encoding="utf-8")
    diagnostics_start = text.index("name: Capture bounded CloudFormation failure diagnostics")
    diagnostics_end = text.index("name: Remove this release's unexecuted change sets")
    diagnostics = text[diagnostics_start:diagnostics_end].replace(
        '--stack-name "${stack}"', '--change-set-name "${arn}"', 1
    )
    path.write_text(
        text[:diagnostics_start] + diagnostics + text[diagnostics_end:],
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="surviving stack"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    ("old", "new"),
    (
        ("continue-on-error: true", "continue-on-error: false"),
        ("--filters FailedEvents=true", "--filters FailedEvents=false"),
    ),
)
def test_release_diagnostic_failure_does_not_replace_deploy_failure(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    text = path.read_text(encoding="utf-8")
    diagnostics_start = text.index("name: Capture bounded CloudFormation failure diagnostics")
    diagnostics_end = text.index("name: Remove this release's unexecuted change sets")
    diagnostics = text[diagnostics_start:diagnostics_end].replace(old, new, 1)
    path.write_text(
        text[:diagnostics_start] + diagnostics + text[diagnostics_end:],
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match=r"diagnostic failure|DescribeEvents call shape"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    ("old", "new"),
    (
        (
            'status_reason: ((.StatusReason // "") | scrub)',
            'status_reason: "discarded"',
        ),
        (
            "steps.prepare_changes.outcome == 'failure'",
            "steps.prepare_changes.outcome == 'success'",
        ),
    ),
)
def test_release_retains_change_set_failure_reason_before_cleanup(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(old, new, 1),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="Change Set failure"):
        validate_notification_workflows(directory)


def test_release_cleanup_success_does_not_replace_deploy_failure(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    path = directory / RELEASE_WORKFLOW
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '          if [ "${DEPLOY_RESULT}" != success ]',
            "          if false",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match=r"preserve.*deploy failure"):
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
