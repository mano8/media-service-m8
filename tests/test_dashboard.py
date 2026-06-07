"""Tests for app/routes/dashboard.py."""

from fastapi.testclient import TestClient


def test_get_dashboard_users_activity(client: TestClient):
    resp = client.get("/media/dashboard/users/activity/")
    assert resp.status_code == 200
    data = resp.json()
    assert "nb_users" in data
    assert "activity" in data


def test_get_dashboard_current_user_stats(client: TestClient):
    resp = client.get("/media/dashboard/users/activity/current/")
    assert resp.status_code == 200
    data = resp.json()
    assert "activity" in data
