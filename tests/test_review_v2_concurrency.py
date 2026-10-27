"""Exactly-one acceptance and bounded lock waits across threads and processes."""

from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import threading
import time

import pytest

from review_v2_support import (
    FakeRetriever,
    make_config,
    submission,
    worker_command,
    worker_env,
)
from watchlist.review import contracts as c
from watchlist.review.locking import validated_timeout, workspace_lock
from watchlist.review.service import ReviewService


@pytest.fixture
def pending(tmp_path):
    root = tmp_path / "race"
    service = ReviewService.initialize(
        root, make_config(lock_timeout=60.0), retriever=FakeRetriever()
    )
    return (
        root,
        service,
        service.start_case("Luma Vesper", origin="test", actor="fictional-analyst"),
    )


def attempt(service, case_id, form):
    try:
        return service.submit_review(case_id, form).status
    except c.ReviewError as exc:
        return exc.code


@pytest.mark.parametrize("shared", [True, False])
def test_threads_racing_different_reviews_accept_exactly_one(pending, shared):
    root, service, view = pending
    forms = [submission(view, reason=f"Fictional reviewer {i}.") for i in range(6)]
    services = [
        service
        if shared
        else ReviewService.open(
            root, make_config(lock_timeout=60.0), retriever=FakeRetriever(fail=True)
        )
        for _ in forms
    ]
    with ThreadPoolExecutor(max_workers=len(forms)) as pool:
        results = list(
            pool.map(
                lambda pair: attempt(pair[0], view.case_id, pair[1]),
                zip(services, forms),
            )
        )
    assert sorted(results) == ["completed"] + ["stale_submission"] * 5
    final = service.get_case(view.case_id)
    winner = forms[results.index("completed")]
    assert final.review.submission.to_dict() == winner
    assert [e.event_type for e in service.audit(view.case_id).events].count(
        "case_finalized"
    ) == 1


def test_threads_retrying_the_same_review_produce_one_effect(pending):
    _, service, view = pending
    form = submission(view)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(
            pool.map(lambda _: attempt(service, view.case_id, form), range(5))
        )
    assert results == ["completed"] * 5
    assert [e.event_type for e in service.audit(view.case_id).events].count(
        "review_submitted"
    ) == 1


def test_concurrent_case_creation_assigns_unique_ordered_cases(tmp_path):
    service = ReviewService.initialize(
        tmp_path / "many", make_config(lock_timeout=60.0), retriever=FakeRetriever()
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        views = list(
            pool.map(
                lambda i: service.start_case(
                    f"Fictional {i}", origin="test", actor="t"
                ),
                range(4),
            )
        )
    listed = service.list_cases()
    assert sorted(v.case_id for v in views) == sorted(s.case_id for s in listed)
    assert len({s.case_id for s in listed}) == 4


def test_processes_racing_different_reviews_accept_exactly_one(pending, tmp_path):
    root, service, view = pending
    processes = []
    for i in range(3):
        path = tmp_path / f"form-{i}.json"
        path.write_text(
            json.dumps(submission(view, reason=f"Fictional process {i}.")),
            encoding="utf-8",
            newline="\n",
        )
        processes.append(
            subprocess.Popen(
                worker_command("review", str(root), "none", view.case_id, str(path)),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=worker_env(),
            )
        )
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=300)
        assert process.returncode == 0, stderr[-2000:]
        outputs.append(json.loads(stdout.strip().splitlines()[-1]))
    accepted = [o for o in outputs if o["ok"]]
    assert len(accepted) == 1 and accepted[0]["status"] == "completed"
    assert (
        sorted(o["error"] for o in outputs if not o["ok"]) == ["stale_submission"] * 2
    )
    assert service.get_case(view.case_id).status == "completed"
    assert service.verify_audit(view.case_id).ok


def test_lock_wait_is_bounded_in_process(pending):
    root, _, view = pending
    impatient = ReviewService.open(root, make_config(), lock_timeout=0.2)
    # The holder must be another thread: the same thread re-entering is a
    # ReentrantCallError, not contention.
    held, release = threading.Event(), threading.Event()

    def hold():
        with workspace_lock(root, timeout=1):
            held.set()
            release.wait(30)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(30)
        started = time.monotonic()
        with pytest.raises(c.WorkspaceBusyError) as caught:
            impatient.get_case(view.case_id)
        assert time.monotonic() - started < 3
    finally:
        release.set()
        holder.join(30)
    assert caught.value.retryable
    assert impatient.get_case(view.case_id).status == "awaiting_review"


def test_lock_held_by_another_process_is_released_when_it_exits(pending):
    root, _, view = pending
    holder = subprocess.Popen(
        worker_command("hold-lock", str(root), "none"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=worker_env(),
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        # Built directly: open() itself validates under the (held) lock.
        service = ReviewService(root, make_config(), read_only=True, lock_timeout=0.2)
        with pytest.raises(c.WorkspaceBusyError):
            ReviewService.open(root, make_config(), read_only=True, lock_timeout=0.2)
        with pytest.raises(c.WorkspaceBusyError):
            service.get_case(view.case_id)
    finally:
        holder.kill()
        holder.communicate(timeout=60)
    assert service.get_case(view.case_id).status == "awaiting_review"


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "5", None])
def test_invalid_lock_timeouts_are_rejected_before_io(value):
    with pytest.raises(c.ValidationError):
        validated_timeout(value)


def test_concurrent_start_with_one_request_id_creates_one_case(tmp_path):
    root = tmp_path / "requests"
    service = ReviewService.initialize(
        root, make_config(lock_timeout=60.0), retriever=FakeRetriever()
    )
    request_id = "b3c1f0e2-6a4d-4e8f-9c2b-1d3e5f7a9b0c"

    def start(_):
        return service.start_case(
            "Luma Vesper",
            origin="test",
            actor="fictional-analyst",
            request_id=request_id,
        ).case_id

    with ThreadPoolExecutor(max_workers=6) as pool:
        case_ids = set(pool.map(start, range(6)))
    assert len(case_ids) == 1 and [s.case_id for s in service.list_cases()] == list(
        case_ids
    )
