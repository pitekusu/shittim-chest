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


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    directory = tmp_path / "workflows"
    directory.mkdir()
    for name in (
        ALLOWED_TARGET_WORKFLOW,
        "discord-security-digest.yml",
        DEPLOY_GUARD_WORKFLOW,
        RELEASE_WORKFLOW,
        DRIFT_WORKFLOW,
        WORKFLOW_RUN_NOTIFICATION,
        "ci.yml",
        RECORDS_CI_WORKFLOW,
        RECORDS_RELEASE_WORKFLOW,
        RECORDS_BACKFILL_WORKFLOW,
    ):
        (directory / name).write_bytes((WORKFLOW_DIRECTORY / name).read_bytes())
    return directory


def _replace(path: Path, old: str, new: str, count: int = -1) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"mutation target not found in {path.name}"
    path.write_text(text.replace(old, new, count), encoding="utf-8")


def test_repository_target_workflow_is_accepted(directory: Path) -> None:
    assert validate_notification_workflows(directory) == 1


@pytest.mark.parametrize(
    ("workflow_name", "marker", "replacement"),
    (
        (
            "ci.yml",
            "python3 tools/run_npm_audit.py -- npm run audit:infra",
            "npm run audit:infra",
        ),
        (
            RECORDS_CI_WORKFLOW,
            "python3 ../../tools/run_npm_audit.py -- pnpm audit --audit-level=low",
            "pnpm audit --audit-level=low",
        ),
        (
            RECORDS_RELEASE_WORKFLOW,
            "python3 ../../tools/run_npm_audit.py -- pnpm audit --audit-level=low",
            "pnpm audit --audit-level=low",
        ),
    ),
)
def test_node_audits_require_the_outage_aware_runner(
    directory: Path,
    workflow_name: str,
    marker: str,
    replacement: str,
) -> None:
    _replace(directory / workflow_name, marker, replacement, 1)

    with pytest.raises(WorkflowPolicyError, match=r"outage-aware|pinned pnpm|web gates"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "marker",
    (
        "timeout-minutes: 90",
        "vars.AWS_RECORDS_DRIFT_ROLE_ARN",
        "ShittimChest-Prod-RecordsStateful",
        "ShittimChest-Prod-RecordsApplication",
        "ShittimChest-Prod-RecordsEdge",
    ),
)
def test_drift_requires_records_stacks_and_dedicated_role(
    directory: Path,
    marker: str,
) -> None:
    _replace(directory / DRIFT_WORKFLOW, marker, "")

    with pytest.raises(WorkflowPolicyError, match="Drift lacks required policy marker"):
        validate_notification_workflows(directory)


def test_drift_roles_cover_their_serial_detection_windows(directory: Path) -> None:
    _replace(
        directory / DRIFT_WORKFLOW, "role-duration-seconds: 3600", "role-duration-seconds: 1800", 1
    )

    with pytest.raises(WorkflowPolicyError, match="serial detection windows"):
        validate_notification_workflows(directory)


def test_ci_requires_runtime_image_path_isolation(directory: Path) -> None:
    _replace(
        directory / "ci.yml",
        "    if: needs.changes.outputs.runtime_container == 'true'\n",
        "    if: always()\n",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="run only for canonical Runtime"):
        validate_notification_workflows(directory)


def test_records_ci_rejects_trigger_path_filters(directory: Path) -> None:
    _replace(
        directory / RECORDS_CI_WORKFLOW,
        "  pull_request:\n",
        "  pull_request:\n    paths:\n      - 'apps/records-web/**'\n",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="must not use path filters"):
        validate_notification_workflows(directory)


def test_records_release_resolves_pnpm_from_records_web_boundary(directory: Path) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        "            cd apps/records-web\n",
        "            cd .\n",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="Records Web package boundary"):
        validate_notification_workflows(directory)


def test_records_release_runs_web_gates_from_records_web_boundary(directory: Path) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        "            cd apps/records-web\n            pnpm exec vp check\n",
        "            cd .\n            pnpm --dir apps/records-web exec vp check\n",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="web gates"):
        validate_notification_workflows(directory)


def test_records_ci_requires_the_canonical_classifier_decision(directory: Path) -> None:
    _replace(
        directory / RECORDS_CI_WORKFLOW,
        "    if: needs.records-changes.outputs.records == 'true'\n",
        "    if: always()\n",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="canonical path decision"):
        validate_notification_workflows(directory)


def test_records_ci_gate_requires_the_classifier_job_to_succeed(directory: Path) -> None:
    _replace(
        directory / RECORDS_CI_WORKFLOW,
        "CHANGES_RESULT: ${{ needs.records-changes.result }}",
        "CHANGES_RESULT: ignored",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="preserve one required result"):
        validate_notification_workflows(directory)


def test_records_ci_requires_a_frozen_python_dependency_audit(directory: Path) -> None:
    _replace(
        directory / RECORDS_CI_WORKFLOW,
        "uv run --frozen pip-audit --strict --require-hashes",
        "echo audit-disabled",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="audit the frozen Records Python lock"):
        validate_notification_workflows(directory)


def test_records_ci_excludes_local_workspace_paths_from_hashed_audit(directory: Path) -> None:
    _replace(directory / RECORDS_CI_WORKFLOW, "--no-emit-local", "--no-emit-project", 1)

    with pytest.raises(WorkflowPolicyError, match="audit the frozen Records Python lock"):
        validate_notification_workflows(directory)


def test_records_ci_requires_the_pinned_pnpm_vite_plus_toolchain(directory: Path) -> None:
    _replace(directory / RECORDS_CI_WORKFLOW, "pnpm exec vp check", "npm run check", 1)

    with pytest.raises(WorkflowPolicyError, match=r"pinned pnpm and Vite\+"):
        validate_notification_workflows(directory)


def test_records_ci_rejects_the_non_allowlisted_vite_plus_action(directory: Path) -> None:
    _replace(
        directory / RECORDS_CI_WORKFLOW,
        "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0",
        "voidzero-dev/setup-vp@313600b80b104eadebb9111787d37a2e83e014ca # v1.17.0",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="allowlisted GitHub-owned"):
        validate_notification_workflows(directory)


