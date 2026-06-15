"""Tests for the subscription admin routes (create / list / delete)."""

import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from media_service.db_models.outbox import Subscription

BASE = "/media/v1/admin/subscriptions"


def _payload(**overrides) -> dict:
    body = {
        "url": "https://hook.example.com/events",
        "secret": "a-strong-subscriber-secret-123456",
        "event_types": ["object.ready", "object.deleted"],
    }
    body.update(overrides)
    return body


def _seed(session: Session, **overrides) -> Subscription:
    sub = Subscription(
        url=overrides.get("url", "https://hook.example.com/events"),
        secret=overrides.get("secret", "a-strong-subscriber-secret-123456"),
        event_types=overrides.get("event_types", []),
        active=overrides.get("active", True),
    )
    session.add(sub)
    session.commit()
    session.refresh(sub)
    return sub


# ── create ───────────────────────────────────────────────────────────────────


def test_create_subscription_returns_201_without_secret(
    superuser_client: TestClient, session: Session
):
    resp = superuser_client.post(BASE, json=_payload())
    assert resp.status_code == 201
    body = resp.json()
    assert body["url"] == "https://hook.example.com/events"
    assert body["event_types"] == ["object.ready", "object.deleted"]
    assert body["active"] is True
    # The signing secret must never be returned in a response.
    assert "secret" not in body
    # It is persisted, though.
    stored = session.exec(select(Subscription)).all()
    assert len(stored) == 1
    assert stored[0].secret == "a-strong-subscriber-secret-123456"


def test_create_subscription_rejects_non_http_url(superuser_client: TestClient):
    resp = superuser_client.post(BASE, json=_payload(url="ftp://nope.example.com"))
    assert resp.status_code == 422


def test_create_subscription_rejects_short_secret(superuser_client: TestClient):
    resp = superuser_client.post(BASE, json=_payload(secret="tooshort"))
    assert resp.status_code == 422


def test_create_subscription_requires_superuser(client: TestClient):
    resp = client.post(BASE, json=_payload())
    assert resp.status_code == 403


# ── list ─────────────────────────────────────────────────────────────────────


def test_list_subscriptions_returns_all(superuser_client: TestClient, session: Session):
    _seed(session, url="https://a.example.com/h")
    _seed(session, url="https://b.example.com/h")
    resp = superuser_client.get(BASE)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    urls = {item["url"] for item in body["items"]}
    assert urls == {"https://a.example.com/h", "https://b.example.com/h"}
    assert all("secret" not in item for item in body["items"])


def test_list_subscriptions_empty(superuser_client: TestClient):
    resp = superuser_client.get(BASE)
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "items": []}


def test_list_subscriptions_requires_superuser(client: TestClient):
    resp = client.get(BASE)
    assert resp.status_code == 403


# ── delete ───────────────────────────────────────────────────────────────────


def test_delete_subscription_returns_204(
    superuser_client: TestClient, session: Session
):
    sub = _seed(session)
    resp = superuser_client.delete(f"{BASE}/{sub.id}")
    assert resp.status_code == 204
    assert session.get(Subscription, sub.id) is None


def test_delete_subscription_unknown_returns_404(superuser_client: TestClient):
    resp = superuser_client.delete(f"{BASE}/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_delete_subscription_requires_superuser(client: TestClient, session: Session):
    sub = _seed(session)
    resp = client.delete(f"{BASE}/{sub.id}")
    assert resp.status_code == 403
