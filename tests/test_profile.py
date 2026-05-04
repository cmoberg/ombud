from profile import apply_withheld


def test_apply_withheld_removes_nested_field_without_mutating_source():
    source = {
        "identity": {"name": "Carl"},
        "search": {
            "compensation": {"base_minimum_usd": 100},
            "status": "active_search",
        },
        "consent": {"withheld_fields": ["search.compensation"]},
    }

    result = apply_withheld(source)

    assert "compensation" not in result["search"]
    assert "compensation" in source["search"]