def test_records_release_requires_production_approval(directory: Path) -> None:
    _replace(directory / RECORDS_RELEASE_WORKFLOW, "    environment: production\n", "", 1)

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_requires_complete_check_run_pagination(directory: Path) -> None:
    _replace(directory / RECORDS_RELEASE_WORKFLOW, "gh api --paginate --slurp", "gh api", 1)

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_binds_the_stream_to_the_deployment_account(directory: Path) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW, 'test "${stream_account}" = "${account}"', "true", 1
    )

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_requires_secure_parameter_metadata(directory: Path) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        "aws ssm describe-parameters",
        "echo parameter-check-disabled",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "parameter_name",
    (
        "/shittim-chest/production/records/discord/oauth/v0001",
        "/shittim-chest/production/records/discord/client-secret",
        "/shittim-chest/production/records/session-key",
    ),
)
def test_records_release_requires_each_auth_parameter_metadata(
    directory: Path,
    parameter_name: str,
) -> None:
    _replace(directory / RECORDS_RELEASE_WORKFLOW, parameter_name, "/removed", 1)

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_requires_read_only_api_smoke(directory: Path) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        '"${endpoint}/api/v1/records"',
        '"${endpoint}/api/v1/session"',
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_requires_anonymous_admin_boundary_smoke(directory: Path) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        '"${endpoint}/api/v1/admin/status"',
        '"${endpoint}/api/v1/session"',
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_requires_anonymous_session_to_be_non_admin(directory: Path) -> None:
    _replace(directory / RECORDS_RELEASE_WORKFLOW, ".isAdmin == false", ".isAdmin == true", 1)

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_does_not_freeze_runtime_digests(directory: Path) -> None:
    release = (directory / RECORDS_RELEASE_WORKFLOW).read_text(encoding="utf-8")

    assert "RuntimeImageDigest" not in release
    assert "BreakGlassImageDigest" not in release
    validate_notification_workflows(directory)


def test_records_release_preserves_old_hashed_web_assets(directory: Path) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        "            --recursive \\\n"
        '            --cache-control "public,max-age=31536000,immutable"',
        "            --recursive --delete \\\n"
        '            --cache-control "public,max-age=31536000,immutable"',
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="without deleting old hashes"):
        validate_notification_workflows(directory)


def test_records_release_revalidates_bundle_checksum_before_deploy(directory: Path) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        '--expected-parameter "RecordsBundleCodeSha256=${bundle_code_sha256}"',
        '--expected-parameter "RecordsBundleCodeSha256=unattested"',
        1,
    )

    with pytest.raises(WorkflowPolicyError, match=r"plan/deploy boundary|code checksum"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "marker",
    [
        'ParameterKey=RecordsMemorialUploadOriginDomain,ParameterValue="${memorial_upload_origin_domain}"',
        '--expected-parameter "RecordsMemorialUploadOriginDomain=${memorial_upload_origin_domain}"',
        "\"connect-src 'self' ${expected_upload_origin}\"",
    ],
)
def test_records_release_binds_exact_memorial_upload_origin(
    directory: Path,
    marker: str,
) -> None:
    _replace(directory / RECORDS_RELEASE_WORKFLOW, marker, "", 1)

    with pytest.raises(WorkflowPolicyError, match="exact Memorial upload origin"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "marker",
    [
        "hostname=$(jq --exit-status --raw-output '.records_public_hostname'",
        "--stack-name ShittimChest-Prod-RecordsApplication",
        "--stack-name ShittimChest-Prod-RecordsEdge",
        'select(.OutputKey == "RecordsPublicOrigin")',
        'test "${hostname}" = "${application_hostname}"',
        'test "${application_hostname}" = "${edge_hostname}"',
        'test "${public_origin}" = "https://${hostname}"',
    ],
)
def test_records_release_verifies_attested_hostname_against_deployment(
    directory: Path, marker: str
) -> None:
    path = directory / RECORDS_RELEASE_WORKFLOW
    text = path.read_text(encoding="utf-8")
    step = text.index(
        "      - name: Verify the deployed Records hostname against attested evidence"
    )
    marker_index = text.index(marker, step)
    path.write_text(
        text[:marker_index] + text[marker_index:].replace(marker, "", 1),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match=r"attested hostname.*deployed Records stacks"):
        validate_notification_workflows(directory)


def test_records_release_attests_the_validated_public_hostname(directory: Path) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        '--records-public-hostname "${PUBLIC_HOSTNAME}"',
        "",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="immutable plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_requires_public_hostname_to_match_upload_cors(directory: Path) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        '          test "${PUBLIC_HOSTNAME}" = "shittim.pitekusu.dev"\n',
        "",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="fixed Memorial upload CORS origin"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "marker",
    [
        "/shittim-chest/production/records/admin/discord-user-id",
        "RecordsPublicHostname",
    ],
)
def test_records_release_binds_admin_and_status_inputs(
    directory: Path,
    marker: str,
) -> None:
    _replace(directory / RECORDS_RELEASE_WORKFLOW, marker, "REMOVED")

    with pytest.raises(
        WorkflowPolicyError,
        match=r"plan/deploy boundary|pre-existing distribution|attested hostname",
    ):
        validate_notification_workflows(directory)


def test_records_release_requires_a_path_only_attestation_signer(directory: Path) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        "--signer-workflow pitekusu/shittim-chest/.github/workflows/records-release.yml",
        '--signer-workflow "${GITHUB_WORKFLOW_REF}"',
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "marker",
    [
        "--only-binary=:all:",
        '--python-version "${PYTHON_VERSION}"',
        "--python-platform aarch64-manylinux_2_28",
    ],
)
def test_records_release_builds_native_dependencies_for_arm64_lambda(
    directory: Path,
    marker: str,
) -> None:
    _replace(directory / RECORDS_RELEASE_WORKFLOW, marker, "REMOVED", 1)

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_binds_attestation_to_main_source_ref(directory: Path) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        "--source-ref refs/heads/main",
        "--source-ref refs/heads/other",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_waits_for_the_attested_change_set_type(directory: Path) -> None:
    _replace(directory / RECORDS_RELEASE_WORKFLOW, '--type "${type}"', "--type UPDATE", 1)

    with pytest.raises(WorkflowPolicyError, match="attest and revalidate"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("validate-change-set-safety", "validate-manifest"),
        ('--logical-name "${logical_name}"', "--logical-name application"),
    ],
)
def test_records_release_uses_the_scoped_change_set_safety_validator(
    directory: Path,
    original: str,
    replacement: str,
) -> None:
    _replace(directory / RECORDS_RELEASE_WORKFLOW, original, replacement, 1)

    with pytest.raises(WorkflowPolicyError, match="scope replacements"):
        validate_notification_workflows(directory)


