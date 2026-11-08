"""Serving the built SPA from the API process.

The native Windows deployment has no nginx and no Docker: one uvicorn serves
both the app and the API on one port, which is what makes them the same origin
and the session cookie work without a `SameSite` exception.

Two things have to hold, and neither is obvious from reading the mount:

* the catch-all must not shadow the API, `/docs` or `/openapi.json`;
* `/{spa_path:path}` comes straight off the URL, so `../backend/.env` is a
  request this server will be sent. It must never read outside the build.
"""

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def spa_client(tmp_path, monkeypatch):
    """An app instance with SERVE_SPA on, against a fake build directory."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>app</title>", encoding="utf-8")
    (dist / "assets" / "main-abc123.js").write_text("console.log(1)", encoding="utf-8")

    # A file that must never be reachable through the SPA route.
    (tmp_path / "secret.env").write_text("SECRET_KEY=do-not-serve-me", encoding="utf-8")

    from app.core import config

    monkeypatch.setattr(config.settings, "serve_spa", True, raising=False)
    monkeypatch.setattr(config.settings, "spa_dist_dir", str(dist), raising=False)

    from app import main

    importlib.reload(main)
    with TestClient(main.create_app()) as client:
        yield client

    # Leave the module as the rest of the suite expects to find it.
    monkeypatch.setattr(config.settings, "serve_spa", False, raising=False)
    importlib.reload(main)


def test_the_app_is_served_at_the_root(spa_client):
    response = spa_client.get("/")
    assert response.status_code == 200
    assert "<title>app</title>" in response.text


def test_a_deep_link_still_loads_the_app(spa_client):
    """Vue Router owns the URL space: /medicine/inventory is a route, not a file.

    Without the fallback, refreshing any screen but the home page 404s.
    """
    response = spa_client.get("/medicine/inventory")
    assert response.status_code == 200
    assert "<title>app</title>" in response.text


def test_index_is_never_cached(spa_client):
    """It names the hashed bundles, so a cached copy pins the old app."""
    response = spa_client.get("/")
    assert "no-store" in response.headers.get("cache-control", "")


def test_hashed_assets_are_served(spa_client):
    response = spa_client.get("/assets/main-abc123.js")
    assert response.status_code == 200
    assert "console.log(1)" in response.text


def test_the_api_is_not_shadowed(spa_client):
    """The catch-all is registered last, so real routes match first."""
    response = spa_client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_the_docs_are_not_shadowed(spa_client):
    assert spa_client.get("/openapi.json").status_code == 200
    assert spa_client.get("/docs").status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "../secret.env",
        "../../secret.env",
        "..%2f..%2fsecret.env",
        "%2e%2e/secret.env",
        "assets/../../secret.env",
    ],
)
def test_nothing_outside_the_build_can_be_read(spa_client, path):
    """A traversal falls through to index.html rather than reading the disk.

    The file exists, one directory up from the build, and holds a secret. If
    the guard ever comes out, this is how it shows up.
    """
    response = spa_client.get(f"/{path}")
    assert "do-not-serve-me" not in response.text
    assert response.status_code == 200
    assert "<title>app</title>" in response.text


def test_a_missing_build_does_not_stop_the_api(tmp_path, monkeypatch):
    """A forgotten `npm run build` is a deployment slip, not a reason to die.

    The API is still perfectly usable, and the log says what is wrong.
    """
    import importlib

    from app.core import config

    monkeypatch.setattr(config.settings, "serve_spa", True, raising=False)
    monkeypatch.setattr(config.settings, "spa_dist_dir", str(tmp_path / "nope"), raising=False)

    from app import main

    importlib.reload(main)
    with TestClient(main.create_app()) as client:
        assert client.get("/api/v1/health").status_code == 200

    monkeypatch.setattr(config.settings, "serve_spa", False, raising=False)
    importlib.reload(main)
