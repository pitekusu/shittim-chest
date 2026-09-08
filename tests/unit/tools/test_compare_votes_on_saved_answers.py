"""Summarize actual ballot outputs, not guessed quality rankings."""

import json

import pytest
from tools.compare_votes_on_saved_answers import aggregate, read_source, render


def test_wins_and_order_changes_remain_separate():
    cases = [
        {
            "case": 1,
            "results": {
                "legacy_normal": {"winner": "participant-b"},
                "legacy_reversed": {"winner": "participant-a"},
            },
        },
        {
            "case": 2,
            "results": {
                "legacy_normal": {"winner": "participant-b"},
                "legacy_reversed": {"winner": "participant-b"},
            },
        },
    ]
    result = aggregate(cases)
    assert result["legacy"]["wins"]["normal"] == {"participant-b": 2}
    assert result["legacy"]["wins"]["reversed"] == {"participant-a": 1, "participant-b": 1}
    assert result["legacy"]["winner_changed_cases"] == [1]
    assert result["composite"]["completed_cases"] == 0


def test_report_retains_full_reason_and_scores_as_escaped_text():
    reason = "<script>text</script>" + "reason" * 80
    assessment = {
        "candidate": "participant-b",
        "entertainment": 4,
        "character": 3,
        "originality": 2,
        "responsiveness": 5,
        "interaction": 3,
        "reason": reason,
        "total": 69,
    }
    case = {
        "case": 1,
        "question": "fixture",
        "speeches": {"initial": [], "final": []},
        "results": {
            "composite_normal": {
                "winner": "participant-b",
                "decided_by": "fixture",
                "ballots": [
                    {
                        "voter": "participant-a",
                        "candidate": "participant-b",
                        "assessments": [assessment],
                    }
                ],
            }
        },
    }
    result = render({"state": "fixture", "cases": [case]})
    assert "<script>" not in result
    assert "&lt;script&gt;text&lt;/script&gt;" + "reason" * 80 in result
    assert "69 / 100" in result


def test_incomplete_source_is_rejected_before_generation(tmp_path):
    path = tmp_path / "source.json"
    path.write_text(json.dumps({"state": "生成中", "completed": 9}))
    with pytest.raises(ValueError, match="completed"):
        read_source(path)