def test_records_release_propagates_change_set_safety_failure(directory: Path) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        '                --logical-name "${logical_name}"\n            fi',
        '                --logical-name "${logical_name}" || true\n            fi',
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="propagate each create_plan safety failure"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "call_end",
    [
        '"${stateful_bucket}" "${stateful_key}"',
        'ParameterKey=RecordsBundleCodeSha256,ParameterValue="${BUNDLE_CODE_SHA256}"',
    ],
)
def test_records_release_propagates_each_create_plan_failure(
    directory: Path,
    call_end: str,
) -> None:
    _replace(directory / RECORDS_RELEASE_WORKFLOW, call_end, f"{call_end} || true", 1)

    with pytest.raises(WorkflowPolicyError, match="create_plan safety failure"):
        validate_notification_workflows(directory)


def test_records_release_requires_errexit_for_create_plan_calls(directory: Path) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        "          set -e\n          create_plan stateful",
        "          set +e\n          create_plan stateful",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="create_plan safety failure"):
        validate_notification_workflows(directory)


def test_records_release_rejects_deletion_time_as_stack_absence(
    directory: Path,
) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        "                  else\n                    .[0].StackStatus\n",
        "                  elif .[0].DeletionTime? != null then\n"
        '                    ""\n'
        "                  else\n"
        "                    .[0].StackStatus\n",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="stack absence from DeletionTime"):
        validate_notification_workflows(directory)


def test_records_release_does_not_require_a_pre_existing_edge_distribution(
    directory: Path,
) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        "          api_endpoint=$(aws cloudformation describe-stacks \\\n",
        "          records_distribution_id=$(aws cloudformation describe-stacks \\\n"
        "            --stack-name ShittimChest-Prod-RecordsEdge)\n"
        "          api_endpoint=$(aws cloudformation describe-stacks \\\n",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="pre-existing distribution"):
        validate_notification_workflows(directory)


def test_records_release_rejects_active_rollback_complete_as_create(
    directory: Path,
) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        '              ""|REVIEW_IN_PROGRESS) type=CREATE ;;',
        '              ""|REVIEW_IN_PROGRESS|ROLLBACK_COMPLETE) type=CREATE ;;',
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="active ROLLBACK_COMPLETE"):
        validate_notification_workflows(directory)


def test_records_release_rejects_runtime_change_set_type_lookup(directory: Path) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        '              aws cloudformation execute-change-set --region "${region}" \\\n'
        '                --change-set-name "${arn}"\n',
        "            aws cloudformation describe-change-set --query ChangeSetType\n"
        '              aws cloudformation execute-change-set --region "${region}" \\\n'
        '                --change-set-name "${arn}"\n',
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="attested Change Set type"):
        validate_notification_workflows(directory)


def test_records_release_requires_noop_cleanup_before_approval(directory: Path) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        "Remove attested no-op Records Change Sets before approval",
        "Ignore attested no-op Records Change Sets",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_requires_boolean_safe_executable_extraction(directory: Path) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        '| if type == "boolean" then tostring else error("executable must be boolean") end',
        "",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="boolean-safe"):
        validate_notification_workflows(directory)


def test_records_release_requires_final_unexecuted_change_set_check(directory: Path) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        "Confirm this Records release has no unexecuted Change Sets",
        "Skip the final Records Change Set inventory",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_requires_termination_protection_update(directory: Path) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        "aws cloudformation update-termination-protection",
        "aws cloudformation describe-stacks",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="plan/deploy boundary"):
        validate_notification_workflows(directory)


def test_records_release_verifies_termination_protection_twice(directory: Path) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        ".Stacks[0].EnableTerminationProtection == true",
        ".Stacks[0].EnableTerminationProtection != true",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="during and after deployment"):
        validate_notification_workflows(directory)


def test_records_release_protects_noop_stacks(directory: Path) -> None:
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


def test_records_release_accepts_all_attested_stable_noop_statuses(directory: Path) -> None:
    _replace(
        directory / RECORDS_RELEASE_WORKFLOW,
        "CREATE_COMPLETE|UPDATE_COMPLETE|UPDATE_ROLLBACK_COMPLETE|IMPORT_COMPLETE) ;;",
        "CREATE_COMPLETE|UPDATE_COMPLETE) ;;",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="all stable statuses"):
        validate_notification_workflows(directory)


def test_records_backfill_rejects_unbounded_page_limit(directory: Path) -> None:
    _replace(
        directory / RECORDS_BACKFILL_WORKFLOW,
        '          test "${PAGE_LIMIT}" -le 100\n',
        "          true\n",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="bounded and content-free"):
        validate_notification_workflows(directory)


def test_records_backfill_requires_every_candidate_to_be_validated(directory: Path) -> None:
    _replace(
        directory / RECORDS_BACKFILL_WORKFLOW,
        "             .validated == .candidates and\n",
        "             .validated <= .candidates and\n",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="bounded and content-free"):
        validate_notification_workflows(directory)


def test_records_backfill_rejects_a_widened_completion_page_bound(directory: Path) -> None:
    _replace(
        directory / RECORDS_BACKFILL_WORKFLOW,
        '  BACKFILL_MAX_PAGES: "25"\n',
        '  BACKFILL_MAX_PAGES: "250"\n',
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="bounded and content-free"):
        validate_notification_workflows(directory)


def test_records_backfill_requires_terminal_completion(directory: Path) -> None:
    _replace(
        directory / RECORDS_BACKFILL_WORKFLOW,
        '          test "${complete}" = true\n',
        "          true\n",
        1,
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
    directory: Path,
    old: str,
    new: str,
) -> None:
    _replace(directory / RECORDS_BACKFILL_WORKFLOW, old, new, 1)

    with pytest.raises(WorkflowPolicyError, match="bounded and content-free"):
        validate_notification_workflows(directory)


