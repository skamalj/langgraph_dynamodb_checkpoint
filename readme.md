// @! create readme for usage , do not exxplain code, include=src/langgraph_dynamodb_saver/dynamodbsaver.py

# LangGraph DynamoDB Saver

A DynamoDB-based checkpoint saver implementation for LangGraph applications.

## Installation

bash
pip install langgraph-dynamodb-saver


## Prerequisites

- AWS credentials configured (either through environment variables, AWS CLI, or IAM roles)
- Appropriate permissions to create and access DynamoDB tables

## Usage

python
from langgraph_dynamodb_saver import DynamoDBSaver

# Create a new saver instance with a table name
saver = DynamoDBSaver(table_name="my-checkpoints")

# Using context manager
with DynamoDBSaver.from_conn_info(table_name="my-checkpoints") as saver:
    # Use the saver in your LangGraph application
    pass


## Configuration

The DynamoDB saver automatically creates a table if it doesn't exist with the following structure:

- Partition Key (PK): String
- Sort Key (SK): String
- On-demand billing mode
- Default throughput limits:
  - Max Read Request Units: 100
  - Max Write Request Units: 100

## Environment Variables

Ensure your AWS credentials are properly configured using one of these methods:

bash
# Option 1: Environment variables
export AWS_ACCESS_KEY_ID="your_access_key"
export AWS_SECRET_ACCESS_KEY="your_secret_key"
export AWS_REGION="your_region"

## Error Handling

The saver will:
- Automatically create the DynamoDB table if it doesn't exist
- Handle connection and operation retries
- Raise appropriate exceptions for permission issues or AWS service problems

## Limitations

- DynamoDB item size limit of 400KB applies to individual checkpoints
- Queries are eventually consistent by default
- Table names must be unique within an AWS region

