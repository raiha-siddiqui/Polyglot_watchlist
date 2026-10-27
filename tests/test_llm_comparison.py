"""Offline Step 18 harness mechanics. Judge responses here are fixtures, never LLM results."""

import hashlib
import json
from pathlib import Path
import sys

import pytest

from watchlist.llm import comparison
from watchlist.llm.client import FakeClient, load_config, make_receipt
from watchlist.llm.schemas import LLMError

CONFIG = Path(comparison.DEFAULT_CONFIG)


def ids_from(request):
    payload = request.user.split("\n", 1)[1].rsplit("\n", 1)[0]
    return [item["id"] for item in json.loads(payload)["shortlist"]]


def ranking_client(order=lambda ids: ids, **extra):
    return FakeClient(
        lambda r: json.dumps({"ranking": order(ids_from(r)), "abstain": False, **extra})
    )


@pytest.fixture
def setup():
    config = comparison.load_comparison_config(CONFIG)
    return config, load_config(config.llm_config_path)


def test_preflight_lists_transmitted_fields_and_blocks_live(setup):
    report = comparison.preflight(*setup)
    assert report["cases"] == 12 and report["live_allowed"] is False
    assert (
        "similarity_score" not in report["transmitted_fields"]
        and "aliases" in report["transmitted_fields"]
    )
    assert report["blockers"][0] == "comparison live.enabled is false"
    assert (
        "live.approved_partition None != partition 'fictional-regression-only'"
        in report["blockers"]
    )
    assert report["regression_only"] is True and report["worst_case_cost_usd"] > 0


def test_judge_sees_equal_evidence_without_baseline_rank_or_scores(setup):
    config, llm_config = setup
    fake = ranking_client()
    records, receipts = comparison.run_cases(config, fake, llm_config)
    assert receipts == []
    for request, record in zip(fake.calls, records):
        shown = ids_from(request)
        assert shown == sorted(record["baseline_order"])
        assert (
            "similarity_score" not in request.user
            and "baseline_rank" not in request.user
        )
        assert "matched_alias" not in request.user
        payload = json.loads(request.user.split("\n", 1)[1].rsplit("\n", 1)[0])
        assert all(
            isinstance(item["aliases"], list) and item["aliases"]
            for item in payload["shortlist"]
        )


def test_fixture_run_is_labelled_and_counts_have_denominators(setup, tmp_path):
    config, llm_config = setup
    records, receipts = comparison.run_cases(config, ranking_client(), llm_config)
    report = comparison.write_report(
        tmp_path / "out", config, llm_config, "fixture", records, receipts
    )
    summary = report["summary"]
    assert (
        report["status"] == "fixture_only_not_an_llm_result"
        and report["regression_only"]
    )
    assert (
        summary["cases"] == 12
        and summary["absent_queries"] == 1
        and summary["cases_with_target"] == 11
    )
    assert summary["llm_valid_outputs"] == {"count": 12, "denominator": 12}
    assert summary["input_tokens"] is None and summary["cost_usd_estimate"] is None
    assert summary["cost_usd_per_1000_queries"] is None
    assert summary["latency_ms"] == {"measured_calls": 0, "median": None, "p95": None}
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "cases.jsonl",
        "report.json",
    ]


def test_retrieval_misses_are_separate_from_llm_errors(setup):
    config, llm_config = setup
    records, _ = comparison.run_cases(config, ranking_client(), llm_config)
    for r in records:
        if r["retrieval_miss"]:
            assert r["llm_target_rank"] is None and r["baseline_target_rank"] is None
    absent = next(r for r in records if r["absent_query"])
    assert absent["retrieval_miss"] is False and absent["llm_target_rank"] is None


