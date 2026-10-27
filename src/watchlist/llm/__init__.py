"""Optional, grounded LLM helpers that never decide or change review outcomes.

Importing this package loads no provider SDK and opens no network connection.
Provider calls sit behind ``client.LLMClient``; tests use fake or replayed
responses. Every model output is validated against the untrusted input or the
frozen evidence packet, and any failure falls back to deterministic behavior.
"""

RESEARCH_LIMITATION = (
    "LLM output is an unverified research aid. It cannot establish identity, "
    "wrongdoing, sanctions status or clearance, and it cannot select, clear or "
    "change any review outcome. Only a human reviewer records outcomes."
)
