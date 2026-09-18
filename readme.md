# LangGraph DynamoDB Checkpoint Saver

A DynamoDB-based checkpoint saver implementation for LangGraph that allows storing and managing checkpoints in Amazon DynamoDB.

**What makes this checkpointer different:** it has message history pruning built in. Pass a `MessageReducer` and the checkpointer automatically caps your message list before writing to DynamoDB — no extra code in your graph, no state annotation changes required.

* Supports both Sync and async methods
* Single table Implementation
* **Built-in message pruning** via [agentstate-reducer](https://pypi.org/project/agentstate-reducer/) (optional)
* Supports delete basis given thread_id
* Supports TTL-based expiry
* Supports logging - Multiple Log levels
* Supports  Class and Context Manager initialization

## Installation

```bash
pip install langgraph_dynamodb_checkpoint
```

With optional message pruning support:

```bash
pip install "langgraph_dynamodb_checkpoint[reducer]"
```

**Requires Python 3.10+**


## Usage

### DynamoDBSaver Constructor

- `table_name` (str): Name of the DynamoDB table to use for storing checkpoints
- `max_read_request_units` (int, optional): Maximum read request units for the DynamoDB table. Defaults to 100
- `max_write_request_units` (int, optional): Maximum write request units for the DynamoDB table. Defaults to 100
- `ttl_seconds` (int, optional): TTL value set for all checkpoint items.
- `reducer` (MessageReducer, optional): Prunes the message history before each checkpoint is stored. See [Built-in Message Pruning](#built-in-message-pruning).
- `messages_key` (str, optional): State channel that holds the message list. Defaults to `"messages"`.

### Import

```
from langgraph_dynamodb_checkpoint import DynamoDBSaver
```


### 🔍 Enable Logging

`langgraph_dynamodb` uses Python's standard `logging` module and emits logs under the logger name `langgraph_dynamodb`.

You can control logging verbosity using the `LANGGRAPH_DYNAMODB_LOG_LEVEL` environment variable:

```bash
export LANGGRAPH_DYNAMODB_LOG_LEVEL=DEBUG
```

#### Available log levels:

* `CRITICAL`
* `ERROR`
* `WARNING`
* `INFO` (default)
* `DEBUG`
* `NOTSET`

#### Example:

```bash
LANGGRAPH_DYNAMODB_LOG_LEVEL=DEBUG 
```

---

### ⚙️ Programmatic Logging Configuration (Optional)

You can also configure logging directly in your code using `configure_logging`:

```python
from langgraph_dynamodb_checkpoint import configure_logging
import logging

configure_logging(
    level=logging.DEBUG,
    log_format="%(levelname)s: %(message)s"
)
```

Redirect logs to a file:

```python
with open("log.txt", "a") as logfile:
    configure_logging(level=logging.INFO, stream=logfile)
```

This gives you full control over the log destination, level, and format.

# Initialize the saver with a table name
```
saver = DynamoDBSaver(
    table_name="your-dynamodb-table-name",
    max_read_request_units=10,  # Optional, default is 100
    max_write_request_units=10  # Optional, default is 100
    ttl_seconds=86400
)
```
Table has ttl enabled with attribute name set to `ttl`

### Alternative Initialization Using Context Manager

```
from langgraph_dynamodb_checkpoint import DynamoDBSaver

with DynamoDBSaver.from_conn_info(table_name="your-dynamodb-table-name") as saver:
    # Use the saver here
    pass
```

### Supports Delete
Checkpointer supports delete basis given thread_id
```
config = {"configurable": {"thread_id": "900"}}
memory.delete(config)
```

## Table Structure

The saver automatically creates a DynamoDB table if it doesn't exist, with the following structure:

- Partition Key (PK): String type, used for thread_id
- Sort Key (SK): String type, used for checkpoint_id

## AWS Configuration

Ensure you have proper AWS credentials configured either through:
- Environment variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
- AWS credentials file (~/.aws/credentials)
- IAM role when running on AWS services

The AWS credentials should have permissions to:
- Create DynamoDB tables (if table doesn't exist)
- Read and write to DynamoDB tables

## Built-in Message Pruning

Long-running agents accumulate message history with every turn. Left unchecked this inflates checkpoint size, increases DynamoDB storage/throughput cost, and eventually blows past LLM context limits.

This checkpointer solves that at the persistence layer: pass a `MessageReducer` and it automatically prunes the message list inside `put()` before the checkpoint is serialised and written to DynamoDB. **Your graph code, state definition, and node logic stay untouched.**

This is an alternative to — or complement of — the LangGraph `Annotated[list, reducer_fn]` pattern. Use the checkpoint-layer approach when:

- You don't own the graph or state definition (e.g. using a pre-built LangGraph agent)
- You want pruning to happen unconditionally at every save
- You want to keep all in-memory state intact and only prune what gets persisted

### Install with reducer support

```bash
pip install "langgraph_dynamodb_checkpoint[reducer]"
```

### Usage — message-count pruning

```python
from agentstate_reducer import MessageReducer
from langgraph_dynamodb_checkpoint import DynamoDBSaver

reducer = MessageReducer(min_messages=10, max_messages=20)

saver = DynamoDBSaver(
    table_name="your-table",
    reducer=reducer,          # prune before each checkpoint save
    messages_key="messages",  # state channel holding the message list (default)
)
```

### Usage — token-budget pruning

```python
from agentstate_reducer import MessageReducer, ReducerConfig
from langgraph_dynamodb_checkpoint import DynamoDBSaver

# Prune when the conversation exceeds 4000 tokens, down to ~2000 — whole messages only, never truncated
reducer = MessageReducer(config=ReducerConfig(max_tokens=4000, target_tokens=2000))
saver = DynamoDBSaver(table_name="your-table", reducer=reducer)
```

When pruning triggers, the oldest `human`/`ai` messages are removed until the target is met. The following are **never** pruned:

- Index 0 (typically the system prompt) — controlled by `preserve_first=True`
- `system` and `function` messages
- `tool` messages — unless their parent `ai` message is pruned (cascade behaviour, configurable)

See [agentstate-reducer on PyPI](https://pypi.org/project/agentstate-reducer/) for full configuration: message-count vs token-budget modes, `preserve_first`, `cascade_tool_messages`, `summarize_fn`, and role alias support (`user`/`assistant`/`agent`).

### Usage — long-term memory via `on_prune` (agentstate-reducer >= 0.4.0)

Messages pruned from the checkpoint are exactly the ones leaving the model's view. The saver forwards a **memory namespace** to the reducer, and any `on_prune` hook receives `(pruned_messages, namespace)` — so pruned turns can flow straight into a LangGraph `BaseStore` (or LangMem, or any memory engine) with no extra node and no package coupling:

```python
from uuid import uuid4
from agentstate_reducer import MessageReducer, ReducerConfig, Background
from langgraph_dynamodb_checkpoint import DynamoDBSaver

store = ...  # any langgraph BaseStore

def remember(pruned, namespace):
    for m in pruned:
        store.put(tuple(namespace), key=str(uuid4()), value={"role": m.type, "content": m.content})

reducer = MessageReducer(config=ReducerConfig(max_messages=20, on_prune=[remember]))
# on_prune=[Background(remember)] runs a slow hook (e.g. LLM extraction) off the request path
saver = DynamoDBSaver(table_name="your-table", reducer=reducer)
graph = builder.compile(checkpointer=saver, store=store)

graph.invoke(input, config={"configurable": {
    "thread_id": uuid4().hex,                    # short-term scope (this checkpoint)
    "memory_namespace": ("memories", user_id),   # long-term scope (the store)
}})
```

The saver reads `memory_namespace` (or whatever `ReducerConfig.namespace_key` names) from `config["configurable"]` on every `put()` and passes it through untouched. If the app never sets it, the namespace falls back to `("memories", thread_id)`. Each pruned message reaches the hooks once, even though LangGraph writes several checkpoints per turn.

## Notes

- The saver automatically creates the DynamoDB table if it doesn't exist
- Uses on-demand billing mode for DynamoDB
- Implements all methods required by the LangGraph BaseCheckpointSaver interface