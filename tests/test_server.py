from starlette.testclient import TestClient

import log_store
import server


def _profile(employer_visible=True, withheld_fields=None):
    return {
        "identity": {"name": "Carl", "links": {"linkedin": "https://example.com/in/carl"}},
        "experience": [],
        "education": [],
        "skills": [],
        "search": {
            "status": "active_search",
            "target": {},
            "geography": {},
            "compensation": {"base_minimum_usd": 100},
        },
        "culture": {},
        "consent": {
            "employer_visible": employer_visible,
            "withheld_fields": withheld_fields or ["search.compensation"],
        },
    }

def test_homepage_requires_auth(monkeypatch):
    monkeypatch.setattr(server, "_USER_TOKEN", "user-token")
    monkeypatch.setattr(server, "_ADMIN_TOKEN", "admin-token")

    client = TestClient(server.app)
    response = client.get("/")

    assert response.status_code == 401
    assert "Enter the access token" in response.text


def test_profile_api_get_accepts_bearer_token(monkeypatch):
    monkeypatch.setattr(server, "_USER_TOKEN", "user-token")
    monkeypatch.setattr(server, "_ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(server, "read_raw_profile", lambda candidate_id: "schema_version: '1.0'\n")

    client = TestClient(server.app)
    response = client.get(
        "/api/profile/cmoberg",
        headers={"Authorization": "Bearer user-token"},
    )

    assert response.status_code == 200
    assert "schema_version" in response.text


def test_logs_api_requires_auth(monkeypatch):
    monkeypatch.setattr(server, "_USER_TOKEN", "user-token")
    monkeypatch.setattr(server, "_ADMIN_TOKEN", "admin-token")

    client = TestClient(server.app)
    response = client.get("/api/logs")

    assert response.status_code == 401


def test_get_profile_returns_not_visible_when_candidate_hidden(monkeypatch):
    monkeypatch.setattr(server, "load_profile", lambda _candidate_id: _profile(employer_visible=False))

    result = server.get_profile()

    assert result["error"] == "candidate_not_visible"


def test_get_profile_applies_withheld_fields(monkeypatch):
    monkeypatch.setattr(
        server,
        "load_profile",
        lambda _candidate_id: _profile(withheld_fields=["identity.links"]),
    )

    result = server.get_profile()

    assert result["identity"]["name"] == "Carl"
    assert "links" not in result["identity"]


def test_get_fit_signal_logs_redacted_company_name(monkeypatch):
    monkeypatch.setattr(server, "load_profile", lambda _candidate_id: _profile())
    log_store._entries.clear()

    result = server.get_fit_signal(role_title="CTO", company_name="Secret Co")

    assert result["schema_version"] == "1.0"
    entry = log_store.all_entries()[0]
    assert entry["inputs"] == {"role_title": "CTO", "has_company_name": True}
