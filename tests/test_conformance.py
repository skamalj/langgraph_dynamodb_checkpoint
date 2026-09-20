"""LangGraph's official checkpointer conformance suite (langgraph-checkpoint-conformance) against DynamoDBSaver.

Requires AWS credentials; uses DDB_TEST_TABLE (default reducer_e2e_test).
"""
import os

import pytest
from langgraph.checkpoint.conformance import checkpointer_test
from langgraph.checkpoint.conformance.report import ProgressCallbacks
from langgraph.checkpoint.conformance.validate import validate

from langgraph_dynamodb_checkpoint import DynamoDBSaver

TABLE = os.environ.get("DDB_TEST_TABLE", "reducer_e2e_test")


@checkpointer_test(name="DynamoDBSaver")
async def _saver():
    yield DynamoDBSaver(TABLE)


@pytest.mark.asyncio
async def test_official_conformance_base_capabilities():
    report = await validate(_saver, progress=ProgressCallbacks.quiet())
    failures = {cap: r.failures for cap, r in report.results.items() if r.failures}
    assert report.passed_all_base(), failures
    assert report.conformance_level() == "FULL"
