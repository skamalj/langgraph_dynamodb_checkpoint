from contextlib import  contextmanager
from typing import Any, Iterator, List, Optional, Tuple
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import WRITES_IDX_MAP, BaseCheckpointSaver, ChannelVersions, Checkpoint, CheckpointMetadata, CheckpointTuple, PendingWrite, get_checkpoint_id
from langgraph_dynamodb_checkpoint.dynamodbSerializer import DynamoDBSerializer
import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
import time

DYNAMODB_KEY_SEPARATOR = "$"

def _make_dynamodb_checkpoint_key(thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> str:
    return DYNAMODB_KEY_SEPARATOR.join([
        "checkpoint", thread_id, checkpoint_ns, checkpoint_id
    ])


def _make_dynamodb_checkpoint_writes_key(thread_id: str, checkpoint_ns: str, checkpoint_id: str, task_id: str, idx: Optional[int]) -> str:
    if idx is None:
        return DYNAMODB_KEY_SEPARATOR.join([
            "writes", thread_id, checkpoint_ns, checkpoint_id, task_id
        ])

    return DYNAMODB_KEY_SEPARATOR.join([
        "writes", thread_id, checkpoint_ns, checkpoint_id, task_id, str(idx)
    ])


def _parse_dynamodb_checkpoint_key(dynamodb_key: str) -> dict:
    namespace, thread_id, checkpoint_ns, checkpoint_id = dynamodb_key.split(
        DYNAMODB_KEY_SEPARATOR
    )
    if namespace != "checkpoint":
        raise ValueError("Expected checkpoint key to start with 'checkpoint'")

    return {
        "thread_id": thread_id,
        "checkpoint_ns": checkpoint_ns,
        "checkpoint_id": checkpoint_id,
    }


def _parse_dynamodb_checkpoint_writes_key(dynamodb_key: str) -> dict:
    namespace, thread_id, checkpoint_ns, checkpoint_id, task_id, idx = dynamodb_key.split(
        DYNAMODB_KEY_SEPARATOR
    )
    if namespace != "writes":
        raise ValueError("Expected checkpoint key to start with 'writes'")

    return {
        "thread_id": thread_id,
        "checkpoint_ns": checkpoint_ns,
        "checkpoint_id": checkpoint_id,
        "task_id": task_id,
        "idx": idx,
    }


def _filter_keys(keys: List[str], before: Optional[RunnableConfig], limit: Optional[int]) -> list:
    """Filter and sort DynamoDB keys based on optional criteria."""
    if before:
        keys = [
            k
            for k in keys
            if _parse_dynamodb_checkpoint_key(k)["checkpoint_id"]
            < before["configurable"]["checkpoint_id"]
        ]

    keys = sorted(
        keys,
        key=lambda k: _parse_dynamodb_checkpoint_key(k)["checkpoint_id"],
        reverse=True,
    )
    if limit:
        keys = keys[:limit]
    return keys


def _load_writes(serde: DynamoDBSerializer, task_id_to_data: dict[tuple[str, str], dict]) -> list[PendingWrite]:
    """Deserialize pending writes."""
    writes = [
        (
            task_id,
            data["channel"],
            serde.loads_typed((data["type"], data["value"])),
        )
        for (task_id, _), data in task_id_to_data.items() if data["type"] and data["value"]
    ]
    return writes


def _parse_dynamodb_checkpoint_data(serde: DynamoDBSerializer, key: str, data: dict, pending_writes: Optional[List[PendingWrite]] = None) -> Optional[CheckpointTuple]:
    """Parse checkpoint data retrieved from DynamoDB."""
    if not data:
        return None

    parsed_key = _parse_dynamodb_checkpoint_key(key)
    thread_id = parsed_key["thread_id"]
    checkpoint_ns = parsed_key["checkpoint_ns"]
    checkpoint_id = parsed_key["checkpoint_id"]
    config = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": checkpoint_id,
        }
    }

    checkpoint = serde.loads_typed((data["type"], data["checkpoint"]))
    metadata = serde.loads(data["metadata"])
    parent_checkpoint_id = data.get("parent_checkpoint_id", "")
    parent_config = (
        {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": parent_checkpoint_id,
            }
        }
        if parent_checkpoint_id
        else None
    )
    return CheckpointTuple(
        config=config,
        checkpoint=checkpoint,
        metadata=metadata,
        parent_config=parent_config,
        pending_writes=pending_writes,
    )