def test_runtime_required_gates_require_the_classifier_job_to_succeed(directory: Path) -> None:
    _replace(
        directory / "ci.yml",
        "CHANGES_RESULT: ${{ needs.changes.result }}",
        "CHANGES_RESULT: ignored",
        1,
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
    directory: Path,
    workflow: str,
    old: str,
    new: str,
) -> None:
    _replace(directory / workflow, old, new, 1)

    with pytest.raises(WorkflowPolicyError, match="Buildx client and BuildKit image digest"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize("workflow", ["ci.yml", RELEASE_WORKFLOW])
def test_container_builds_reject_an_extra_buildx_setup(
    directory: Path,
    workflow: str,
) -> None:
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


def test_release_requires_pre_attestation_referrer_baselines(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        "Capture ACTIVE referrer baselines before attestations",
        "Capture referrers after attestations",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="referrer"):
        validate_notification_workflows(directory)


def test_release_requires_current_run_referrer_selection(directory: Path) -> None:
    _replace(directory / RELEASE_WORKFLOW, "select-release-referrers", "verify-image", 1)

    with pytest.raises(WorkflowPolicyError, match="selected current-run referrers"):
        validate_notification_workflows(directory)


def test_release_binds_selected_referrers_to_notation_inspection(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        '--notation-inspection "${RUNNER_TEMP}/normal.notation.json"',
        '--notation-inspection "${RUNNER_TEMP}/normal.referrers-after.json"',
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="referrer delta verification"):
        validate_notification_workflows(directory)


def test_release_referrer_snapshots_must_keep_aws_pagination(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        "aws ecr list-image-referrers \\",
        "aws ecr list-image-referrers --no-paginate \\",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="complete AWS pagination"):
        validate_notification_workflows(directory)


def test_release_attestations_defer_to_the_canonical_summary(directory: Path) -> None:
    _replace(directory / RELEASE_WORKFLOW, "          show-summary: false\n", "", 1)

    with pytest.raises(WorkflowPolicyError, match="canonical summary"):
        validate_notification_workflows(directory)


def test_release_attestation_summary_rejects_maskable_owner_url(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        "https://github.com/pitek%75su/shittim-chest/attestations/",
        "https://github.com/pitekusu/shittim-chest/attestations/",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="owner masking"):
        validate_notification_workflows(directory)


def test_repeated_action_version_requires_one_commit_pin(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0",
        "actions/setup-node@0000000000000000000000000000000000000000 # v7.0.0",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="inconsistent action pin"):
        validate_notification_workflows(directory)


def test_release_requires_the_locked_node_version(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        '          node-version: "24.18.0"',
        "          node-version-file: .node-version",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="lacks required policy marker"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize("check_name", sorted(RELEASE_REQUIRED_MAIN_CHECKS))
def test_release_requires_every_main_check(
    directory: Path,
    check_name: str,
) -> None:
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


def test_release_rejects_an_extra_main_check(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        "quality tests security package cdk docs-public-safety",
        "quality tests security package cdk unexpected docs-public-safety",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="exactly 8 CI checks and 3 CodeQL"):
        validate_notification_workflows(directory)


def test_release_requires_reproducible_registry_images(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        '          SOURCE_DATE_EPOCH: "0"',
        '          SOURCE_DATE_EPOCH: "1"',
        1,
    )

    with pytest.raises(WorkflowPolicyError, match=r"reproducible|required policy marker"):
        validate_notification_workflows(directory)


def test_release_requires_ci_identical_docker_exporters(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        "outputs: type=docker,rewrite-timestamp=true,compression=gzip,"
        "compression-level=6,force-compression=true",
        "outputs: type=registry,rewrite-timestamp=true,compression=gzip,"
        "compression-level=6,force-compression=true",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match=r"CI-identical|another exporter"):
        validate_notification_workflows(directory)


def test_release_image_builds_reject_the_mutated_workspace_context(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW, "context: ${{ env.RELEASE_IMAGE_CONTEXT }}", "context: .", 1
    )

    with pytest.raises(WorkflowPolicyError, match="use the immutable image context"):
        validate_notification_workflows(directory)


def test_release_image_checkout_is_pinned_to_github_sha(directory: Path) -> None:
    path = directory / RELEASE_WORKFLOW
    text = path.read_text(encoding="utf-8")
    start = text.index("      - name: Check out the immutable image build context")
    end = text.index("\n      - name:", start + 1)
    checkout = text[start:end].replace("ref: ${{ github.sha }}", "ref: main", 1)
    path.write_text(text[:start] + checkout + text[end:], encoding="utf-8")

    with pytest.raises(WorkflowPolicyError, match=r"pin github\.sha"):
        validate_notification_workflows(directory)


def test_release_pytest_disables_checkout_bytecode_writes(directory: Path) -> None:
    _replace(directory / RELEASE_WORKFLOW, '  PYTHONDONTWRITEBYTECODE: "1"\n', "", 1)

    with pytest.raises(WorkflowPolicyError, match="PYTHONDONTWRITEBYTECODE"):
        validate_notification_workflows(directory)


def test_release_runs_no_gates_after_the_image_checkout(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        "      - name: Set up Buildx\n",
        "      - name: Re-run an unsafe test in the image checkout\n"
        "        run: uv run --frozen pytest\n"
        "      - name: Set up Buildx\n",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="must not run test"):
        validate_notification_workflows(directory)


def test_release_reuses_the_exact_main_ci_image_cache_scopes(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        "cache-from: type=gha,scope=container-arm64-production",
        "cache-from: type=gha,scope=release-production-arm64",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="exact main CI image cache"):
        validate_notification_workflows(directory)


def test_release_checks_the_config_digest_before_push(directory: Path) -> None:
    _replace(directory / RELEASE_WORKFLOW, "--config-digest-only", "", 1)

    with pytest.raises(WorkflowPolicyError, match=r"local config|required policy marker"):
        validate_notification_workflows(directory)


def test_release_gates_the_rebuilt_image_on_fixable_high_findings(
    directory: Path,
) -> None:
    _replace(directory / RELEASE_WORKFLOW, "            --only-fixed \\\n", "", 1)

    with pytest.raises(WorkflowPolicyError, match="fixable High/Critical"):
        validate_notification_workflows(directory)


def test_release_rejects_cross_run_same_sha_config_comparison(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        "      - name: Validate release image configs before push\n",
        "      - name: Resolve the successful same-SHA main CI run\n"
        "        run: echo forbidden\n"
        "      - name: Validate release image configs before push\n",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="cross-run same-SHA"):
        validate_notification_workflows(directory)


def test_release_plan_requires_actions_read_for_records_release_gate(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        "      actions: read\n      attestations: write",
        "      attestations: write",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="canonical plan/deploy/cleanup split"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    ("marker", "replacement"),
    (
        ("actions/workflows/records-release.yml/runs", "actions/runs"),
        (".head_sha == $sha", ".head_sha != $sha"),
        ("status=completed", "status=success"),
        ("sort_by(.updated_at, .id, .run_attempt) | last", "sort_by(.id) | last"),
        (
            "run_attempt=$(jq --exit-status --raw-output '.run_attempt'",
            "run_attempt=$(jq --exit-status --raw-output '.run_number'",
        ),
        (".updated_at", ".created_at"),
        ('test "${conclusion}" = success', 'test "${conclusion}" = failure'),
        (
            'echo "artifact_name=records-plan-${run_id}-${run_attempt}"',
            'echo "artifact_name=records-plan-${run_id}"',
        ),
        ('.status == "completed"', '.status == "in_progress"'),
    ),
)
def test_release_requires_successful_same_sha_records_release(
    directory: Path, marker: str, replacement: str
) -> None:
    _replace(directory / RELEASE_WORKFLOW, marker, replacement, 1)

    with pytest.raises(WorkflowPolicyError, match="successful same-SHA Records release"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    ("marker", "message"),
    [
        (
            "name: ${{ steps.records_release.outputs.artifact_name }}",
            "exact successful same-SHA Records artifact",
        ),
        (
            "run-id: ${{ steps.records_release.outputs.run_id }}",
            "exact successful same-SHA Records artifact",
        ),
        (
            'gh attestation verify "${manifest}"',
            "attested same-SHA Records hostname evidence",
        ),
        ("--deny-self-hosted-runners", "attested same-SHA Records hostname evidence"),
        ('--signer-digest "${GITHUB_SHA}"', "attested same-SHA Records hostname evidence"),
        ('--source-digest "${GITHUB_SHA}"', "attested same-SHA Records hostname evidence"),
        (
            'validate-manifest "${manifest}" --expected-commit-sha "${GITHUB_SHA}"',
            "attested same-SHA Records hostname evidence",
        ),
        (
            "hostname=$(jq --exit-status --raw-output '.records_public_hostname'",
            "attested same-SHA Records hostname evidence",
        ),
        (
            'test "${hostname}" = "shittim.pitekusu.dev"',
            "attested same-SHA Records hostname evidence",
        ),
        (
            'echo "hostname=${hostname}" >> "${GITHUB_OUTPUT}"',
            "attested same-SHA Records hostname evidence",
        ),
    ],
)
def test_release_consumes_attested_same_sha_records_hostname(
    directory: Path, marker: str, message: str
) -> None:
    _replace(directory / RELEASE_WORKFLOW, marker, "", 1)

    with pytest.raises(WorkflowPolicyError, match=message):
        validate_notification_workflows(directory)


def test_core_release_cannot_read_records_stacks_for_hostname_binding(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        "      - name: Require the active Project cost-allocation tag",
        "      - name: Bind Records hostname to the deployed Records stacks\n"
        "        run: aws cloudformation describe-stacks "
        "--stack-name ShittimChest-Prod-RecordsApplication\n"
        "      - name: Require the active Project cost-allocation tag",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="without Records stack access"):
        validate_notification_workflows(directory)


def test_release_attests_only_registry_confirmed_manifest_digests(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        "steps.push-images.outputs.normal_digest",
        "steps.build-normal.outputs.digest",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="registry-confirmed"):
        validate_notification_workflows(directory)


def test_ci_requires_reproducible_production_and_fault_images(directory: Path) -> None:
    _replace(
        directory / "ci.yml",
        '          SOURCE_DATE_EPOCH: "0"',
        '          SOURCE_DATE_EPOCH: "1"',
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="reproducible"):
        validate_notification_workflows(directory)


def test_ci_requires_loaded_image_file_timestamp_rewrite(directory: Path) -> None:
    _replace(
        directory / "ci.yml",
        "outputs: type=docker,rewrite-timestamp=true,compression=gzip,"
        "compression-level=6,force-compression=true",
        "load: true",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="deterministically compress"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize("workflow", ["ci.yml", RELEASE_WORKFLOW])
def test_image_builds_require_forced_canonical_compression(
    directory: Path,
    workflow: str,
) -> None:
    _replace(directory / workflow, ",force-compression=true", "", 1)

    with pytest.raises(WorkflowPolicyError, match=r"deterministic|deterministically"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    ("workflow", "step_name"),
    [
        ("ci.yml", "Build and load the production image"),
        ("ci.yml", "Build and load the CI-only fault image"),
        (RELEASE_WORKFLOW, "Build and load the production image once"),
    ],
)
def test_docker_image_builds_reject_manifest_list_output(
    directory: Path,
    workflow: str,
    step_name: str,
) -> None:
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


def test_ci_requires_rootfs_diff_id_evidence(directory: Path) -> None:
    _replace(
        directory / "ci.yml", "production-image-rootfs-diffids.json", "missing-rootfs-evidence.json"
    )

    with pytest.raises(WorkflowPolicyError, match="rootfs diff ID"):
        validate_notification_workflows(directory)


def test_ci_regenerates_cache_sensitive_final_image_stages(
    directory: Path,
) -> None:
    _replace(directory / "ci.yml", "          no-cache-filters: builder,runtime-base\n", "", 1)

    with pytest.raises(WorkflowPolicyError, match=r"builder snapshot|final"):
        validate_notification_workflows(directory)


def test_release_regenerates_cache_sensitive_final_image_stages(
    directory: Path,
) -> None:
    _replace(
        directory / RELEASE_WORKFLOW, "          no-cache-filters: builder,runtime-base\n", "", 1
    )

    with pytest.raises(WorkflowPolicyError, match=r"builder snapshot|final"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize("workflow", ["ci.yml", RELEASE_WORKFLOW])
def test_risk_bound_images_reject_cached_builder_snapshots(
    directory: Path,
    workflow: str,
) -> None:
    _replace(
        directory / workflow,
        "          no-cache-filters: builder,runtime-base\n",
        "          no-cache-filters: runtime-base\n",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="builder snapshot"):
        validate_notification_workflows(directory)


def test_ci_requires_actual_docker_context_bytecode_proof(directory: Path) -> None:
    _replace(directory / "ci.yml", "python3 -m py_compile src/shittim_chest/__init__.py", "true", 1)

    with pytest.raises(WorkflowPolicyError, match=r"actual \.dockerignore output"):
        validate_notification_workflows(directory)


def test_release_normalizes_cost_tag_metadata_before_comparison(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        ".CostAllocationTags | map({Status, TagKey, Type}) ==",
        ".CostAllocationTags ==",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="lacks required policy marker"):
        validate_notification_workflows(directory)


def test_release_requires_the_unversioned_signing_profile_arn(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        "signing-profiles/shittim_chest_ecr$",
        "signing-profiles/shittim_chest_ecr/[A-Za-z0-9]{10}$",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="lacks required policy marker"):
        validate_notification_workflows(directory)


def test_release_requires_the_fail_fast_image_evidence_waiter(directory: Path) -> None:
    _replace(directory / RELEASE_WORKFLOW, "tools/wait_release_image_evidence.sh", "", 1)

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
    directory: Path,
    marker: str,
) -> None:
    _replace(directory / RELEASE_WORKFLOW, marker, "", 1)

    with pytest.raises(WorkflowPolicyError, match="exact Lambda bundle checksum"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "marker",
    [
        "${{ runner.temp }}/records-release-evidence/records-release-manifest.json",
        "RECORDS_PUBLIC_HOSTNAME: ${{ steps.records_evidence.outputs.hostname }}",
        'records_manifest="${RUNNER_TEMP}/release/records-release-evidence/records-release-manifest.json"',
        "records_public_hostname=$(jq --exit-status --raw-output",
        'test "${#records_public_hostname}" -le 253',
        '[[ "${records_public_hostname}" =~ ^[a-z0-9]'
        "([a-z0-9-]{0,61}[a-z0-9])?"
        "(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$ ]]",
        'test "${records_public_hostname}" = "shittim.pitekusu.dev"',
        '"ParameterKey=RecordsPublicHostname,ParameterValue=${RECORDS_PUBLIC_HOSTNAME}"',
        '--expected-parameter "RecordsPublicHostname=${records_public_hostname}"',
        'expected_memorial_url="https://${records_public_hostname}/memorial"',
        'select(.name == "SHITTIM_RECORDS_MEMORIAL_URL")',
    ],
)
def test_release_binds_exact_records_memorial_url(
    directory: Path,
    marker: str,
) -> None:
    _replace(directory / RELEASE_WORKFLOW, marker, "", 1)

    with pytest.raises(WorkflowPolicyError, match="exact Records Memorial URL"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "marker",
    [
        'gh attestation verify "${records_manifest}"',
        "--signer-workflow pitekusu/shittim-chest/.github/workflows/records-release.yml",
        "uv run --frozen python tools/records_release_manifest.py validate-manifest",
        '"${records_manifest}" --expected-commit-sha "${GITHUB_SHA}"',
    ],
)
def test_release_deploy_revalidates_attested_records_hostname(
    directory: Path,
    marker: str,
) -> None:
    path = directory / RELEASE_WORKFLOW
    text = path.read_text(encoding="utf-8")
    step = text.index("      - name: Revalidate the manifest and its GitHub attestation")
    marker_index = text.index(marker, step)
    path.write_text(
        text[:marker_index] + text[marker_index:].replace(marker, "", 1),
        encoding="utf-8",
    )

    with pytest.raises(WorkflowPolicyError, match="exact Records Memorial URL"):
        validate_notification_workflows(directory)


def test_release_rejects_raw_hostname_job_output(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        "      plan_attempt: ${{ steps.evidence.outputs.run_attempt }}\n",
        "      plan_attempt: ${{ steps.evidence.outputs.run_attempt }}\n"
        "      records_public_hostname: ${{ steps.records_evidence.outputs.hostname }}\n",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="exact Records Memorial URL"):
        validate_notification_workflows(directory)


def test_release_rejects_raw_hostname_job_output_consumer(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        "          ASSET_BUCKET: ${{ vars.CDK_ASSET_BUCKET }}\n"
        "          ECR_REPOSITORY_URI: ${{ vars.ECR_REPOSITORY_URI }}\n"
        "          MONITOR_ARN: ${{ vars.EXISTING_SERVICE_ANOMALY_MONITOR_ARN }}\n"
        "          SIGNING_PROFILE_ARN: ${{ vars.ECR_SIGNING_PROFILE_ARN }}\n",
        "          ASSET_BUCKET: ${{ vars.CDK_ASSET_BUCKET }}\n"
        "          ECR_REPOSITORY_URI: ${{ vars.ECR_REPOSITORY_URI }}\n"
        "          MONITOR_ARN: ${{ vars.EXISTING_SERVICE_ANOMALY_MONITOR_ARN }}\n"
        "          RECORDS_PUBLIC_HOSTNAME: "
        "${{ needs.plan.outputs.records_public_hostname }}\n"
        "          SIGNING_PROFILE_ARN: ${{ vars.ECR_SIGNING_PROFILE_ARN }}\n",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="exact Records Memorial URL"):
        validate_notification_workflows(directory)


def test_release_rejects_notation_login(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        '          notation verify "${NORMAL_REFERENCE}"',
        "          notation login --username AWS registry.example\n"
        '          notation verify "${NORMAL_REFERENCE}"',
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="for Notation"):
        validate_notification_workflows(directory)


def test_release_requires_two_ephemeral_ecr_logins(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        '          aws ecr get-login-password --region "${AWS_REGION}" | docker login \\\n'
        '            --username AWS --password-stdin "${registry}"\n',
        "",
        1,
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
    directory: Path,
    marker: str,
) -> None:
    _replace(directory / RELEASE_WORKFLOW, marker, "")

    with pytest.raises(WorkflowPolicyError, match="lacks required policy marker"):
        validate_notification_workflows(directory)


def test_release_revalidates_preserved_cdk_artifact_paths(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        "${RUNNER_TEMP}/release/cdk.out/${artifact}.template.json",
        "${RUNNER_TEMP}/release/${artifact}.template.json",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="preserved artifact paths"):
        validate_notification_workflows(directory)


def test_release_loads_regenerated_image_verification_for_comparison(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        '--slurpfile actual "${RUNNER_TEMP}/normal.verification.json"',
        "--slurpfile actual",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="image verification document"):
        validate_notification_workflows(directory)


def test_release_requires_all_current_scan_evidence_except_timestamp(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        "($actual[0].scan | del(.scanned_at))",
        "($actual[0].scan | del(.scanned_at, .severity_counts))",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="all current scan evidence"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "mutable_comparison",
    [
        ".images.normal.scan == $actual[0].scan",
        ".images.normal.scan.scanned_at == $actual[0].scan.scanned_at",
    ],
)
def test_release_rejects_mutable_scan_evidence_comparisons(
    directory: Path,
    mutable_comparison: str,
) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        "(.images.normal.scan | del(.scanned_at)) ==",
        f"{mutable_comparison} and\n             (.images.normal.scan | del(.scanned_at)) ==",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="mutable scan evidence"):
        validate_notification_workflows(directory)


def test_release_passes_the_planned_artifact_name_to_deploy(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        "name: ${{ needs.plan.outputs.evidence_name }}",
        "name: production-release-${{ github.run_id }}-${{ github.run_attempt }}",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="exact planned artifact"):
        validate_notification_workflows(directory)


def test_release_uses_uuidv7_for_the_deployment_guard(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        "guard_id=$(uv run --frozen python -c 'import uuid; print(uuid.uuid7())')",
        'guard_id="release-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"',
        1,
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
    directory: Path,
    marker: str,
) -> None:
    _replace(directory / RELEASE_WORKFLOW, marker, "", 1)

    with pytest.raises(WorkflowPolicyError, match=r"CDK asset|policy marker"):
        validate_notification_workflows(directory)


def test_release_publishes_cdk_assets_before_building_images(directory: Path) -> None:
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


def test_release_cdk_asset_publisher_must_force_republish(directory: Path) -> None:
    _replace(directory / RELEASE_WORKFLOW, "            --force \\\n", "", 1)

    with pytest.raises(WorkflowPolicyError, match="incomplete"):
        validate_notification_workflows(directory)


def test_release_records_a_change_set_before_it_starts_polling(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        '            jq --arg stack "${stack}" --arg arn "${arn}"',
        '            jq --arg recorded_stack "${stack}" --arg arn "${arn}"',
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="change set recording"):
        validate_notification_workflows(directory)


def test_release_cleanup_uses_only_the_current_change_set_name(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        'change_set_name="release-${GITHUB_SHA}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"',
        'change_set_name="release-${GITHUB_SHA}-${GITHUB_RUN_ID}-stale"',
        1,
    )

    with pytest.raises(WorkflowPolicyError, match="exact name"):
        validate_notification_workflows(directory)


def test_release_deploy_cleanup_uses_the_attested_change_set_arn(directory: Path) -> None:
    _replace(directory / RELEASE_WORKFLOW, "--manifest", "--change-set-name", 2)

    with pytest.raises(WorkflowPolicyError, match="attested change set ARN"):
        validate_notification_workflows(directory)


def test_release_independent_cleanup_requires_its_trusted_checkout(directory: Path) -> None:
    checkout = """      - name: Check out the exact release cleanup implementation
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: ${{ github.sha }}
          persist-credentials: false
"""
    _replace(directory / RELEASE_WORKFLOW, checkout, "", 1)

    with pytest.raises(WorkflowPolicyError, match="independent cleanup"):
        validate_notification_workflows(directory)


def test_release_independent_cleanup_also_runs_after_a_failed_plan(directory: Path) -> None:
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


def test_release_partial_plan_cleanup_requires_the_always_guard(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW, "always() && steps.plan_aws.outcome == 'success' && ", "", 1
    )

    with pytest.raises(WorkflowPolicyError, match="partial-plan cleanup"):
        validate_notification_workflows(directory)


def test_release_partial_plan_cleanup_must_invoke_the_helper(directory: Path) -> None:
    call = """          bash tools/cleanup_release_change_sets.sh \\
            --change-set-name "${change_set_name}"
"""
    _replace(directory / RELEASE_WORKFLOW, call, "", 1)

    with pytest.raises(WorkflowPolicyError, match="partial-plan cleanup"):
        validate_notification_workflows(directory)


def test_release_independent_cleanup_runs_before_rerun_rejection(directory: Path) -> None:
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
        "--stale-before-plan",
        "needs: [plan, deploy]",
        "continue-on-error: true",
        "EVIDENCE_RESULT: ${{ steps.cleanup_evidence.outcome }}",
        '[[ ! "${PLAN_ATTEMPT}" =~ ^[1-9][0-9]*$ ]]',
        'change_set_name="release-${GITHUB_SHA}-${GITHUB_RUN_ID}-${PLAN_ATTEMPT}"',
        "--attempt-name",
        "group: production-release",
        "if: ${{ needs.plan.result == 'success' && "
        "fromJSON(needs.plan.outputs.plan_attempt) == github.run_attempt }}",
        'contains(fromJSON(\'["success","failure","cancelled"]\'), steps.prepare_changes.outcome)',
    ],
)
def test_release_requires_preflight_and_independent_cleanup(
    directory: Path,
    marker: str,
) -> None:
    _replace(directory / RELEASE_WORKFLOW, marker, "", 1)

    with pytest.raises(WorkflowPolicyError, match=r"policy marker|cleanup|diagnostic failure"):
        validate_notification_workflows(directory)


