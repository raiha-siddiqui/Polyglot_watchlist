"""Fictional episode evaluation: adversarial subset end to end and scorer sanity."""

import copy
import json
from pathlib import Path

from watchlist.case_memory.evaluation import Totals, run_evaluation, score_episode

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "eval" / "configs" / "memory.json"
FIXTURE = ROOT / "eval" / "fixtures" / "memory-episodes.json"


def test_fixture_shape_groups_and_splits():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    episodes = fixture["episodes"]
    assert len(episodes) == 24
    groups = {e["group"] for e in episodes}
    assert len(groups) == 12
    assert all(sum(e["group"] == g for e in episodes) == 2 for g in groups)
    dev, held = fixture["splits"]["development"], fixture["splits"]["held_out"]
    assert set(dev).isdisjoint(held) and set(dev) | set(held) == groups
    # The same-name pair stays inside one split.
    assert {"syn-001", "syn-012"} <= set(dev)


def test_adversarial_episodes_have_zero_wrong_links_and_ranking_parity():
    report = run_evaluation(
        CONFIG, episode_ids=["e01", "e02", "e12", "e18", "e24"], root=ROOT
    )
    totals = report["totals"]["all"]
    assert report["episodes_passed"] == report["episodes_total"] == 5, report[
        "episodes"
    ]
    assert totals["wrong_links"] == 0 and totals["stale_exposures"] == 0
    assert totals["ranking_parity"] == "5/5"
    assert totals["displayed_links"] == totals["correct_links"] == 4


def _result(cards_by_entity, *, labels, expected, **episode_extra):
    episode = {"episode_id": "x", "expected": expected, **episode_extra}
    keys = [
        {
            "source_entity_id": entity,
            "cards": [
                {"case_id": case, "relation": "selected_for_follow_up"}
                for case in cases
            ],
            "eligible_count": len(cases),
            "omitted": {},
            "differing_outcomes": False,
        }
        for entity, cases in cards_by_entity.items()
    ]
    return {
        "episode": episode,
        "labels": labels,
        "context": {"status": "ok", "pending_publications": 0, "keys": keys},
        "parity": True,
        "quarantined": 0,
        "fresh_after": None,
        "memory_ok": True,
    }


def test_scorer_counts_a_planted_wrong_link_and_stale_exposure():
    """Hand-calculated: 2 cards shown, 1 wrong (name-based link) -> precision 1/2."""
    totals = Totals()
    result = _result(
        {"syn-001": ["c1"], "syn-012": ["c1"]},
        labels={"c1": "h1"},
        expected={"syn-001": ["h1"], "syn-012": []},
        stale_labels=["h1"],
    )
    problems = score_episode(result, totals)
    report = totals.report()
    assert report["displayed_links"] == 2 and report["wrong_links"] == 1
    assert report["link_precision"] == 0.5
    assert report["episodes_with_wrong_link"] == 1
    assert report["stale_exposures"] == 2
    assert any("wrong link h1 under syn-012" in p for p in problems)


def test_scorer_reports_missing_cards_and_parity_failure():
    totals = Totals()
    result = _result({"syn-003": []}, labels={}, expected={"syn-003": ["h1", "h2"]})
    result["parity"] = False
    problems = score_episode(copy.deepcopy(result), totals)
    report = totals.report()
    assert report["recall_at_cap"] == 0.0 and report["expected_cards"] == 2
    assert report["ranking_parity"] == "0/1"
    assert any("ranking differs" in p for p in problems)
