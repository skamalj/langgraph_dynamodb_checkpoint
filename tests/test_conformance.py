"""LangGraph's official checkpointer conformance suite (langgraph-checkpoint-conformance) against DynamoDBSaver.

Requires AWS credentials; uses DDB_TEST_TABLE (default reducer_e2e_test).
"""
import os

import pytest
from langgraph.checkpoint.conformance import checkpointer_test
from langgraph.checkpoint.conformance.capabilities import EXTENDED_CAPABILITIES
from langgraph.checkpoint.conformance.report import ProgressCallbacks
from langgraph.checkpoint.conformance.validate import validate

from langgraph_dynamodb_checkpoint import DynamoDBSaver

TABLE = os.environ.get("DDB_TEST_TABLE", "reducer_e2e_test")


@checkpointer_test(name="DynamoDBSaver")
async def _saver():
    yield DynamoDBSaver(TABLE)


@pytest.mark.asyncio
async def test_official_conformance_all_capabilities():
    report = await validate(_saver, progress=ProgressCallbacks.quiet())
    report.print_report()
    failures = {cap: r.failures for cap, r in report.results.items() if r.failures}
    assert report.passed_all_base(), failures
    # Every extended capability must be implemented (detected) and green.
    for cap in EXTENDED_CAPABILITIES:
        result = report.results.get(cap.value)
        assert result is not None and result.detected, f"{cap.value} not implemented"
        assert result.passed is True, {cap.value: result.failures}
    assert report.passed_all(), failures
    assert report.conformance_level() == "FULL"
