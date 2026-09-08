from tools.evaluate_combined_random import select_records, summarize


def test_random_sample_is_unique_reproducible_and_independent_of_catalog_order():
    catalog = [{"PK": {"S": f"fixture-{i:03d}"}} for i in range(50)]
    selected = select_records(catalog, 734)
    assert len(selected) == len({x["PK"]["S"] for x in selected}) == 20
    assert selected == select_records(list(reversed(catalog)), 734)
    assert selected != select_records(catalog, 735)


def test_disagreement_is_not_counted_as_improvement():
    axes = {
        "answers_request": "yes",
        "persona_contradiction": "absent",
        "positions": "shared_but_personal",
        "final_preserves_personal_stances": "yes",
        "forced_disagreement": "absent",
    }
    first = {"preferred": "combined", "assessments": {"released": axes, "combined": axes}}
    second = {**first, "preferred": "released"}
    result = summarize([first, second])
    assert result["outcome"] == "disputed"
    second = {
        **first,
        "assessments": {"released": axes, "combined": {**axes, "answers_request": "no"}},
    }
    assert summarize([first, second])["stable_assessment"] is False