class DynamoDBSaver(BaseCheckpointSaver):
    """DynamoDB-based checkpoint saver implementation."""

    table: Any

    def __init__(self, table_name: str,  max_read_request_units: int = 100, max_write_request_units: int = 100, ttl_seconds: int = None) -> None:
        super().__init__()
        self.dynamodb = boto3.resource('dynamodb')
        self.dynamodb_serde = DynamoDBSerializer(self.serde)
        self.ttl_seconds = ttl_seconds  # Time to live in seconds (default: 24 hours)
        self.table = self._get_or_create_table(table_name, max_read_request_units,max_write_request_units)

    def _get_or_create_table(self, table_name: str, max_read_request_units: int, max_write_request_units: int):
        try:
            # Attempt to load the table
            table = self.dynamodb.Table(table_name)
            table.load()  # This will raise an exception if the table does not exist
            print(f"Table '{table_name}' already exists.")
            return table
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                # Table does not exist, create it
                print(f"Table '{table_name}' not found. Creating table...")
                key_schema = [
                    {'AttributeName': 'PK', 'KeyType': 'HASH'},  # Partition key
                    {'AttributeName': 'SK', 'KeyType': 'RANGE'},  # Sort key
                ]
                attribute_definitions = [
                    {'AttributeName': 'PK', 'AttributeType': 'S'},  # String type
                    {"AttributeName": "SK", "AttributeType": "S"},
                ]
                
                table = self.dynamodb.create_table(
                    TableName=table_name,
                    KeySchema=key_schema,
                    AttributeDefinitions=attribute_definitions,
                    BillingMode='PAY_PER_REQUEST',
                    OnDemandThroughput={
                        'MaxReadRequestUnits': max_read_request_units,
                        'MaxWriteRequestUnits': max_write_request_units
                    }
                )
                table.wait_until_exists()  # Wait for the table to become active

                if self.ttl_seconds:
                    self.dynamodb.meta.client.update_time_to_live(
                        TableName=table_name,
                        TimeToLiveSpecification={
                            'Enabled': True,
                            'AttributeName': 'ttl'  # This should be a Number (epoch time in seconds)
                        }
                    )

                print(f"Table '{table_name}' created successfully.")
                return table
            else:
                raise  # Re-raise any other exceptions

    @classmethod
    @contextmanager
    def from_conn_info(cls, *, table_name: str, max_read_request_units: int = 100, max_write_request_units: int = 100, ttl_seconds: int = None) -> Iterator["DynamoDBSaver"]:
        saver = None
        try:
            saver = DynamoDBSaver(table_name,max_read_request_units,max_write_request_units, ttl_seconds)
            yield saver
        finally:
            pass

    def put(self, config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata, new_versions: ChannelVersions) -> RunnableConfig:
        """Save a checkpoint to DynamoDB.

        Args:
            config (RunnableConfig): The config to associate with the checkpoint.
            checkpoint (Checkpoint): The checkpoint to save.
            metadata (CheckpointMetadata): Additional metadata to save with the checkpoint.
            new_versions (ChannelVersions): New channel versions as of this write.

        Returns:
            RunnableConfig: Updated configuration after storing the checkpoint.
        """
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"]["checkpoint_ns"]
        checkpoint_id = checkpoint["id"]
        parent_checkpoint_id = config["configurable"].get("checkpoint_id")
        key = _make_dynamodb_checkpoint_key(thread_id, checkpoint_ns, checkpoint_id)

        type_, serialized_checkpoint = self.dynamodb_serde.dumps_typed(checkpoint)
        serialized_metadata = self.dynamodb_serde.dumps(metadata)

        data = {
            "PK": thread_id,
            "SK": checkpoint_id,
            "checkpoint_key": key,
            "checkpoint": serialized_checkpoint,
            "type": type_,
            "metadata": serialized_metadata,
            "parent_checkpoint_id": parent_checkpoint_id
            if parent_checkpoint_id
            else "",
        }

        if self.ttl_seconds:
            data["ttl"] = int(time.time()) + self.ttl_seconds

        self.table.put_item(Item=data)
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    def put_writes(self, config: RunnableConfig, writes: List[Tuple[str, Any]], task_id: str) -> None:
        """Store intermediate writes linked to a checkpoint.

        Args:
            config (RunnableConfig): Configuration of the related checkpoint.
            writes (Sequence[Tuple[str, Any]]): List of writes to store, each as (channel, value) pair.
            task_id (str): Identifier for the task creating the writes.
        """
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"]["checkpoint_ns"]
        checkpoint_id = config["configurable"]["checkpoint_id"]

        for idx, (channel, value) in enumerate(writes):
            key = _make_dynamodb_checkpoint_writes_key(
                thread_id,
                checkpoint_ns,
                checkpoint_id,
                task_id,
                WRITES_IDX_MAP.get(channel, idx),
            )
            type_, serialized_value = self.dynamodb_serde.dumps_typed(value)
            SK = DYNAMODB_KEY_SEPARATOR.join([
                checkpoint_id, task_id
            ])
            data = {"PK": thread_id,"SK": SK, "checkpoint_key": key, "channel": channel, "type": type_, "value": serialized_value}
            
            if self.ttl_seconds:
                data["ttl"] = int(time.time()) + self.ttl_seconds

            self.table.put_item(Item=data)

