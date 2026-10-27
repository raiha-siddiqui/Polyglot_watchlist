"""Research-only HTTP API over the shared v2 review service (Completion Step 24).

Requires the ``api`` extra (FastAPI, uvicorn, httpx). The API returns possible
record matches for human review over fictional data; it never establishes
identity, wrongdoing, sanctions status or clearance. See ``docs/api.md``.

``create_app(settings, *, service=None, context_providers=())`` builds the app;
``python -m watchlist.api serve|openapi|new-key|hash-key`` is the command line.
"""

from watchlist.api.app import API_VERSION, case_revision, create_app, openapi_document
from watchlist.api.settings import ApiSettings, SettingsError

__all__ = [
    "API_VERSION",
    "ApiSettings",
    "SettingsError",
    "case_revision",
    "create_app",
    "openapi_document",
]
