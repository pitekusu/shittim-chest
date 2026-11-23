"""Resolve the opt-in Core release parameter without exposing stack outputs."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess

_PARAMETER = "StudentGachaEnabled"


def resolve_student_gacha_rollout(choice: str, stack: object) -> str:
    if choice in {"true", "false"}:
        return choice
    if choice != "keep" or not isinstance(stack, dict):
        raise ValueError("student gacha rollout is invalid")
    parameters = stack.get("Parameters", [])
    if not isinstance(parameters, list) or any(not isinstance(item, dict) for item in parameters):
        raise ValueError("runtime stack parameters are invalid")
    matches = [
        item.get("ParameterValue") for item in parameters if item.get("ParameterKey") == _PARAMETER
    ]
    if not matches:
        return "false"
    if len(matches) != 1 or matches[0] not in ("true", "false"):
        raise ValueError("deployed student gacha parameter is invalid")
    return str(matches[0])


def _deployed_stack(region: str) -> object:
    aws = shutil.which("aws")
    if aws is None or region != "ap-northeast-1":
        raise ValueError("runtime stack lookup is invalid")
    result = subprocess.run(  # noqa: S603 - installed AWS CLI with fixed arguments
        [
            aws,
            "cloudformation",
            "describe-stacks",
            "--region",
            region,
            "--stack-name",
            "ShittimChest-Prod-Runtime",
            "--query",
            "Stacks[0].{Parameters: Parameters}",
            "--output",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        if "(ValidationError)" in result.stderr and "does not exist" in result.stderr:
            return {}
        raise ValueError("runtime stack lookup failed")
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("choice", choices=("keep", "true", "false"))
    parser.add_argument("--region", default="ap-northeast-1")
    args = parser.parse_args()
    try:
        stack = _deployed_stack(args.region) if args.choice == "keep" else {}
        print(resolve_student_gacha_rollout(args.choice, stack))
    except ValueError, OSError:
        parser.exit(1, "student gacha rollout could not be resolved\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
