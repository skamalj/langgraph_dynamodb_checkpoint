// @! create readme for included code include=dynamodbSaver.py

# DynamoDB Checkpoint Saver for LangGraph

This module provides a DynamoDB-based implementation of the checkpoint saver for LangGraph, allowing you to persist and manage checkpoints using Amazon DynamoDB as the storage backend.

## Overview

The `DynamoDBSaver` class implements the `BaseCheckpointSaver` interface and provides functionality to:

## Features

- Automatic table creation with appropriate schema
- Efficient key-value storage using DynamoDB's partition and sort keys

## Usage

# Using context manager
with DynamoDBSaver.from_conn_info(table_name='my_checkpoints') as saver:
    # Use the saver
    pass

# Direct initialization
saver = DynamoDBSaver('my_checkpoints')

