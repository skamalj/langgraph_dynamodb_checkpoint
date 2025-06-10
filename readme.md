# LangGraph DynamoDB Checkpoint Saver

A DynamoDB-based checkpoint saver implementation for LangGraph that allows storing and managing checkpoints in Amazon DynamoDB.
* Supports both Sync and async methods

## Installation
### If installing this version, then delete the underlying table as well. Existing data is not compatible with version >= 1.5
bash
pip install langgraph_dynamodb_checkpoint


## Usage

### DynamoDBSaver Constructor

- `table_name` (str): Name of the DynamoDB table to use for storing checkpoints
- `max_read_request_units` (int, optional): Maximum read request units for the DynamoDB table. Defaults to 10
- `max_write_request_units` (int, optional): Maximum write request units for the DynamoDB table. Defaults to 10
- `ttl_seconds` (int, optional): TTL value set for all checkpoint items.

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

## Notes

- The saver automatically creates the DynamoDB table if it doesn't exist
- Uses on-demand billing mode for DynamoDB
- Implements all methods required by the LangGraph BaseCheckpointSaver interface