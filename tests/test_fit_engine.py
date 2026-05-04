from fit_engine import compute_fit_signal


def test_compute_fit_signal_flags_remote_policy_conflict():
    profile = {
        "search": {
            "status": "active_search",
            "target": {},
            "geography": {"remote_policy": "remote_only", "relocation_excluded": []},
        },
        "culture": {"motivators": [], "deal_breakers": []},
        "skills": [],
        "experience": [],
    }
    role = {"title": "CTO", "remote_policy": "onsite"}

    result = compute_fit_signal(profile, role)

    assert result["overall"]["signal"] == "poor"
    assert "remote policy conflict" in result["fit"]["blockers"][0]