@pytest.mark.parametrize(
    "client, code",
    [
        (ranking_client(lambda ids: ids[:-1]), "invalid_output:invalid_ranking"),
        (
            ranking_client(lambda ids: ids + ["syn-999"]),
            "invalid_output:invalid_ranking",
        ),
        (ranking_client(lambda ids: ids + ids[:1]), "invalid_output:invalid_ranking"),
        (ranking_client(confidence=0.97), "invalid_output:wrong_schema"),
        (
            FakeClient({"judge": "I think syn-001 is the same person."}),
            "invalid_output:invalid_json",
        ),
        (FakeClient(lambda r: RuntimeError("boom")), "client_error:RuntimeError"),
    ],
)
def test_invalid_judgements_are_recorded_not_imputed(setup, client, code):
    config, llm_config = setup
    records, _ = comparison.run_cases(config, client, llm_config)
    assert all(r["llm_status"] == code and r["llm_order"] is None for r in records)
    summary = comparison.summarize(records)
    assert summary["llm_errors"] == {code: 12}
    assert summary["llm_top1_given_retrieved_and_valid"] == {
        "hits": 0,
        "denominator": 0,
    }
    assert comparison.status_for(summary) == "BLOCKED_no_llm_outputs"


def test_replay_without_receipts_is_blocked(tmp_path, capsys):
    receipts = tmp_path / "receipts.json"
    receipts.write_text("[]", encoding="utf-8")
    out = tmp_path / "replay"
    assert (
        comparison.main(
            [
                "replay",
                "--config",
                str(CONFIG),
                "--receipts",
                str(receipts),
                "--output",
                str(out),
            ]
        )
        == 0
    )
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "BLOCKED_no_llm_outputs"
    assert report["summary"]["llm_errors"] == {"client_error:replay_miss": 12}
    assert (
        comparison.main(
            [
                "replay",
                "--config",
                str(CONFIG),
                "--receipts",
                str(receipts),
                "--output",
                str(out),
            ]
        )
        == 2
    )
    assert "must not already exist" in capsys.readouterr().err


