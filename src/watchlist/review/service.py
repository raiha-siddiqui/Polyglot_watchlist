"""The single v2 review service used by every interface (CLI, API, UI, MCP).

Lifecycle::

    service = ReviewService.initialize(workspace, config)      # new workspace only
    service = ReviewService.open(workspace, config)            # existing only
    reader = ReviewService.open(workspace, config, read_only=True)

    view = service.start_case('Luma Vesper', origin='test', actor='analyst-1', request_id=None)
    view = service.submit_review(view.case_id, ReviewSubmission(...))
    service.get_case(case_id); service.list_cases(limit=None, offset=0, origin=None)
    service.recover(case_id); service.cancel_case(case_id, actor=..., reason=...)
    service.audit(case_id); service.verify_audit(case_id, anchor=None)

The service object holds no open database handle or lock between calls. Every
operation takes the cooperative workspace lock (bounded wait), opens fresh
SQLite connections, works and releases both, so one object may be shared by
threads and many processes may use one workspace. Reads open SQLite read-only
and never change status or create files. Every failure is a ``ReviewError``
subclass with a stable ``code``. Human review is always required: the service
never selects an outcome, and errors never become negative results.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import threading
from typing import Any, Callable, Iterator, Mapping, Sequence
from uuid import uuid4

from watchlist.review import storage
from watchlist.review.contracts import (
    AUDIT_SCHEMA,
    CASE_SCHEMA,
    LIMITATION,
    PACKET_SCHEMA,
    SUBMISSION_SCHEMA,
    AcceptedReview,
    AuditAnchor,
    AuditVerification,
    AuditView,
    Cancellation,
    CaseHeader,
    CaseSummary,
    CaseView,
    ContextProvider,
    ContextProviderError,
    InvalidStateError,
    ReadOnlyError,
    RecoveryRequiredError,
    RequestConflictError,
    Retriever,
    RetrievalError,
    ReviewConfig,
    ReviewError,
    ReviewSubmission,
    StaleSubmissionError,
    StorageIntegrityError,
    SubmissionConflictError,
    WorkflowIdentity,
)
from watchlist.review.locking import validated_timeout, workspace_lock
from watchlist.review.recovery import (
    Assessment,
    CheckpointFacts,
    assess_case,
    plan_recovery,
    require_compatible,
    require_consistent,
)
from watchlist.review.validation import (
    coerce_submission,
    same_submission,
    validate_actor,
    validate_case_id,
    validate_name,
    validate_origin,
    validate_page,
    validate_providers,
    validate_reason,
    validate_request_id,
    validate_submission_for_packet,
)

__all__ = ["ReviewService", "utc_now"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _no_failpoint(stage: str) -> None:
    return None


@dataclass
class _Session:
    """Resources of one locked operation; closed before the lock is released."""

    ledger: storage.Ledger
    saver: Any
    graph: Any


class ReviewService:
    """Versioned review operations over one v2 workspace.

    Use ``initialize`` or ``open``; the constructor performs no validation of
    the workspace. ``context_providers`` is the hook for display-only packet
    context (for example reviewer memory): each provider's JSON output is frozen
    into the packet and its digest, and cannot change candidates or outcomes.
    ``failpoint`` is a test hook called with crash-window names.
    """

    def __init__(
        self,
        workspace: Path,
        config: ReviewConfig,
        *,
        read_only: bool,
        retriever: Retriever | None = None,
        context_providers: Sequence[ContextProvider] = (),
        clock: Callable[[], str] | None = None,
        failpoint: Callable[[str], None] | None = None,
        lock_timeout: float | None = None,
    ):
        if not isinstance(config, ReviewConfig):
            raise TypeError(
                "config must be a ReviewConfig; use ReviewConfig.load(path)"
            )
        self._root = Path(workspace).absolute()
        self._config = config
        self._identity = config.workflow_identity()
        self._read_only = bool(read_only)
        self._retriever = retriever
        self._retriever_lock = threading.Lock()
        self._providers = validate_providers(tuple(context_providers))
        self._clock = clock or utc_now
        self._failpoint = failpoint or _no_failpoint
        self._timeout = validated_timeout(
            config.operations.lock_timeout_seconds
            if lock_timeout is None
            else lock_timeout
        )

    # -- construction ----------------------------------------------------------

    @staticmethod
    def _load_config(config: ReviewConfig | Path | str) -> ReviewConfig:
        return config if isinstance(config, ReviewConfig) else ReviewConfig.load(config)

    @classmethod
    def initialize(
        cls, workspace: Path | str, config: ReviewConfig | Path | str, **options: Any
    ) -> ReviewService:
        """Create a new workspace (missing or empty directory) and open it writable."""
        config = cls._load_config(config)
        clock = options.get("clock") or utc_now
        storage.create_workspace(workspace, created_at=clock())
        return cls.open(workspace, config, **options)

    @classmethod
    def open(
        cls,
        workspace: Path | str,
        config: ReviewConfig | Path | str,
        *,
        read_only: bool = False,
        **options: Any,
    ) -> ReviewService:
        """Open an existing workspace; never creates, migrates or repairs files.

        Validation only reads the database under the lock; it changes no record.
        A writable open uses a read-write connection so SQLite can roll back a
        hot journal left by a process that died inside a transaction; a
        read-only open cannot do that and reports the workspace as unreadable.
        """
        config = cls._load_config(config)
        service = cls(
            storage.check_workspace(workspace), config, read_only=read_only, **options
        )
        with service._locked(write=not read_only) as db:
            storage.verify_identity(db)
        return service

    # -- public properties -------------------------------------------------------

    @property
    def workspace(self) -> Path:
        return self._root

    @property
    def config(self) -> ReviewConfig:
        return self._config

    @property
    def workflow_identity(self) -> WorkflowIdentity:
        return self._identity

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def context_providers(self) -> tuple[ContextProvider, ...]:
        return self._providers

    # -- internal plumbing -------------------------------------------------------

    @contextmanager
    def _locked(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        storage.check_workspace(self._root)
        with workspace_lock(self._root, timeout=self._timeout):
            if not write and storage.sidecars(self._root):
                # Under the lock no writer is active, so a journal is a hot one
                # from a process that died mid-transaction. Read-only SQLite
                # cannot roll it back; never delete it by hand.
                raise RecoveryRequiredError(
                    "an interrupted transaction left a SQLite journal; open the "
                    "workspace writable once so SQLite can roll it back",
                    details={"files": storage.sidecars(self._root)},
                )
            db = storage.connect(self._root, read_only=not write)
            try:
                if write and storage.sidecars(self._root):
                    storage.verify_identity(db)
                    storage.settle_journal(db)
                yield db
            except sqlite3.Error as exc:
                raise storage.sqlite_failure(
                    exc, "cannot open review database"
                ) from None
            finally:
                db.close()

    def _get_retriever(self) -> Retriever:
        if self._read_only:
            raise ReadOnlyError("read-only service cannot run retrieval")
        with self._retriever_lock:
            if self._retriever is None:
                from watchlist.review.nodes import make_retriever

                self._retriever = make_retriever(self._config.retriever)
            return self._retriever

    @contextmanager
    def _session(self, *, write: bool) -> Iterator[_Session]:
        if write and self._read_only:
            raise ReadOnlyError("this service was opened read-only")
        from watchlist.review import workflow
        from watchlist.review.nodes import NodeDeps

        with self._locked(write=write) as db:
            try:
                storage.verify_identity(db)
                checkpoints = storage.connect_checkpoints(
                    self._root, read_only=not write
                )
            except sqlite3.Error as exc:
                raise storage.sqlite_failure(
                    exc, "cannot open review database"
                ) from None
            try:
                ledger = storage.Ledger(db)
                deps = NodeDeps(
                    ledger,
                    self._config.workflow,
                    self._get_retriever,
                    self._providers,
                    self._clock,
                    self._failpoint,
                )
                saver = workflow.open_saver(checkpoints)
                yield _Session(ledger, saver, workflow.build_graph(saver, deps))
            except sqlite3.Error as exc:
                raise storage.sqlite_failure(exc, "review database error") from None
            finally:
                checkpoints.close()

    def _assess(
        self, session: _Session, case_id: str
    ) -> tuple[Assessment, CheckpointFacts]:
        from watchlist.review.workflow import checkpoint_facts

        snapshot = session.ledger.snapshot(case_id)
        try:
            facts = checkpoint_facts(
                session.graph, session.saver, snapshot.case_row["thread_id"]
            )
        except ReviewError:
            raise
        except Exception as exc:
            # Undecodable or foreign checkpoint data is reported, not repaired.
            assessment = assess_case(
                snapshot, CheckpointFacts(exists=True), self._identity
            )
            issues = (
                *assessment.issues,
                f"checkpoint: unreadable ({type(exc).__name__})",
            )
            return Assessment(
                **{
                    **assessment.__dict__,
                    "status": "inconsistent",
                    "next_action": "none",
                    "recovery_required": False,
                    "issues": issues,
                }
            ), CheckpointFacts(exists=True)
        return assess_case(snapshot, facts, self._identity), facts

    @staticmethod
    def _view(assessment: Assessment) -> CaseView:
        if assessment.header is None:
            raise StorageIntegrityError(
                "case header is unreadable",
                details={
                    "case_id": assessment.case_id,
                    "issues": list(assessment.issues),
                },
            )
        return CaseView(
            assessment.case_id,
            assessment.status,
            assessment.next_action,
            assessment.recovery_required,
            assessment.compatible,
            assessment.compatibility_issues,
            assessment.issues,
            assessment.header,
            assessment.packet,
            assessment.packet_sha256,
            assessment.review,
            assessment.explanation,
            assessment.final_receipt,
            assessment.cancellation,
            LIMITATION,
        )

    def _current_view(self, session: _Session, case_id: str) -> CaseView:
        return self._view(self._assess(session, case_id)[0])

    def _run(self, session: _Session, case_id: str, thread_id: str, value: Any) -> None:
        """Invoke the graph with synchronous checkpoint durability."""
        from watchlist.review.workflow import run_config

        try:
            session.graph.invoke(value, run_config(thread_id), durability="sync")
        except ReviewError as exc:
            exc.details.setdefault("case_id", case_id)
            raise
        except Exception as exc:
            raise RecoveryRequiredError(
                f"operation interrupted ({type(exc).__name__}); inspect the case and "
                "run recover",
                details={"case_id": case_id},
            ) from exc

    # -- operations ----------------------------------------------------------------

    def start_case(
        self, name: str, *, origin: str, actor: str, request_id: str | None = None
    ) -> CaseView:
        """Create a case, retrieve candidates and stop at the human-review interrupt.

        Input is validated before any file effect. Retrieval runs inside the
        workspace lock but outside SQLite write transactions. If anything fails
        after the case row commits, the case remains ``preparing`` and the error
        carries ``details['case_id']`` for ``recover``.

        ``request_id`` (a client UUID; API clients should always send one) makes
        retries idempotent: the same request returns, recovering if needed, the
        case it created; another request with that ID is ``RequestConflictError``.
        Without it, a failure after the case exists is never ``retryable``
        (``details['next_action'] == 'recover'``): a blind retry would create
        a second case.
        """
        if self._read_only:
            raise ReadOnlyError("this service was opened read-only")
        policy = self._config.workflow
        name, normalized = validate_name(name, policy)
        origin = validate_origin(origin, policy)
        actor = validate_actor(actor, policy)
        request_id = validate_request_id(request_id)
        retriever = self._get_retriever()
        try:
            descriptor = retriever.descriptor()
        except ReviewError:
            raise
        except Exception as exc:
            raise RetrievalError(
                f"retriever descriptor failed: {type(exc).__name__}"
            ) from exc
        header = CaseHeader(
            CASE_SCHEMA,
            str(uuid4()),
            str(uuid4()),
            request_id,
            self._clock(),
            origin,
            actor,
            name,
            normalized,
            self._identity,
            PACKET_SCHEMA,
            descriptor,
            tuple(provider.name for provider in self._providers),
        )
        from watchlist.review.workflow import initial_state

        with self._session(write=True) as session:
            existing = (
                None
                if request_id is None
                else session.ledger.case_for_request(request_id)
            )
            if existing is not None:
                saved = session.ledger.header(existing)
                if (saved.name, saved.origin, saved.actor) != (name, origin, actor):
                    raise RequestConflictError(
                        "request_id was already used for a different request",
                        details={"request_id": request_id},
                    )
                return self._recover_in(session, existing)
            session.ledger.create_case(
                header, max_cases=self._config.operations.max_cases
            )
            self._failpoint("after_case")
            try:
                self._run(
                    session, header.case_id, header.thread_id, initial_state(header)
                )
            except ReviewError as exc:
                exc.details["case_id"] = header.case_id
                if request_id is None and exc.retryable:
                    exc.retryable = False
                    exc.details["next_action"] = "recover"
                raise
            return self._current_view(session, header.case_id)

    def get_case(self, case_id: str) -> CaseView:
        """Read one case; inconsistent storage is reported in the view, not repaired."""
        case_id = validate_case_id(case_id)
        with self._session(write=False) as session:
            return self._current_view(session, case_id)

    def list_cases(
        self, limit: int | None = None, offset: int = 0, *, origin: str | None = None
    ) -> list[CaseSummary]:
        """Catalogue page in creation order, without names, evidence or notes.

        ``limit=None`` means ``min(50, operations.page_max)``. ``origin`` keeps
        only cases of that origin, so human, demo and test cases can be read
        as separate partitions.
        """
        limit, offset, origin = validate_page(
            limit, offset, self._config.operations, origin
        )
        result = []
        with self._session(write=False) as session:
            for case_id in session.ledger.case_ids(limit, offset, origin):
                row = session.ledger.case_row(case_id)
                assessment, _ = self._assess(session, case_id)
                version = (
                    assessment.header.workflow.version
                    if assessment.header
                    else "unknown"
                )
                result.append(
                    CaseSummary(
                        case_id,
                        row["created_at"],
                        row["origin"],
                        assessment.status,
                        assessment.next_action,
                        assessment.recovery_required,
                        assessment.compatible,
                        version,
                    )
                )
        return result

    def submit_review(
        self, case_id: str, submission: ReviewSubmission | Mapping[str, Any]
    ) -> CaseView:
        """Accept exactly one human review per case, then finish the workflow.

        * Same ``submission_id`` and identical content as the accepted review:
          idempotent replay, returns the current view (no second effect).
        * Same ``submission_id`` with different content, or an ID used by
          another case: ``SubmissionConflictError``.
        * Another accepted review, or a packet digest that differs from the saved
          packet: ``StaleSubmissionError``.
        * Cancelled or not yet awaiting review: ``InvalidStateError`` /
          ``RecoveryRequiredError``.

        If finalization is interrupted after acceptance the case is
        ``finalizing``; call ``recover`` (never submit a second review).
        """
        if self._read_only:
            raise ReadOnlyError("this service was opened read-only")
        case_id = validate_case_id(case_id)
        submission = coerce_submission(submission, self._config.workflow)
        with self._session(write=True) as session:
            assessment, _ = self._assess(session, case_id)
            require_consistent(assessment)
            accepted = assessment.review
            if accepted is not None:
                if accepted.submission.submission_id == submission.submission_id:
                    if same_submission(submission, accepted.submission):
                        return self._view(assessment)
                    raise SubmissionConflictError(
                        "submission_id was already accepted with different content",
                        details={"case_id": case_id},
                    )
                raise StaleSubmissionError(
                    "case already has an accepted review",
                    details={"case_id": case_id, "status": assessment.status},
                )
            other = session.ledger.submission_case(submission.submission_id)
            if other is not None:
                raise SubmissionConflictError(
                    "submission_id is already used by another case",
                    details={"case_id": case_id},
                )
            if assessment.status == "cancelled":
                raise InvalidStateError(
                    "case is cancelled", details={"case_id": case_id}
                )
            require_compatible(assessment)
            if assessment.status != "awaiting_review":
                error = (
                    RecoveryRequiredError
                    if assessment.recovery_required
                    else InvalidStateError
                )
                raise error(
                    "case is not awaiting human review",
                    details={"case_id": case_id, "status": assessment.status},
                )
            validate_submission_for_packet(
                submission, assessment.packet, assessment.packet_sha256
            )
            review = AcceptedReview(
                SUBMISSION_SCHEMA, case_id, self._clock(), submission
            )
            providers = {provider.name: provider for provider in self._providers}
            with ExitStack() as guards:
                for name in assessment.header.context_providers:
                    provider = providers.get(name)
                    if provider is None:
                        raise ContextProviderError(
                            "a context provider required by this packet is unavailable",
                            details={"case_id": case_id, "provider": name},
                        )
                    guard = getattr(provider, "guard_review", None)
                    if guard is not None:
                        try:
                            guards.enter_context(guard(assessment.packet))
                        except StaleSubmissionError:
                            raise
                        except Exception as exc:
                            raise ContextProviderError(
                                "context freshness could not be checked",
                                details={"case_id": case_id, "provider": name},
                            ) from exc
                session.ledger.record_submission(review)
            self._failpoint("after_submission")
            from watchlist.review.workflow import Command

            self._run(
                session,
                case_id,
                assessment.header.thread_id,
                Command(resume=review.to_dict()),
            )
            return self._current_view(session, case_id)

    def recover(self, case_id: str) -> CaseView:
        """Finish interrupted work from durable state; idempotent and side-effect safe.

        Never re-runs retrieval once evidence is saved, never invents a review
        and never issues a second final event. A pending review stays pending.
        """
        if self._read_only:
            raise ReadOnlyError("this service was opened read-only")
        case_id = validate_case_id(case_id)
        with self._session(write=True) as session:
            return self._recover_in(session, case_id)

    def _recover_in(self, session: _Session, case_id: str) -> CaseView:
        """Recovery body shared by ``recover`` and an idempotent ``start_case`` replay."""
        from watchlist.review.workflow import Command, initial_state

        assessment, facts = self._assess(session, case_id)
        action = plan_recovery(assessment, facts)
        if action == "none":
            return self._view(assessment)
        thread_id = assessment.header.thread_id
        if action == "start":
            value: Any = initial_state(assessment.header)
        elif action == "resume_review":
            value = Command(resume=assessment.review.to_dict())
        else:
            value = None
        self._run(session, case_id, thread_id, value)
        return self._current_view(session, case_id)

    def cancel_case(self, case_id: str, *, actor: str, reason: str) -> CaseView:
        """Stop a case before any review is accepted. Allowed for incompatible cases.

        Cancellation is not a review outcome and implies nothing about the name.
        Repeating the identical cancellation is idempotent.
        """
        if self._read_only:
            raise ReadOnlyError("this service was opened read-only")
        case_id = validate_case_id(case_id)
        actor = validate_actor(actor, self._config.workflow)
        reason = validate_reason(reason, self._config.workflow)
        with self._session(write=True) as session:
            assessment, _ = self._assess(session, case_id)
            require_consistent(assessment)
            if assessment.cancellation is not None:
                if (assessment.cancellation.actor, assessment.cancellation.reason) == (
                    actor,
                    reason,
                ):
                    return self._view(assessment)
                raise InvalidStateError(
                    "case is already cancelled", details={"case_id": case_id}
                )
            if assessment.review is not None:
                raise InvalidStateError(
                    "case already has an accepted review",
                    details={"case_id": case_id, "status": assessment.status},
                )
            session.ledger.record_cancellation(
                Cancellation(case_id, actor, reason, self._clock())
            )
            return self._current_view(session, case_id)

    def audit(self, case_id: str) -> AuditView:
        """Return every audit event of a case; use ``verify_audit`` to check them."""
        case_id = validate_case_id(case_id)
        with self._locked(write=False) as db:
            storage.verify_identity(db)
            ledger = storage.Ledger(db)
            ledger.case_row(case_id)
            rows = ledger.events(case_id)
        count, head, _ = storage.verify_chain(rows, case_id)
        return AuditView(
            AUDIT_SCHEMA,
            case_id,
            count,
            head,
            tuple(storage.to_audit_event(row) for row in rows),
        )

    def verify_audit(
        self, case_id: str, anchor: AuditAnchor | Mapping[str, Any] | None = None
    ) -> AuditVerification:
        """Check the hash chain and every record/checkpoint relationship.

        ``ok`` is False with ``issues`` for any detected tampering; this method
        does not raise for integrity failures. An ``anchor`` retained elsewhere
        additionally detects a truncated or replaced chain tail; anchors are
        validated on construction (``count >= 1``). A valid local chain is
        tamper evidence, not authentication.
        """
        case_id = validate_case_id(case_id)
        if anchor is not None and not isinstance(anchor, AuditAnchor):
            anchor = AuditAnchor.from_dict(anchor)
        with self._session(write=False) as session:
            rows = session.ledger.events(case_id)
            assessment, _ = self._assess(session, case_id)
        count, head, _ = storage.verify_chain(rows, case_id)
        issues = list(assessment.issues)
        if anchor is not None:
            if anchor.case_id != case_id:
                issues.append("anchor: belongs to another case")
            elif count < anchor.count:
                issues.append(
                    "anchor: audit chain is shorter than the anchor (truncated tail)"
                )
            elif rows[anchor.count - 1]["event_sha256"] != anchor.head_sha256:
                issues.append("anchor: event hash at the anchored position differs")
        return AuditVerification(
            case_id, not issues, count, head, anchor is not None, tuple(issues)
        )
