"""
E2E integration test: DynamoDBSaver with built-in MessageReducer.

Exercises the real storage + reducer path against a real DynamoDB table:
build a checkpoint with a message list, put() it (reducer prunes before write),
then get_tuple() it back and assert on what was persisted.

Deliberately LLM-free so it is deterministic and does not depend on any model API.

Requires:
  - AWS credentials configured (env: AWS_PROFILE or access keys) with DynamoDB
    create/read/write permissions, and a region (AWS_REGION / AWS_DEFAULT_REGION).

The saver auto-creates the DynamoDB table (PAY_PER_REQUEST) if it does not exist.
"""
import os
import uuid

import pytest
from langgraph.checkpoint.base import empty_checkpoint

from langgraph_dynamodb_checkpoint import DynamoDBSaver
from agentstate_reducer import MessageReducer
from agentstate_reducer.models import ReducerConfig

TABLE_NAME = os.environ.get("DDB_TEST_TABLE", "reducer_e2e_test")
MIN_MESSAGES = 4
MAX_MESSAGES = 6


def make_config(thread_id):
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


def build_messages(n_pairs):
    """2*n_pairs alternating human/ai message dicts with deterministic content."""
    messages = []
    for i in range(n_pairs):
        messages.append({"role": "human", "content": f"msg {i}"})
        messages.append({"role": "ai", "content": f"reply {i}"})
    return messages


def build_checkpoint(messages):
    cp = empty_checkpoint()
    cp["channel_values"]["messages"] = messages
    return cp


def store_and_read(saver, thread_id, messages):
    config = make_config(thread_id)
    saver.delete(config)  # deterministic: clear any prior state
    checkpoint = build_checkpoint(messages)
    saver.put(config, checkpoint, {}, {})
    tup = saver.get_tuple(config)
    assert tup is not None, "get_tuple returned None after put"
    return tup.checkpoint["channel_values"].get("messages", [])


def test_reducer_caps_stored_messages():
    """With a reducer, the persisted message list is capped (min + 1 for preserve_first)."""
    reducer = MessageReducer(config=ReducerConfig(
        min_messages=MIN_MESSAGES, max_messages=MAX_MESSAGES, preserve_first=True))
    saver = DynamoDBSaver(table_name=TABLE_NAME, reducer=reducer)

    stored = store_and_read(saver, f"ddb-reducer-cap-{uuid.uuid4()}", build_messages(10))  # 20 msgs
    assert len(stored) <= MIN_MESSAGES + 1, (
        f"Expected at most {MIN_MESSAGES + 1} messages stored, got {len(stored)}"
    )


def test_reducer_preserves_recent_content_and_order():
    """Surviving = preserved first + most-recent contiguous tail, in order, after round-trip."""
    reducer = MessageReducer(config=ReducerConfig(
        min_messages=MIN_MESSAGES, max_messages=MAX_MESSAGES, preserve_first=True))
    saver = DynamoDBSaver(table_name=TABLE_NAME, reducer=reducer)

    messages = build_messages(10)  # last is {"role": "ai", "content": "reply 9"}
    stored = store_and_read(saver, f"ddb-reducer-order-{uuid.uuid4()}", messages)

    assert stored[0] == messages[0]                          # preserved first
    assert stored[-1] == {"role": "ai", "content": "reply 9"}  # most recent survives
    tail = stored[1:]
    assert tail == messages[-len(tail):]                     # contiguous recent tail, order preserved


def test_no_reducer_keeps_all_messages():
    """Without a reducer, the full message list is persisted unchanged."""
    saver = DynamoDBSaver(table_name=TABLE_NAME)  # no reducer
    messages = build_messages(10)  # 20 messages

    stored = store_and_read(saver, f"ddb-noreducer-{uuid.uuid4()}", messages)
    assert len(stored) == 20
    assert stored == messages


def test_reducer_below_threshold_no_pruning():
    """When message count is at/under max_messages, nothing is pruned."""
    reducer = MessageReducer(config=ReducerConfig(
        min_messages=MIN_MESSAGES, max_messages=MAX_MESSAGES, preserve_first=True))
    saver = DynamoDBSaver(table_name=TABLE_NAME, reducer=reducer)

    messages = build_messages(2)  # 4 messages, under max=6
    stored = store_and_read(saver, f"ddb-below-{uuid.uuid4()}", messages)
    assert stored == messages
