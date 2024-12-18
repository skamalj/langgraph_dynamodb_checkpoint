# LangGraph DynamoDB Checkpoint Saver

## Overview

The `langgraph_dynamodb_checkpoint` module provides an implementation for saving checkpoints in DynamoDB for LangGraph applications. 

## Installation

To install the module, ensure you have Python 3.9 or higher and use the following command:

`pip install langgraph-dynamodb-checkpoint
`

## Usage

Below is a basic example of how to use the DynamoDB Checkpoint Saver in your LangGraph application.

`
from langgraph_dynamodb_checkpoint import DynamoDBSaver
`
# Initialize the DynamoDB Saver

### Module creates this table if it does not already exists.
`
saver = DynamoDBSaver(table_name='your_table_name', max_read_request_unit=10, max_write_request_unit=10)
`