def test_release_failure_diagnostics_use_the_surviving_stack(directory: Path) -> None:
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
    directory: Path,
    old: str,
    new: str,
) -> None:
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
    directory: Path,
    old: str,
    new: str,
) -> None:
    _replace(directory / RELEASE_WORKFLOW, old, new, 1)

    with pytest.raises(WorkflowPolicyError, match="Change Set failure"):
        validate_notification_workflows(directory)


def test_release_cleanup_success_does_not_replace_deploy_failure(directory: Path) -> None:
    _replace(
        directory / RELEASE_WORKFLOW,
        '          if [ "${DEPLOY_RESULT}" != success ]',
        "          if false",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match=r"preserve.*deploy failure"):
        validate_notification_workflows(directory)


def test_unapproved_target_workflow_is_rejected(directory: Path) -> None:
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
    directory: Path,
    old: str,
    new: str,
    message: str,
) -> None:
    _replace(directory / ALLOWED_TARGET_WORKFLOW, old, new)
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
    directory: Path,
    old: str,
    new: str,
) -> None:
    _replace(directory / ALLOWED_TARGET_WORKFLOW, old, new, 1)

    with pytest.raises(WorkflowPolicyError, match=r"non-canonical|read-only"):
        validate_notification_workflows(directory)


def test_additional_secret_is_rejected(directory: Path) -> None:
    _replace(
        directory / ALLOWED_TARGET_WORKFLOW,
        "DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}",
        "DISCORD_WEBHOOK_URL: ${{ secrets.EXTRA_SECRET }}",
    )
    with pytest.raises(WorkflowPolicyError, match="only DISCORD_WEBHOOK_URL"):
        validate_notification_workflows(directory)


