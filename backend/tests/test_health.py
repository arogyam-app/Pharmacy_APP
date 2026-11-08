from app.core.config import settings


def test_health_reports_ok(client):
    response = client.get(f"{settings.api_v1_prefix}/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["version"] == settings.version


def test_unknown_route_uses_error_envelope(client):
    response = client.get(f"{settings.api_v1_prefix}/does-not-exist")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "NOT_FOUND"
