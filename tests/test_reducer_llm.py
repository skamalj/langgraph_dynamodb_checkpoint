"""
LLM-backed e2e test: full LangGraph + DynamoDBSaver + MessageReducer path.

Runs a real graph with a real model, exercising pruning through graph.invoke()
against a real DynamoDB table.

Requires:
  - AWS credentials (AWS_PROFILE / access keys) + region
  - OPENAI_API_KEY
"""
import os
import uuid

import pytest
from langgraph.graph import StateGraph, MessagesState, START
from langchain_openai import ChatOpenAI

from langgraph_dynamodb_checkpoint import DynamoDBSaver
from agentstate_reducer import MessageReducer
from agentstate_reducer.models import ReducerConfig

TABLE_NAME = os.environ.get("DDB_TEST_TABLE", "reducer_e2e_test")
MIN_MESSAGES = 4
MAX_MESSAGES = 6

model = ChatOpenAI(model="gpt-4o-mini", temperature=0)


def call_model(state: MessagesState):
    return {"messages": model.invoke(state["messages"])}


def build_graph(checkpointer):
    builder = StateGraph(MessagesState)
    builder.add_node("call_model", call_model)
    builder.add_edge(START, "call_model")
    return builder.compile(checkpointer=checkpointer)


def test_reducer_caps_stored_messages_via_graph():
    reducer = MessageReducer(config=ReducerConfig(
        min_messages=MIN_MESSAGES, max_messages=MAX_MESSAGES, preserve_first=True))
    saver = DynamoDBSaver(table_name=TABLE_NAME, reducer=reducer)
    graph = build_graph(saver)
    config = {"configurable": {"thread_id": f"ddb-llm-cap-{uuid.uuid4()}"}}

    turns = [
        "Hi, my name is Kamal.",
        "I live in Pune.",
        "What is the capital of France?",
        "What is 2 + 2?",
        "Tell me a short joke.",
        "What colour is the sky?",
        "Count to three.",
    ]
    for turn in turns:
        graph.invoke({"messages": [{"role": "user", "content": turn}]}, config)

    stored = saver.get_tuple(config).checkpoint["channel_values"].get("messages", [])
    assert len(stored) <= MIN_MESSAGES + 1, f"got {len(stored)} stored messages"


def test_reducer_preserves_recent_context_via_graph():
    reducer = MessageReducer(config=ReducerConfig(
        min_messages=MIN_MESSAGES, max_messages=MAX_MESSAGES, preserve_first=True))
    saver = DynamoDBSaver(table_name=TABLE_NAME, reducer=reducer)
    graph = build_graph(saver)
    config = {"configurable": {"thread_id": f"ddb-llm-ctx-{uuid.uuid4()}"}}

    graph.invoke({"messages": [{"role": "user", "content": "Hi, my name is Kamal."}]}, config)
    graph.invoke({"messages": [{"role": "user", "content": "What is 2 + 2?"}]}, config)
    graph.invoke({"messages": [{"role": "user", "content": "Tell me a short joke."}]}, config)

    result = graph.invoke(
        {"messages": [{"role": "user", "content": "What did I say my name was?"}]}, config)
    reply = result["messages"][-1].content.lower()
    assert "kamal" in reply, f"expected recall of 'kamal', got: {reply}"
