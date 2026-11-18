"""The new frontend screens are served and wired (static checks; the API calls behind them are in test_web.py)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from kcd.web.app import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def test_index_has_the_new_views(client: TestClient) -> None:
    page = client.get("/").text
    for needle in ('id="view-director"', 'id="director"', 'id="view-ct"', 'id="ct"', 'id="view-tools"', 'id="tools"'):
        assert needle in page, needle
    assert "not connected in the Python desk" not in page


def test_extras_module_is_served_and_imported(client: TestClient) -> None:
    extras = client.get("/extras.js")
    assert extras.status_code == 200
    assert extras.headers["content-type"].split(";")[0] in ("text/javascript", "application/javascript")
    assert "export function createExtras" in extras.text
    app_js = client.get("/app.js").text
    assert "from '/extras.js'" in app_js and "createExtras(" in app_js
    assert "verification_failed" in app_js and "X.render()" in app_js and "X.onEvent(e)" in app_js
    for tab in ("['ct', 'CT']", "['tools', 'Admin']"):
        assert tab in app_js, tab


def test_css_has_the_new_blocks(client: TestClient) -> None:
    css = client.get("/app.css").text
    for selector in (".console", ".cmsg", ".xgrid", ".xrow"):
        assert selector in css, selector


def test_state_has_what_the_new_screens_read(client: TestClient) -> None:
    s = client.get("/api/state").json()
    assert {"studies", "ct_model", "edge_report", "patients"} <= set(s)
    assert {"mode", "real_available"} <= set(s["ct_model"])
