"""The v2 LangGraph StateGraph: one forward path with a durable human interrupt.

normalize -> retrieve -> format_packet -> prepare_review -> human_review
          -> explain -> audit

The graph is compiled per locked operation with a ``SqliteSaver`` bound to
the workspace database. Retriever, ledger and context providers are injected
runtime dependencies (``nodes.NodeDeps``), never checkpoint values. Checkpoint
state holds only JSON-safe primitives.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, TypedDict

# The pilot never sends traces or fetches models, regardless of shell defaults.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command  # noqa: E402

from watchlist.review.contracts import CaseHeader  # noqa: E402
from watchlist.review.nodes import NodeDeps, make_nodes  # noqa: E402
from watchlist.review.recovery import CheckpointFacts  # noqa: E402

NODE_ORDER = (
    "normalize",
    "retrieve",
    "format_packet",
    "prepare_review",
    "human_review",
    "explain",
    "audit",
)

__all__ = [
    "Command",
    "NODE_ORDER",
    "ReviewGraphState",
    "build_graph",
    "checkpoint_facts",
    "initial_state",
    "open_saver",
    "run_config",
]


class ReviewGraphState(TypedDict):
    case_id: str
    thread_id: str
    normalized_name: str | None
    evidence_sha256: str | None
    packet: dict[str, Any] | None
    packet_sha256: str | None
    review_status: str
    review: dict[str, Any] | None
    explanation: dict[str, Any] | None
    audit_status: str
    final_event_sha256: str | None
    final_event_sequence: int | None


def initial_state(header: CaseHeader) -> ReviewGraphState:
    """The graph input derived only from the saved case header."""
    return ReviewGraphState(
        case_id=header.case_id,
        thread_id=header.thread_id,
        normalized_name=None,
        evidence_sha256=None,
        packet=None,
        packet_sha256=None,
        review_status="not_requested",
        review=None,
        explanation=None,
        audit_status="pending",
        final_event_sha256=None,
        final_event_sequence=None,
    )


def open_saver(db: sqlite3.Connection) -> SqliteSaver:
    """Bind the saver to owned tables; skip setup(), which would enable WAL."""
    saver = SqliteSaver(db)
    saver.is_setup = True
    return saver


def build_graph(saver: SqliteSaver, deps: NodeDeps):
    graph = StateGraph(ReviewGraphState)
    for name, node in make_nodes(deps).items():
        graph.add_node(name, node)
    graph.add_edge(START, NODE_ORDER[0])
    for left, right in zip(NODE_ORDER, NODE_ORDER[1:]):
        graph.add_edge(left, right)
    graph.add_edge(NODE_ORDER[-1], END)
    return graph.compile(checkpointer=saver)


def run_config(thread_id: str, checkpoint_id: str | None = None) -> dict[str, Any]:
    configurable: dict[str, Any] = {"thread_id": thread_id}
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable, "callbacks": []}


def checkpoint_facts(graph, saver: SqliteSaver, thread_id: str) -> CheckpointFacts:
    """Inspect the explicit latest checkpoint, including saved task outputs.

    Asking for the latest checkpoint ID explicitly (as v1 learned) shows a task
    whose outputs were saved before the next checkpoint as a task with a result,
    so recovery consumes it instead of executing the node again.
    """
    latest = saver.get_tuple(run_config(thread_id))
    if latest is None:
        return CheckpointFacts(exists=False)
    checkpoint_id = latest.config["configurable"]["checkpoint_id"]
    snapshot = graph.get_state(run_config(thread_id, checkpoint_id))
    return CheckpointFacts(
        exists=True,
        checkpoint_id=checkpoint_id,
        next=tuple(snapshot.next),
        interrupted=any(task.interrupts for task in snapshot.tasks),
        has_task_results=any(task.result is not None for task in snapshot.tasks),
        values=dict(snapshot.values or {}),
    )
