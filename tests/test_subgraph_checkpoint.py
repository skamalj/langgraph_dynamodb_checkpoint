"""Tests for DynamoDB checkpoint namespace isolation with subgraphs.

Demonstrates a bug where root and subgraph checkpoints collide because:
1. The DynamoDB Sort Key (SK) does not include checkpoint_ns
2. The checkpoint_key prefix filter is too loose, matching across namespaces

These tests use a minimal parent → subgraph graph where the subgraph calls
interrupt() to pause execution.  No LLM is required — all nodes are pure
Python functions.

Requirements:
    - A local DynamoDB instance (e.g. localstack, DynamoDB Local)
    - AWS_ENDPOINT_URL environment variable pointing to it
    - boto3 credentials configured (can be dummy for local)
"""

import uuid
import pytest

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from langgraph_dynamodb_checkpoint import DynamoDBSaver

from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages


# ---------------------------------------------------------------------------
# State schemas
# ---------------------------------------------------------------------------

class ChildState(TypedDict):
    question: str
    answer: str


class ParentState(TypedDict):
    status: str
    result: str


# ---------------------------------------------------------------------------
# Subgraph: asks a question via interrupt(), returns the answer
# ---------------------------------------------------------------------------

def ask_human(state: ChildState) -> ChildState:
    """Node that interrupts to ask the human a question."""
    question = "What is the answer?"
    answer = interrupt(question)
    return {"question": question, "answer": answer}


def build_child_graph():
    builder = StateGraph(ChildState)
    builder.add_node("ask_human", ask_human)
    builder.add_edge(START, "ask_human")
    builder.add_edge("ask_human", END)
    return builder.compile(checkpointer=True)  # inherit parent's checkpointer


# ---------------------------------------------------------------------------
# Parent graph: routes to the subgraph via simple edges
# ---------------------------------------------------------------------------

def before_child(state: ParentState) -> ParentState:
    """Runs before the child subgraph."""
    return {"status": "asking"}


def after_child(state: ParentState) -> ParentState:
    """Runs after the child subgraph completes."""
    return {"status": "done"}


def build_parent_graph(checkpointer):
    child = build_child_graph()

    builder = StateGraph(ParentState)
    builder.add_node("before_child", before_child)
    builder.add_node("child", child)
    builder.add_node("after_child", after_child)
    builder.add_edge(START, "before_child")
    builder.add_edge("before_child", "child")
    builder.add_edge("child", "after_child")
    builder.add_edge("after_child", END)
    return builder.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def table_name():
    """Unique table name per test run to avoid collisions."""
    return f"test_subgraph_ckpt_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def checkpointer(table_name):
    """Create a DynamoDBSaver with a fresh table."""
    saver = DynamoDBSaver(table_name=table_name)
    yield saver
    # Cleanup: delete all items
    try:
        saver.delete({"configurable": {"thread_id": "test"}})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSubgraphCheckpointNamespace:
    """Verify that root and subgraph checkpoints are stored in separate
    namespaces and do not collide."""

    def test_root_checkpoint_has_correct_namespace(self, checkpointer):
        """After a subgraph interrupt, get_tuple() for the root namespace
        must return a checkpoint with checkpoint_ns == '' (empty string),
        NOT the subgraph's namespace.

        BUG: Without the fix, get_tuple() returns the subgraph checkpoint
        because the SK does not include checkpoint_ns and the prefix filter
        matches both root and subgraph checkpoint_keys.
        """
        graph = build_parent_graph(checkpointer)
        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}

        # Run until interrupt — the child subgraph will call interrupt()
        chunks = []
        for chunk in graph.stream(
            {"status": "start", "result": ""},
            config,
            stream_mode="updates",
            subgraphs=True,
        ):
            chunks.append(chunk)

        # Should have produced chunks (graph ran)
        assert len(chunks) > 0, "Graph produced no chunks"

        # Get the root checkpoint
        root_tuple = checkpointer.get_tuple(
            {"configurable": {"thread_id": thread_id}}
        )

        assert root_tuple is not None, "Root checkpoint not found"
        root_ns = root_tuple.config["configurable"]["checkpoint_ns"]

        # THE KEY ASSERTION: root checkpoint must have empty namespace
        assert root_ns == "", (
            f"Root checkpoint has wrong namespace: '{root_ns}'. "
            f"Expected '' (empty string). This indicates the root and "
            f"subgraph checkpoints are colliding in DynamoDB."
        )

    def test_subgraph_interrupt_resume_cycle(self, checkpointer):
        """Full interrupt → resume cycle: the subgraph's interrupt() must
        receive the resume value after Command(resume=...).

        BUG: Without the fix, the root checkpoint is lost (or returns the
        subgraph's checkpoint), so Command(resume=...) starts a fresh
        execution instead of resuming. The answer never reaches the
        subgraph's interrupt() call.
        """
        graph = build_parent_graph(checkpointer)
        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}

        # --- Run 1: execute until interrupt ---
        for _ in graph.stream(
            {"status": "start", "result": ""},
            config,
            stream_mode="updates",
            subgraphs=True,
        ):
            pass

        # Verify root state shows a pending interrupted task
        root_state = graph.get_state(config)
        assert len(root_state.tasks) > 0, (
            f"Root state has no tasks after interrupt. "
            f"tasks={root_state.tasks}, next={root_state.next}. "
            f"The root checkpoint likely returned the subgraph's "
            f"checkpoint due to namespace collision."
        )

        # --- Run 2: resume with an answer ---
        resume_chunks = []
        for chunk in graph.stream(
            Command(resume="42"),
            config,
            stream_mode="updates",
            subgraphs=True,
        ):
            resume_chunks.append(chunk)

        assert len(resume_chunks) > 0, "Resume produced no output chunks"

        # Verify the graph completed
        final_state = graph.get_state(config)
        assert len(final_state.next) == 0, (
            f"Graph did not complete after resume. next={final_state.next}"
        )
