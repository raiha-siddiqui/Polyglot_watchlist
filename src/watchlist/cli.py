"""JSON name matching over fictional records or one explicit SQLite snapshot."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import sys
from typing import Sequence

from watchlist.entities import load_entities
from watchlist.database import load_database
from watchlist.matching import SCORER_NAME, rank_candidates
from watchlist.normalize import normalize_name

RESEARCH_NOTICE = (
    "Possible record match for research review; not identity confirmation. "
    "All reference records are fictional. Similarity scores are not probabilities. "
    "Low scores do not establish absence or provide clearance."
)


def main(argv: Sequence[str] | None = None) -> int:
    """Validate CLI arguments and print candidates; errors exit through argparse."""
    for stream in (sys.stdout, sys.stderr):
        # Windows consoles default to a legacy code page that cannot show many scripts.
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower() != "utf-8":
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Research-only name matching; fictional records are the default."
    )
    parser.add_argument("--name", required=True, help="Name to compare (1–200 characters)")
    parser.add_argument("--top-k", type=int, default=5, help="Maximum candidates (default: 5)")
    parser.add_argument("--db", type=Path, help="Use an explicit frozen SQLite snapshot")
    args = parser.parse_args(argv)
    try:
        normalized_query = normalize_name(args.name)
        if args.db is None:
            entities, provenance, synthetic = load_entities(), None, True
        else:
            entities, provenance, synthetic = load_database(args.db)
        candidates = rank_candidates(args.name, entities, top_k=args.top_k)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))

    result = {
        "query": args.name,
        "normalized_query": normalized_query,
        "scorer": SCORER_NAME,
        "top_k": args.top_k,
        "candidates": [
            {"rank": rank, **{key: value for key, value in asdict(candidate).items()
                              if key != "provenance" or value is not None}}
            for rank, candidate in enumerate(candidates, start=1)
        ],
        "notice": RESEARCH_NOTICE if synthetic else (
            "Possible record match for research review; not identity confirmation. "
            "Reference records are a limited public-data snapshot, not a current complete list. "
            "Similarity scores are not probabilities. A name match establishes neither "
            "identity nor wrongdoing; low scores do not provide clearance."
        ),
    }
    if provenance is not None:
        result["snapshot"] = asdict(provenance)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
