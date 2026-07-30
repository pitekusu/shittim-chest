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
    ):
        raise WorkflowPolicyError("Release permissions are not the canonical plan/deploy split")
    required = (
        "name: Production Release",
        "cancel-in-progress: false",
        "runs-on: ubuntu-24.04-arm",
        'node-version: "24.18.0"',
        'EXPECTED_REPOSITORY_ID: "1302516701"',
        ".use_immutable_subject == true",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "repository_owner_id",
        "'Analyze (python)'",
        "map({Status, TagKey, Type})",
        "vars.AWS_RELEASE_PLAN_ROLE_ARN",
        "vars.AWS_RELEASE_DEPLOY_ROLE_ARN",
        "actions/attest@f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6",
        "create-storage-record: false",
        "target: production",
        "target: break-glass",
        "tools/install_aws_signer_notation.sh",
        "notation verify",
        "describe-images",
        "list-image-referrers",
        "--bundle-from-oci",
        "--deny-self-hosted-runners",
        "--signer-digest",
        "tools/check_container_risk_acceptance.py",
        "validate-change-set",
        "tools/release_supply_chain.py create-manifest",
        "--if-none-match '*'",
        "head-object",
        "environment: production",
        "tools.control_records acquire",
        "tools.control_records release",
        "execute-change-set",
        "describe-task-definition",
        "if: always() && steps.acquire.outputs.acquired == 'true'",
        "retention-days: 90",
    )
    for marker in required:
        if marker not in text:
            raise WorkflowPolicyError(f"Release lacks required policy marker: {marker}")
    if text.count("environment: production") != 1:
        raise WorkflowPolicyError("Release must use production Environment only for deploy")
    if re.search(r"secrets\.AWS[A-Z0-9_]*|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY", text):
        raise WorkflowPolicyError("Release must not use static AWS credentials")
    if re.search(r"runs-on:\s*self-hosted|\b(?:git\s+push|gh\s+pr\s+merge)\b", text):
        raise WorkflowPolicyError("Release contains a forbidden runner or repository mutation")
    secrets = set(re.findall(r"secrets\.([A-Z0-9_]+)", text))
    if secrets != {"DHI_TOKEN", "DHI_USERNAME", "OPERATOR_NOTIFICATION_EMAIL"}:
        raise WorkflowPolicyError("Release secret allowlist changed")
    _require_full_action_pins(text, "Release")


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
