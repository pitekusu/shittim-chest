# SPDX-License-Identifier: MIT
"""Required CI checks cannot turn failed or missing work into success."""

import json
import sys
from pathlib import Path

import pytest
from tools.check_ci_scope import (
    check_dependencies,
    check_jobs,
    check_junit_reports,
    check_scope,
    check_steps,
    main,
)


@pytest.mark.parametrize("required", ["true", "false"])
def test_classification_requires_an_explicit_success(required: str) -> None:
    assert check_scope("success", required) is (required == "true")


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "timed_out", ""])
@pytest.mark.parametrize("required", ["true", "false"])
def test_failed_classification_cannot_be_not_applicable(result: str, required: str) -> None:
    with pytest.raises(ValueError, match="classification did not succeed"):
        check_scope(result, required)


@pytest.mark.parametrize("required", ["", "True", "null", "unknown"])
def test_missing_or_invalid_scope_is_rejected(required: str) -> None:
    with pytest.raises(ValueError, match="explicitly publish"):
        check_scope("success", required)


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "timed_out", None])
def test_required_upstream_work_must_succeed(result: str | None) -> None:
    with pytest.raises(ValueError, match="required dependency"):
        check_dependencies(True, {"image": {"result": result}}, ["image"])


def test_irrelevant_upstream_work_does_not_require_execution() -> None:
    check_dependencies(False, {"image": {"result": "skipped"}}, ["image"])
    check_dependencies(True, {"image": {"result": "success"}}, ["image"])


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "timed_out", None])
def test_verification_rejects_missing_unsuccessful_and_masked_steps(result: str | None) -> None:
    with pytest.raises(ValueError, match="verification step"):
        check_steps(True, {"verify": {"outcome": result, "conclusion": "success"}}, ["verify"])


@pytest.mark.parametrize("required", [True, False])
def test_verification_step_must_exist_even_when_not_applicable(required: bool) -> None:
    with pytest.raises(ValueError, match="verification step"):
        check_steps(required, {}, ["verify"])


@pytest.mark.parametrize("required", [True, False])
def test_success_and_not_applicable_are_distinct(required: bool) -> None:
    result = "success" if required else "skipped"
    check_steps(required, {"verify": {"outcome": result, "conclusion": result}}, ["verify"])
    with pytest.raises(ValueError, match="verification step"):
        check_steps(not required, {"verify": {"outcome": result, "conclusion": result}}, ["verify"])


@pytest.mark.parametrize("required", [True, False])
def test_cli_verifies_github_results_and_reports_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, required: bool
) -> None:
    output, summary = tmp_path / "output", tmp_path / "summary"
    monkeypatch.setenv("CI_CHANGES_RESULT", "success")
    monkeypatch.setenv("CI_REQUIRED", str(required).lower())
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    outcome = "success" if required else "skipped"
    monkeypatch.setenv(
        "CI_STEP_RESULTS", json.dumps({"verify": {"outcome": outcome, "conclusion": outcome}})
    )
    monkeypatch.setattr(
        sys, "argv", ["scope", "--github-output", str(output), "--require-step", "verify"]
    )

    assert main() == 0
    assert output.read_text() == f"required={str(required).lower()}\n"
    assert ("not applicable" in summary.read_text()) is not required


def test_aggregate_gate_accepts_a_mixture_of_required_and_irrelevant_jobs() -> None:
    check_jobs(
        "success",
        {"python": "true", "web": "false"},
        {"python-job": {"result": "success"}, "web-job": {"result": "skipped"}},
        ["python-job=python", "web-job=web"],
    )


@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled", "timed_out", None])
def test_aggregate_gate_requires_every_applicable_job(result: str | None) -> None:
    with pytest.raises(ValueError, match="verification job"):
        check_jobs(
            "success", {"python": "true"}, {"python-job": {"result": result}}, ["python-job=python"]
        )


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", ""])
def test_aggregate_gate_rejects_failed_classification(result: str) -> None:
    with pytest.raises(ValueError, match="classification did not succeed"):
        check_jobs(
            result,
            {"python": "false"},
            {"python-job": {"result": "skipped"}},
            ["python-job=python"],
        )


def test_aggregate_gate_rejects_a_missing_scope() -> None:
    with pytest.raises(ValueError, match="explicitly publish"):
        check_jobs("success", {}, {}, ["python-job=python"])


def test_junit_evidence_accepts_actual_agp_suite_shape_recursively(tmp_path: Path) -> None:
    nested = tmp_path / "device"
    nested.mkdir()
    (nested / "TEST-results.xml").write_text(
        '<testsuites tests="2" failures="0" errors="0" skipped="0">'
        '<testsuite tests="2" failures="0" errors="0" skipped="0">'
        '<testcase name="first"/><testcase name="second"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    (tmp_path / "TEST-other.xml").write_text(
        '<testsuite tests="1"><testcase name="third"/></testsuite>', encoding="utf-8"
    )

    assert check_junit_reports(tmp_path) == 3


def test_junit_evidence_rejects_missing_results_not_lint_reports(tmp_path: Path) -> None:
    (tmp_path / "lint-results-debug.xml").write_text("<issues/>", encoding="utf-8")

    with pytest.raises(ValueError, match="reports are missing"):
        check_junit_reports(tmp_path)


@pytest.mark.parametrize(
    "report,error",
    (
        ("<testsuite", "cannot be parsed"),
        ('<testsuites tests="0"/>', "no test cases"),
        ('<testsuite tests="2"><testcase/></testsuite>', "counts do not show passing"),
        ('<testsuite tests="unknown"><testcase/></testsuite>', "invalid counts"),
        ('<testsuite errors="1"><testcase/></testsuite>', "counts do not show passing"),
        ("<testsuite><testcase><failure>PRIVATE</failure></testcase></testsuite>", "failed"),
        ("<testsuite><testcase><error>PRIVATE</error></testcase></testsuite>", "failed"),
        ("<testsuite><testcase><skipped>PRIVATE</skipped></testcase></testsuite>", "skipped"),
    ),
)
def test_junit_evidence_rejects_empty_incomplete_or_unsuccessful_results(
    tmp_path: Path, report: str, error: str
) -> None:
    (tmp_path / "TEST-results.xml").write_text(report, encoding="utf-8")

    with pytest.raises(ValueError, match=error) as raised:
        check_junit_reports(tmp_path)
    assert "PRIVATE" not in str(raised.value)


@pytest.mark.parametrize("required", ("true", "false"))
def test_cli_requires_junit_only_for_applicable_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    required: str,
) -> None:
    monkeypatch.setenv("CI_CHANGES_RESULT", "success")
    monkeypatch.setenv("CI_REQUIRED", required)
    monkeypatch.setattr(sys, "argv", ["scope", "--junit-reports", str(tmp_path)])
    if required == "true":
        (tmp_path / "TEST-results.xml").write_text(
            "<testsuite><testcase/></testsuite>", encoding="utf-8"
        )

    assert main() == 0
    assert capsys.readouterr().out == ("Android tests executed: 1\n" if required == "true" else "")


def test_cli_checks_classification_before_junit_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CI_CHANGES_RESULT", "failure")
    monkeypatch.setenv("CI_REQUIRED", "true")
    monkeypatch.setattr(sys, "argv", ["scope", "--junit-reports", str(tmp_path)])

    with pytest.raises(ValueError, match="classification did not succeed"):
        main()