def test_replay_of_recorded_fixture_receipts_is_deterministic(setup, tmp_path):
    config, llm_config = setup
    fake = ranking_client(lambda ids: list(reversed(ids)))
    comparison.run_cases(config, fake, llm_config)
    receipts = [
        make_receipt(req, fake.complete(req), "2026-10-26T00:00:00+00:00")
        for req in fake.calls[:12]
    ]
    path = tmp_path / "receipts.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in receipts), encoding="utf-8")
    outputs = []
    for name in ("a", "b"):
        args = [
            "replay",
            "--config",
            str(CONFIG),
            "--receipts",
            str(path),
            "--output",
            str(tmp_path / name),
        ]
        assert comparison.main(args) == 0
        outputs.append((tmp_path / name / "cases.jsonl").read_bytes())
    assert outputs[0] == outputs[1]
    report = json.loads((tmp_path / "a" / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "fixture_only_not_an_llm_result"


def test_live_run_is_refused_without_approval_and_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(
        sys.modules, "anthropic", None
    )  # any import attempt would fail loudly
    for extra in ([], ["--allow-live"]):
        out = tmp_path / f"live{len(extra)}"
        assert (
            comparison.main(
                ["run", "--config", str(CONFIG), "--output", str(out), *extra]
            )
            == 2
        )
        assert not out.exists()
    err = capsys.readouterr().err
    assert (
        "live_blocked" in err
        and "--allow-live not passed" in err
        and "provider not approved" in err
    )


def test_status_labels():
    base = {
        "llm_valid_outputs": {"count": 3},
        "llm_errors": {},
        "evidence_kinds": ["replayed_receipt"],
    }
    assert (
        comparison.status_for(base, regression_only=False)
        == "complete_on_this_partition"
    )
    assert comparison.status_for(base) == "regression_only_not_a_comparison_result"
    assert (
        comparison.status_for({**base, "llm_errors": {"x": 1}})
        == "incomplete_with_recorded_llm_errors"
    )
    assert (
        comparison.status_for(
            {**base, "evidence_kinds": ["fake_fixture", "replayed_receipt"]}
        )
        == "fixture_only_not_an_llm_result"
    )


def test_config_validation_rejects_leaky_ordering(tmp_path):
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    data["shortlist_order"] = "baseline_rank"
    path = tmp_path / "c.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="baseline rank"):
        comparison.load_comparison_config(path)


def test_validate_judgement_contract():
    assert comparison.validate_judgement(
        '{"ranking": ["b", "a"], "abstain": true}', ["a", "b"], 1000
    ) == (["b", "a"], True)
    with pytest.raises(LLMError):
        comparison.validate_judgement(
            '{"ranking": ["a"], "abstain": "no"}', ["a"], 1000
        )


def write_config(tmp_path, name="c.json", **changes):
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    for key, value in changes.items():
        data[key] = value
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "partition", ["development", "final_test", "fictional-but-real", None]
)
def test_partition_must_be_allowed_for_the_reference(tmp_path, partition):
    with pytest.raises(ValueError, match="partition must be one of"):
        comparison.load_comparison_config(write_config(tmp_path, partition=partition))


def fixture_receipts(tmp_path, setup, relabel=False):
    config, llm_config = setup
    fake = ranking_client(lambda ids: list(reversed(ids)))
    comparison.run_cases(config, fake, llm_config)
    receipts = [
        make_receipt(req, fake.complete(req), "2026-10-26T00:00:00+00:00")
        for req in fake.calls[:12]
    ]
    if relabel:
        # Forgery probe: the hash is trivially recomputable, so origin labels prove nothing.
        receipts = [
            {
                **r,
                "evidence_kind": "live_provider",
                "model": llm_config.model,
                "provider": "anthropic",
                "input_tokens": 100,
                "output_tokens": 20,
                "cost_usd": 0.0008,
                "latency_ms": 5.0,
            }
            for r in receipts
        ]
    path = tmp_path / "receipts.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in receipts), encoding="utf-8")
    return path


def test_relabelled_receipts_on_fictional_data_stay_regression_only(setup, tmp_path):
    path = fixture_receipts(tmp_path, setup, relabel=True)
    out = tmp_path / "out"
    assert (
        comparison.main(
            [
                "replay",
                "--config",
                str(CONFIG),
                "--receipts",
                str(path),
                "--output",
                str(out),
            ]
        )
        == 0
    )
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert (
        report["regression_only"] is True
        and report["status"] == "regression_only_not_a_comparison_result"
    )
    assert (
        report["receipts"]["authentication"]
        == "sha256 integrity only; origin not authenticated"
    )
    assert report["receipts"]["pinned"] is False
    assert report["summary"]["cost_usd_per_1000_queries"] == pytest.approx(0.8)


def test_receipts_file_can_be_pinned_to_its_recorded_hash(setup, tmp_path, capsys):
    path = fixture_receipts(tmp_path, setup)
    good = hashlib.sha256(path.read_bytes()).hexdigest()
    base = ["replay", "--config", str(CONFIG), "--receipts", str(path)]
    assert (
        comparison.main(
            base + ["--receipts-sha256", "0" * 64, "--output", str(tmp_path / "bad")]
        )
        == 2
    )
    assert (
        not (tmp_path / "bad").exists() and "pinned SHA-256" in capsys.readouterr().err
    )
    assert (
        comparison.main(
            base + ["--receipts-sha256", good, "--output", str(tmp_path / "ok")]
        )
        == 0
    )
    report = json.loads((tmp_path / "ok" / "report.json").read_text(encoding="utf-8"))
    assert report["receipts"] == {
        "source": "replay",
        "file_sha256": good,
        "pinned": True,
        "authentication": "sha256 integrity only; origin not authenticated",
    }


@pytest.mark.parametrize(
    "change", [{"input_tokens": "10"}, {"latency_ms": -1}, {"cost_usd": "NaN"}]
)
def test_malformed_receipts_fail_cleanly_without_partial_output(
    setup, tmp_path, capsys, change
):
    path = fixture_receipts(tmp_path, setup)
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    lines[0].update(change)
    path.write_text("".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")
    out = tmp_path / "out"
    assert (
        comparison.main(
            [
                "replay",
                "--config",
                str(CONFIG),
                "--receipts",
                str(path),
                "--output",
                str(out),
            ]
        )
        == 2
    )
    assert not out.exists() and "invalid_receipt" in capsys.readouterr().err


def approved_live_configs(tmp_path):
    from test_llm_contracts import approved_config

    llm = approved_config(
        tmp_path,
        max_requests_per_run=12,
        approval={
            "provider_approved": True,
            "data_transfer_approved": True,
            "retention_approved": True,
            "budget_usd": 5.0,
        },
    )
    path = write_config(
        tmp_path,
        "live.json",
        llm_config=str(tmp_path / "llm.json"),
        live={"enabled": True, "approved_partition": "fictional-regression-only"},
    )
    return path, llm


def test_live_run_keeps_paid_receipts_when_a_later_step_fails(
    tmp_path, monkeypatch, capsys
):
    from test_llm_contracts import FakeSDK

    fake = FakeSDK()
    monkeypatch.setitem(sys.modules, "anthropic", fake.module)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    config_path, llm = approved_live_configs(tmp_path)
    check = comparison.preflight(comparison.load_comparison_config(config_path), llm)
    assert check["live_allowed"], check["blockers"]
    calls = {"n": 0}
    real = comparison.validate_judgement

    def crash_on_third(*args):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("disk went away")
        return real(*args)

    monkeypatch.setattr(comparison, "validate_judgement", crash_on_third)
    out = tmp_path / "live-out"
    assert (
        comparison.main(
            ["run", "--config", str(config_path), "--output", str(out), "--allow-live"]
        )
        == 2
    )
    assert len(fake.created) == 3
    kept = (out / "receipts.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(kept) == 3 and all(
        json.loads(line)["evidence_kind"] == "live_provider" for line in kept
    )
    assert not (out / "report.json").exists()
    assert "test-key-not-real" not in "".join(kept) + capsys.readouterr().err


def test_live_run_writes_report_with_receipts_hash(tmp_path, monkeypatch):
    from test_llm_contracts import FakeSDK

    monkeypatch.setitem(sys.modules, "anthropic", FakeSDK().module)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    config_path, _ = approved_live_configs(tmp_path)
    out = tmp_path / "live-out"
    assert (
        comparison.main(
            ["run", "--config", str(config_path), "--output", str(out), "--allow-live"]
        )
        == 0
    )
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    receipts = out / "receipts.jsonl"
    assert (
        report["receipts"]["file_sha256"]
        == hashlib.sha256(receipts.read_bytes()).hexdigest()
    )
    # The fake SDK returns non-judgement text, so every case is an invalid output, never imputed.
    assert (
        report["status"] == "BLOCKED_no_llm_outputs"
        and report["regression_only"] is True
    )
    assert report["summary"]["llm_errors"] == {"invalid_output:wrong_schema": 12}
    replay = tmp_path / "replayed"
    assert (
        comparison.main(
            [
                "replay",
                "--config",
                str(config_path),
                "--receipts",
                str(receipts),
                "--receipts-sha256",
                report["receipts"]["file_sha256"],
                "--output",
                str(replay),
            ]
        )
        == 0
    )
    assert (replay / "cases.jsonl").read_text(encoding="utf-8").count(
        "invalid_output:wrong_schema"
    ) == 12


def test_live_run_refused_when_output_cannot_be_created_first(tmp_path, monkeypatch):
    from test_llm_contracts import FakeSDK

    fake = FakeSDK()
    monkeypatch.setitem(sys.modules, "anthropic", fake.module)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    config_path, _ = approved_live_configs(tmp_path)
    parent = tmp_path / "a-file"
    parent.write_text("x", encoding="utf-8")
    assert (
        comparison.main(
            [
                "run",
                "--config",
                str(config_path),
                "--output",
                str(parent / "out"),
                "--allow-live",
            ]
        )
        == 2
    )
    assert fake.created == []
