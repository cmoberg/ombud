from pathlib import Path

import profile as profile_module
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


def test_read_raw_profile_uses_first_existing_local_profile_dir(monkeypatch, tmp_path):
    packaged_dir = tmp_path / "packaged"
    packaged_dir.mkdir()
    (packaged_dir / "candidate.yaml").write_text("schema_version: '1.0'\n")

    fallback_dir = tmp_path / "fallback"
    fallback_dir.mkdir()
    (fallback_dir / "candidate.yaml").write_text("schema_version: 'stale'\n")

    monkeypatch.setattr(profile_module, "PROFILE_BUCKET", "")
    monkeypatch.setattr(profile_module, "_PROFILE_DIR_CANDIDATES", (packaged_dir, fallback_dir))

    assert profile_module.read_raw_profile("candidate") == "schema_version: '1.0'\n"