def test_extra_checkout_without_trusted_ref_is_rejected(directory: Path) -> None:
    _replace(
        directory / ALLOWED_TARGET_WORKFLOW,
        "      - name: Notify pull-request lifecycle",
        "      - uses: actions/checkout@0000000000000000000000000000000000000000\n"
        "      - name: Notify pull-request lifecycle",
    )
    with pytest.raises(WorkflowPolicyError, match="every checkout"):
        validate_notification_workflows(directory)


def test_multiline_run_cannot_expand_pull_request_metadata(directory: Path) -> None:
    _replace(
        directory / ALLOWED_TARGET_WORKFLOW,
        "run: python3 -m tools.github_discord_notifications pull-request",
        "run: |\n          echo ${{ github.event.pull_request.title }}",
    )
    with pytest.raises(WorkflowPolicyError, match="untrusted event"):
        validate_notification_workflows(directory)


def test_vulnerability_alerts_permission_cannot_be_widened(directory: Path) -> None:
    digest = directory / "discord-security-digest.yml"
    _replace(digest, "vulnerability-alerts: read", "vulnerability-alerts: write")
    with pytest.raises(WorkflowPolicyError, match="one read-only"):
        validate_notification_workflows(directory)


def test_vulnerability_alerts_permission_cannot_be_duplicated(directory: Path) -> None:
    extra = directory / "extra.yml"
    extra.write_text("permissions:\n  vulnerability-alerts: read\n", encoding="utf-8")
    with pytest.raises(WorkflowPolicyError, match="one read-only"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize("trigger", ["pull_request", "pull_request_target", "push", "schedule"])
def test_deploy_guard_rejects_automatic_triggers(directory: Path, trigger: str) -> None:
    _replace(
        directory / DEPLOY_GUARD_WORKFLOW,
        "  workflow_dispatch:",
        f"  {trigger}:\n  workflow_dispatch:",
        1,
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
    directory: Path,
    old: str,
    new: str,
    message: str,
) -> None:
    _replace(directory / DEPLOY_GUARD_WORKFLOW, old, new, 1)
    with pytest.raises(WorkflowPolicyError, match=message):
        validate_notification_workflows(directory)


def test_deploy_guard_rejects_reason_expression_in_shell(directory: Path) -> None:
    _replace(
        directory / DEPLOY_GUARD_WORKFLOW,
        "uv run --frozen python -m tools.control_records guard",
        "echo ${{ inputs.break_glass_reason }}\n"
        "          uv run --frozen python -m tools.control_records guard",
    )
    with pytest.raises(WorkflowPolicyError, match="through env"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    "permission",
    ["actions", "checks", "contents", "packages", "pull-requests"],
)
def test_deploy_guard_rejects_every_other_write_permission(
    directory: Path,
    permission: str,
) -> None:
    _replace(
        directory / DEPLOY_GUARD_WORKFLOW,
        "      id-token: write",
        f"      id-token: write\n      {permission}: write",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match=r"canonical|duplicated"):
        validate_notification_workflows(directory)


def test_deploy_guard_rejects_write_all_permissions(directory: Path) -> None:
    _replace(
        directory / DEPLOY_GUARD_WORKFLOW,
        "    permissions:",
        "    permissions: write-all\n    legacy-permissions:",
        1,
    )

    with pytest.raises(WorkflowPolicyError, match=r"non-canonical|shorthand"):
        validate_notification_workflows(directory)


def test_deploy_guard_rejects_flow_style_extra_write_permission(directory: Path) -> None:
    _replace(
        directory / DEPLOY_GUARD_WORKFLOW,
        "    permissions:\n      contents: read\n      id-token: write",
        "    permissions: {contents: read, id-token: write, actions: write}",
        1,
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
    directory: Path,
    permission_block: str,
) -> None:
    canonical = "    permissions:\n      contents: read\n      id-token: write"
    _replace(directory / DEPLOY_GUARD_WORKFLOW, canonical, permission_block, 1)

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
    directory: Path,
    permissions: str,
) -> None:
    (directory / "unsafe-permissions.yml").write_text(
        f"name: unsafe-permissions\non: [pull_request]\n{permissions}\njobs: {{}}\n",
        encoding="utf-8",
    )

    with pytest.raises(
        WorkflowPolicyError,
        match=r"non-canonical|AWS or deployment capability",
    ):
        validate_notification_workflows(directory)


