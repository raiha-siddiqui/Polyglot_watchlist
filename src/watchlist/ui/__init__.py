"""Streamlit reviewer interface over the v2 review API (research only).

The interface is a thin client: every review operation is an HTTP call to
``watchlist.api`` (Completion Step 24), which wraps the one shared v2
``ReviewService``. Nothing here selects, infers or changes a review outcome.
Outputs are possible record matches from fictional reference data for human
review, never identity, wrongdoing, sanctions status or clearance.

Run with ``streamlit run src/watchlist/ui/app.py`` after starting the API
(``python -B -m watchlist.api serve ...``) and setting ``WATCHLIST_API_URL``
and ``WATCHLIST_API_KEY``. This package imports no Streamlit at import time;
only ``app.py`` does.
"""

from __future__ import annotations

__all__ = ["client", "components"]
