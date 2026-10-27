"""Shared fixtures for the reviewer-interface tests (collects no tests).

A real v2 ``ReviewService`` in a temporary workspace sits behind the real
FastAPI app; the UI's ``WatchlistClient`` talks to it through FastAPI's
``TestClient`` (an ``httpx.Client``), injected into Streamlit session state.
No network, no model download; all names are authored fictional fixtures.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import secrets
from typing import Any, Iterator, Sequence

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("streamlit")

from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from review_v2_support import ROOT, FakeRetriever, make_config
from watchlist.api import ApiSettings, create_app
from watchlist.api.auth import ApiKey, hash_key
from watchlist.review.service import ReviewService
from watchlist.ui.client import ClientConfig, WatchlistClient

APP_PATH = ROOT / "src" / "watchlist" / "ui" / "app.py"
REVIEWER_KEY = secrets.token_urlsafe(24)
VIEWER_KEY = secrets.token_urlsafe(24)
KEYS = (
    ApiKey("fictional-reviewer", "reviewer", hash_key(REVIEWER_KEY)),
    ApiKey("fictional-viewer", "viewer", hash_key(VIEWER_KEY)),
)
RUN_TIMEOUT = 60


@contextmanager
def live_api(
    tmp_path: Path,
    retriever: Any = None,
    *,
    context_providers: Sequence[Any] = (),
    key: str = REVIEWER_KEY,
) -> Iterator[tuple[WatchlistClient, ReviewService]]:
    """A UI client wired to a real API + service over a temporary workspace."""
    service = ReviewService.initialize(
        tmp_path / "ws",
        make_config(),
        retriever=retriever or FakeRetriever(),
        context_providers=tuple(context_providers),
    )
    app = create_app(ApiSettings(api_keys=KEYS), service=service)
    with TestClient(app) as http:
        client = WatchlistClient(ClientConfig("http://127.0.0.1:8000", key), http=http)
        yield client, service


def app_test(client: Any) -> AppTest:
    """The real ``app.py`` with ``client`` injected, after its first run."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=RUN_TIMEOUT)
    at.session_state["_watchlist_ui_client"] = client
    at.run()
    return at


def html_text(at: AppTest) -> str:
    """Concatenated bodies of every ``st.html`` element on the page."""
    return "\n".join(element.proto.body for element in at.get("html"))


def page_text(at: AppTest) -> str:
    """Every rendered text value except ``st.html`` bodies."""
    parts: list[str] = []
    for kind in (
        "markdown",
        "caption",
        "warning",
        "error",
        "success",
        "info",
        "title",
        "header",
        "subheader",
    ):
        parts.extend(str(element.value) for element in at.get(kind))
    return "\n".join(parts)


def screen(at: AppTest, name: str, origin: str = "test") -> AppTest:
    """Fill and submit the screening form."""
    at.text_input(key="screen_name").input(name)
    at.selectbox(key="screen_origin").select(origin)
    submit = next(b for b in at.get("button") if b.label == "Screen name")
    submit.click()
    return at.run()


def button(at: AppTest, label: str):
    """The first button with this label."""
    for item in at.button:
        if item.label == label:
            return item
    raise AssertionError(f"no button {label!r}")


def form_ids(at: AppTest) -> str:
    """Submission ID of the review form on screen."""
    radio = next(r for r in at.radio if r.key and r.key.startswith("outcome_"))
    return radio.key.removeprefix("outcome_")