def test_permission_like_comments_do_not_widen_capability(directory: Path) -> None:
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
    directory: Path,
    capability: str,
) -> None:
    unsafe = directory / "unsafe-pr.yml"
    unsafe.write_text(
        f"name: unsafe-pr\non:\n  pull_request:\npermissions: {{}}\njobs:\n  unsafe:\n{capability}",
        encoding="utf-8",
    )
    with pytest.raises(WorkflowPolicyError, match="AWS or deployment capability"):
        validate_notification_workflows(directory)


def test_inline_pull_request_trigger_cannot_bypass_aws_boundary(directory: Path) -> None:
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
    directory: Path,
    trigger: str,
) -> None:
    unsafe = directory / "unsafe-indirect.yml"
    unsafe.write_text(
        f"name: unsafe-indirect\n{trigger}\npermissions:\n  id-token: write\n",
        encoding="utf-8",
    )
    with pytest.raises(WorkflowPolicyError, match="AWS or deployment capability"):
        validate_notification_workflows(directory)


def test_deploy_guard_rejects_flow_style_trigger_list(directory: Path) -> None:
    _replace(
        directory / DEPLOY_GUARD_WORKFLOW,
        "on:\n",
        "on: [workflow_dispatch, push]\nlegacy-trigger-config:\n",
        1,
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
    directory: Path,
    marker: str,
) -> None:
    _replace(directory / DEPLOY_GUARD_WORKFLOW, marker, "")
    with pytest.raises(WorkflowPolicyError, match="lacks required policy marker"):
        validate_notification_workflows(directory)


def test_notification_allowlist_must_include_deploy_guard(directory: Path) -> None:
    _replace(directory / WORKFLOW_RUN_NOTIFICATION, "      - Production Deploy Guard\n", "")
    with pytest.raises(WorkflowPolicyError, match="notification allowlist"):
        validate_notification_workflows(directory)