# @! create delete function for dynamodb similar to put item . Function accept  config only as threadid

    def delete(self, config: RunnableConfig) -> None:
        """
        Delete all checkpoints from DynamoDB for the given thread ID, handling pagination.

        Args:
            config (RunnableConfig): The config containing the thread ID for the checkpoint to delete.
        """
        thread_id = config["configurable"]["thread_id"]
        print(f"Deleting items for thread_id: {thread_id}")

        last_evaluated_key = None
        total_deleted = 0

        while True:
            if last_evaluated_key:
                response = self.table.query(
                    KeyConditionExpression=Key('PK').eq(thread_id),
                    ExclusiveStartKey=last_evaluated_key
                )
            else:
                response = self.table.query(
                    KeyConditionExpression=Key('PK').eq(thread_id)
                )

            items = response.get("Items", [])
            print(f"Fetched {len(items)} items to delete")

            if not items:
                break
            
            with self.table.batch_writer() as batch:
                for item in items:
                    batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
                    total_deleted += 1

            last_evaluated_key = response.get("LastEvaluatedKey")
            if not last_evaluated_key:
                break

        print(f"Total items deleted: {total_deleted}")




    def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        """Get a checkpoint tuple from DynamoDB.

        This method retrieves a checkpoint tuple from DynamoDB based on the
        provided config. If the config contains a "checkpoint_id" key, the checkpoint with
        the matching thread ID and checkpoint ID is retrieved. Otherwise, the latest checkpoint
        for the given thread ID is retrieved.

        Args:
            config (RunnableConfig): The config to use for retrieving the checkpoint.

        Returns:
            Optional[CheckpointTuple]: The retrieved checkpoint tuple, or None if no matching checkpoint was found.
        """
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = get_checkpoint_id(config)
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")

        checkpoint_key = self._get_checkpoint_key(
            self.table, thread_id, checkpoint_ns, checkpoint_id
        )
        if not checkpoint_key:
            return None
        
        checkpoint_id = _parse_dynamodb_checkpoint_key(checkpoint_key)["checkpoint_id"]

        response = self.table.get_item(Key={"PK": thread_id, "SK": checkpoint_id})
        checkpoint_data = response.get('Item', {})

        # load pending writes
        checkpoint_id = (
            checkpoint_id
            or _parse_dynamodb_checkpoint_key(checkpoint_key)["checkpoint_id"]
        )
        pending_writes = self._load_pending_writes(
            thread_id, checkpoint_ns, checkpoint_id
        )
        return _parse_dynamodb_checkpoint_data(
            self.dynamodb_serde, checkpoint_key, checkpoint_data, pending_writes=pending_writes
        )

    def list(self, config: Optional[RunnableConfig], *, filter: Optional[dict[str, Any]] = None, before: Optional[RunnableConfig] = None, limit: Optional[int] = None) -> Iterator[CheckpointTuple]:
        """List checkpoints from the database.

        This method retrieves a list of checkpoint tuples from DynamoDB based
        on the provided config. The checkpoints are ordered by checkpoint ID in descending order (newest first).

        Args:
            config (RunnableConfig): The config to use for listing the checkpoints.
            filter (Optional[Dict[str, Any]]): Additional filtering criteria for metadata. Defaults to None.
            before (Optional[RunnableConfig]): If provided, only checkpoints before the specified checkpoint ID are returned. Defaults to None.
            limit (Optional[int]): The maximum number of checkpoints to return. Defaults to None.

        Yields:
            Iterator[CheckpointTuple]: An iterator of checkpoint tuples.
        """
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        pattern = _make_dynamodb_checkpoint_key(thread_id, checkpoint_ns, "*")

        checkpoint_key = DYNAMODB_KEY_SEPARATOR.join([
            "checkpoint", thread_id, checkpoint_ns
            ])
        
        items = self.table.query(
            KeyConditionExpression=Key('PK').eq(thread_id),
            FilterExpression=Key('checkpoint_key').begins_with(checkpoint_key),
            ScanIndexForward=False,
            Limit=limit if limit else 0)["Items"]
        
        for data in items:
            if data and "checkpoint" in data and "metadata" in data:
                # load pending writes
                key = data["checkpoint_key"]
                checkpoint_id = _parse_dynamodb_checkpoint_key(key)[
                    "checkpoint_id"
                ]
                pending_writes = self._load_pending_writes(
                    thread_id, checkpoint_ns, checkpoint_id
                )
                yield _parse_dynamodb_checkpoint_data(
                    self.dynamodb_serde, key, data, pending_writes=pending_writes
                )

    def _load_pending_writes(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> List[PendingWrite]:
        
        writes_key = DYNAMODB_KEY_SEPARATOR.join([
            "writes", thread_id, checkpoint_ns, checkpoint_id
        ])


        matching_keys = self.table.query(
            KeyConditionExpression=Key('PK').eq(thread_id),
            FilterExpression=Key('checkpoint_key').begins_with(writes_key),
            ScanIndexForward=False
            )["Items"]
        
        parsed_keys = [
            _parse_dynamodb_checkpoint_writes_key(key["checkpoint_key"]) for key in matching_keys
        ]
        pending_writes = _load_writes(
            self.dynamodb_serde,
            {
                (parsed_key["task_id"], parsed_key["idx"]): self.table.get_item(Key={"PK": key["PK"], "SK": key["SK"]})['Item']
                for key, parsed_key in sorted(
                    zip(matching_keys, parsed_keys), key=lambda x: x[1]["idx"]
                )
            },
        )
        return pending_writes

    def _get_checkpoint_key(self, table, thread_id: str, checkpoint_ns: str, checkpoint_id: Optional[str]) -> Optional[str]:
        """Determine the DynamoDB key for a checkpoint."""
        if checkpoint_id:
            return _make_dynamodb_checkpoint_key(thread_id, checkpoint_ns, checkpoint_id)
        
        checkpoint_key = DYNAMODB_KEY_SEPARATOR.join([
        "checkpoint", thread_id, checkpoint_ns
        ])

        all_keys = table.query(
            KeyConditionExpression=Key('PK').eq(thread_id),
            FilterExpression=Key('checkpoint_key').begins_with(checkpoint_key),
            ScanIndexForward=False,
            Limit=1
            )["Items"]
        
        if not all_keys:
            return None
        latest_key = max(
            all_keys,
            key=lambda k: _parse_dynamodb_checkpoint_key(k["checkpoint_key"])["checkpoint_id"],
        )
        return latest_key["checkpoint_key"]