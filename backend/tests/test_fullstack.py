from fastapi.testclient import TestClient

import fullstack_app


def test_unknown_api_route_returns_json_404_not_html():
    client = TestClient(fullstack_app.app)
    response = client.get("/api/this-route-does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"] == "API endpoint not found"


def test_health_still_served_after_spa_fallback_is_added():
    client = TestClient(fullstack_app.app)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
