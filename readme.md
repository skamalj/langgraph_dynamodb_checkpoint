# LangGraph DynamoDB Checkpoint Saver

A DynamoDB-based checkpoint saver implementation for LangGraph that allows storing and managing checkpoints in Amazon DynamoDB.

## Installation
### If installing this version, then delete the underlying table as well. Existing data is not compatible with version >= 1.5
bash
pip install langgraph_dynamodb_checkpoint


## Usage

### DynamoDBSaver Constructor

- `table_name` (str): Name of the DynamoDB table to use for storing checkpoints
- `max_read_request_units` (int, optional): Maximum read request units for the DynamoDB table. Defaults to 10
- `max_write_request_units` (int, optional): Maximum write request units for the DynamoDB table. Defaults to 10
- `ttl` (int, optional): TTL value set for all checkpoint items.

### Basic Initialization

python
from langgraph_dynamodb_checkpoint import DynamoDBSaver

# Initialize the saver with a table name
```
saver = DynamoDBSaver(
    table_name="your-dynamodb-table-name",
    max_read_request_units=10,  # Optional, default is 100
    max_write_request_units=10  # Optional, default is 100
    ttl=86400
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