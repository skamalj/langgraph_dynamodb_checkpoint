import copy
from contextlib import  contextmanager
from typing import Any, Iterator, List, Optional, Tuple, AsyncIterator, Dict
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import WRITES_IDX_MAP, BaseCheckpointSaver, ChannelVersions, Checkpoint, CheckpointMetadata, CheckpointTuple, PendingWrite, get_checkpoint_id
from langgraph_dynamodb_checkpoint.dynamodbSerializer import DynamoDBSerializer
from langgraph_dynamodb_checkpoint._nudge import nudge_unbounded_history
import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError
import time
import asyncio
import logging
logger = logging.getLogger("langgraph_dynamodb")

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
    metadata = serde.loads_typed(data["metadata"])
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

    def __init__(self, table_name: str,  max_read_request_units: int = 100, max_write_request_units: int = 100, ttl_seconds: int = None, reducer=None, messages_key: str = "messages") -> None:
        super().__init__()
        self.dynamodb = boto3.resource('dynamodb')
        self.dynamodb_serde = DynamoDBSerializer(self.serde)
        self.ttl_seconds = ttl_seconds  # Time to live in seconds (default: 24 hours)
        self.reducer = reducer
        self.messages_key = messages_key
        if reducer is None:
            nudge_unbounded_history(logger)
        self.table = self._get_or_create_table(table_name, max_read_request_units,max_write_request_units)

    def _memory_namespace(self, config: RunnableConfig):
        """Resolve the long-term-memory namespace forwarded to reducer ``on_prune`` hooks.

        Looks up ``reducer.config.namespace_key`` (default ``"memory_namespace"``)
        in ``config["configurable"]``; the app sets it per invoke, e.g.
        ``{"thread_id": ..., "memory_namespace": ("memories", user_id)}``.
        Falls back to ``("memories", thread_id)`` so apps that never set it
        still get per-thread memory. The checkpointer never builds the namespace
        itself beyond that fallback.
        """
        conf = config.get("configurable", {}) if config else {}
        key = getattr(getattr(self.reducer, "config", None), "namespace_key", "memory_namespace")
        ns = conf.get(key)
        if ns is not None:
            return ns
        thread_id = conf.get("thread_id")
        return ("memories", thread_id) if thread_id is not None else None

    def _apply_reducer(self, checkpoint: Checkpoint, config: Optional[RunnableConfig] = None) -> Checkpoint:
        """Prune the message list in the checkpoint before persistence, if a reducer is configured.

        Non-mutating: returns a shallow copy of the checkpoint with a reduced
        message list. When no reducer is set (or there are no messages), the
        original checkpoint is returned unchanged. The memory namespace resolved
        from ``config`` is forwarded to the reducer so ``on_prune`` hooks can
        write pruned messages to long-term memory (agentstate-reducer >= 0.4.0;
        older reducers ignore it).
        """
        if self.reducer is None:
            return checkpoint
        channel_values = checkpoint.get("channel_values", {})
        messages = channel_values.get(self.messages_key)
        if not messages:
            return checkpoint
        try:
            result = self.reducer.reduce(
                existing=messages, new=[], namespace=self._memory_namespace(config)
            )
        except TypeError:  # agentstate-reducer < 0.4.0: no namespace kwarg
            result = self.reducer.reduce(existing=messages, new=[])
        new_channel_values = dict(channel_values)
        new_channel_values[self.messages_key] = result.surviving
        new_checkpoint = copy.copy(checkpoint)
        new_checkpoint["channel_values"] = new_channel_values
        return new_checkpoint

    def _get_or_create_table(self, table_name: str, max_read_request_units: int, max_write_request_units: int):
        try:
            # Attempt to load the table
            table = self.dynamodb.Table(table_name)
            table.load()  # This will raise an exception if the table does not exist
            logger.info(f"Table '{table_name}' already exists.")
            return table
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                # Table does not exist, create it
                logger.info(f"Table '{table_name}' not found. Creating table...")
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

                logger.info(f"Table '{table_name}' created successfully.")
                return table
            else:
                raise  # Re-raise any other exceptions

    @classmethod
    @contextmanager
    def from_conn_info(cls, *, table_name: str, max_read_request_units: int = 100, max_write_request_units: int = 100, ttl_seconds: int = None, reducer=None, messages_key: str = "messages") -> Iterator["DynamoDBSaver"]:
        saver = None
        try:
            saver = DynamoDBSaver(table_name,max_read_request_units,max_write_request_units, ttl_seconds, reducer=reducer, messages_key=messages_key)
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
        checkpoint = self._apply_reducer(checkpoint, config)
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"]["checkpoint_ns"]
        checkpoint_id = checkpoint["id"]
        parent_checkpoint_id = config["configurable"].get("checkpoint_id")
        key = _make_dynamodb_checkpoint_key(thread_id, checkpoint_ns, checkpoint_id)

        type_, serialized_checkpoint = self.dynamodb_serde.dumps_typed(checkpoint)
        serialized_metadata = self.dynamodb_serde.dumps_typed(metadata)

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

        # Top-level copy of metadata["run_id"] so delete_for_runs can find the
        # item with a filter/GSI without deserializing metadata.
        run_id = metadata.get("run_id") if isinstance(metadata, dict) else None
        if run_id:
            data["run_id"] = str(run_id)

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

    def put_writes(self, config: RunnableConfig, writes: List[Tuple[str, Any]], task_id: str, task_path: str = "") -> None:
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
            # One item per write: the sort key must include the write index, otherwise
            # every write of a task overwrites the previous one.
            SK = DYNAMODB_KEY_SEPARATOR.join([
                checkpoint_id, task_id, str(WRITES_IDX_MAP.get(channel, idx))
            ])
            data = {"PK": thread_id,"SK": SK, "checkpoint_key": key, "channel": channel, "type": type_,
                    "value": serialized_value, "task_path": task_path}
            
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
        logger.debug(f"Deleting items for thread_id: {thread_id}")

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
            logger.debug(f"Fetched {len(items)} items to delete")

            if not items:
                break
            
            with self.table.batch_writer() as batch:
                for item in items:
                    batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
                    total_deleted += 1

            last_evaluated_key = response.get("LastEvaluatedKey")
            if not last_evaluated_key:
                break

        logger.debug(f"Total items deleted: {total_deleted}")




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
        logger.debug(f"Getting checkpoint tuple for config: {config}")
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = get_checkpoint_id(config)
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")

        checkpoint_key = self._get_checkpoint_key(
            self.table, thread_id, checkpoint_ns, checkpoint_id
        )
        if not checkpoint_key:
            return None
        
        checkpoint_id = _parse_dynamodb_checkpoint_key(checkpoint_key)["checkpoint_id"]
        logger.debug(f"Checkpoint key: {checkpoint_key}, checkpoint_id: {checkpoint_id}")
        response = self.table.get_item(Key={"PK": thread_id, "SK": checkpoint_id}, ConsistentRead=True)
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

    def list(self, config: Optional[RunnableConfig], *, filter: Optional[Dict[str, Any]] = None, before: Optional[RunnableConfig] = None, limit: Optional[int] = None) -> Iterator[CheckpointTuple]:
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
        
        # Paginate the whole partition: DynamoDB's Limit applies before FilterExpression,
        # so it cannot be used to cap *matching* checkpoints. Filters, `before` and
        # `limit` are applied here.
        before_id = get_checkpoint_id(before) if before else None
        kwargs = dict(
            KeyConditionExpression=Key('PK').eq(thread_id),
            FilterExpression=Key('checkpoint_key').begins_with(checkpoint_key + DYNAMODB_KEY_SEPARATOR),
            ScanIndexForward=False,
        )
        yielded = 0
        while True:
            resp = self.table.query(**kwargs)
            for data in resp.get("Items", []):
                if not (data and "checkpoint" in data and "metadata" in data):
                    continue
                key = data["checkpoint_key"]
                parsed = _parse_dynamodb_checkpoint_key(key)
                if parsed.get("checkpoint_ns", checkpoint_ns) != checkpoint_ns:
                    continue
                checkpoint_id = parsed["checkpoint_id"]
                if before_id is not None and checkpoint_id >= before_id:
                    continue
                pending_writes = self._load_pending_writes(thread_id, checkpoint_ns, checkpoint_id)
                tup = _parse_dynamodb_checkpoint_data(
                    self.dynamodb_serde, key, data, pending_writes=pending_writes
                )
                if tup is None:
                    continue
                if filter and not all(tup.metadata.get(k) == v for k, v in filter.items()):
                    continue
                yield tup
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                return
            kwargs["ExclusiveStartKey"] = lek

    def delete_thread(self, thread_id: str) -> None:
        """Delete all checkpoints and writes for a thread (LangGraph BaseCheckpointSaver API)."""
        self.delete({"configurable": {"thread_id": thread_id}})

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self.delete_thread, thread_id)

    # ------------------------------------------------------------------
    # Optional LangGraph checkpointer capabilities (copy_thread,
    # delete_for_runs, prune) and their async variants.
    # ------------------------------------------------------------------

    def _iter_thread_items(self, thread_id: str) -> Iterator[dict]:
        """Yield every item (checkpoints and writes) in a thread's partition, paginated."""
        kwargs = dict(KeyConditionExpression=Key("PK").eq(thread_id), ConsistentRead=True)
        while True:
            resp = self.table.query(**kwargs)
            yield from resp.get("Items", [])
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                return
            kwargs["ExclusiveStartKey"] = lek

    @staticmethod
    def _rewrite_thread_in_key(checkpoint_key: str, target_thread_id: str) -> str:
        """Replace the thread segment (index 1) of a checkpoint/writes key."""
        parts = checkpoint_key.split(DYNAMODB_KEY_SEPARATOR)
        parts[1] = target_thread_id
        return DYNAMODB_KEY_SEPARATOR.join(parts)

    def copy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        """Copy every checkpoint and pending write of ``source_thread_id`` to ``target_thread_id``.

        All namespaces are copied; checkpoint ids, parent ids, metadata and
        write ordering are preserved (only the partition key and the thread
        segment of ``checkpoint_key`` change). The source thread is left
        untouched. A nonexistent source is a no-op. Items are copied with a
        ``batch_writer`` so the copy is not atomic: a failure part-way leaves a
        partial target thread.
        """
        if source_thread_id == target_thread_id:
            return
        with self.table.batch_writer() as batch:
            for item in self._iter_thread_items(source_thread_id):
                new_item = dict(item)
                new_item["PK"] = target_thread_id
                if "checkpoint_key" in new_item:
                    new_item["checkpoint_key"] = self._rewrite_thread_in_key(
                        new_item["checkpoint_key"], target_thread_id
                    )
                batch.put_item(Item=new_item)

    async def acopy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        await asyncio.get_running_loop().run_in_executor(
            None, self.copy_thread, source_thread_id, target_thread_id
        )

    def _delete_checkpoint_and_writes(self, batch, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> None:
        """Queue deletes for one checkpoint item and all of its write items."""
        batch.delete_item(Key={"PK": thread_id, "SK": checkpoint_id})
        writes_prefix = DYNAMODB_KEY_SEPARATOR.join(
            ["writes", thread_id, checkpoint_ns, checkpoint_id]
        ) + DYNAMODB_KEY_SEPARATOR
        kwargs = dict(
            KeyConditionExpression=Key("PK").eq(thread_id)
            & Key("SK").begins_with(checkpoint_id + DYNAMODB_KEY_SEPARATOR),
            FilterExpression=Key("checkpoint_key").begins_with(writes_prefix),
            ProjectionExpression="PK, SK",
            ConsistentRead=True,
        )
        while True:
            resp = self.table.query(**kwargs)
            for w in resp.get("Items", []):
                batch.delete_item(Key={"PK": w["PK"], "SK": w["SK"]})
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                return
            kwargs["ExclusiveStartKey"] = lek

    def delete_for_runs(self, run_ids) -> None:
        """Delete every checkpoint (and its writes) whose metadata ``run_id`` is in ``run_ids``.

        Works across all threads and namespaces. An empty list or unknown run
        ids is a no-op.

        Lookup: ``put`` stores a top-level ``run_id`` attribute on checkpoint
        items whenever ``metadata["run_id"]`` is set; this method finds them
        with a paginated table ``Scan`` filtered on that attribute (the IN list
        is chunked to DynamoDB's limit of 100 values). Two caveats:

        * Items written by versions of this package that predate the ``run_id``
          attribute are invisible to this method (their run id lives only inside
          the serialized ``metadata`` blob).
        * A full-table scan is O(table size). For production tables the upgrade
          path is a GSI on ``run_id`` (query instead of scan); the storage
          layout already carries the attribute needed for it.
        """
        run_ids = [r for r in run_ids if r]
        if not run_ids:
            return
        matches: List[dict] = []
        for start in range(0, len(run_ids), 100):
            chunk = run_ids[start:start + 100]
            kwargs = dict(
                FilterExpression=Attr("run_id").is_in(chunk),
                ProjectionExpression="PK, SK, checkpoint_key",
                ConsistentRead=True,
            )
            while True:
                resp = self.table.scan(**kwargs)
                matches.extend(resp.get("Items", []))
                lek = resp.get("LastEvaluatedKey")
                if not lek:
                    break
                kwargs["ExclusiveStartKey"] = lek
        if not matches:
            return
        with self.table.batch_writer() as batch:
            for item in matches:
                parsed = _parse_dynamodb_checkpoint_key(item["checkpoint_key"])
                self._delete_checkpoint_and_writes(
                    batch, item["PK"], parsed["checkpoint_ns"], parsed["checkpoint_id"]
                )

    async def adelete_for_runs(self, run_ids) -> None:
        await asyncio.get_running_loop().run_in_executor(
            None, self.delete_for_runs, list(run_ids)
        )

    def prune(self, thread_ids, *, strategy: str = "keep_latest") -> None:
        """Prune checkpoints for the given threads.

        ``strategy="keep_latest"`` keeps, per thread and per namespace, only the
        checkpoint with the greatest checkpoint id (plus its pending writes) and
        deletes every other checkpoint and their writes. ``strategy="delete"``
        removes everything for the thread (``delete_thread``). Any other value
        raises ``ValueError``. Empty list or unknown threads are a no-op.

        DeltaChannel caveat: this implementation is not delta-aware. A naive
        ``keep_latest`` drops the intermediate checkpoints and writes that
        ``DeltaChannel`` reconstruction walks back through, so delta-backed
        channels on the surviving checkpoint may silently reconstruct as empty.
        Do not prune threads whose graph uses ``DeltaChannel``.
        """
        if strategy not in ("keep_latest", "delete"):
            raise ValueError(
                f"Unknown prune strategy: {strategy!r} (expected 'keep_latest' or 'delete')"
            )
        for thread_id in thread_ids:
            if strategy == "delete":
                self.delete_thread(thread_id)
                continue
            latest: Dict[str, str] = {}  # checkpoint_ns -> greatest checkpoint_id
            seen: List[Tuple[str, str]] = []
            for item in self._iter_thread_items(thread_id):
                key = item.get("checkpoint_key", "")
                if not key.startswith("checkpoint" + DYNAMODB_KEY_SEPARATOR):
                    continue
                parsed = _parse_dynamodb_checkpoint_key(key)
                ns, cid = parsed["checkpoint_ns"], parsed["checkpoint_id"]
                seen.append((ns, cid))
                if ns not in latest or cid > latest[ns]:
                    latest[ns] = cid
            victims = [(ns, cid) for ns, cid in seen if latest[ns] != cid]
            if not victims:
                continue
            with self.table.batch_writer() as batch:
                for ns, cid in victims:
                    self._delete_checkpoint_and_writes(batch, thread_id, ns, cid)

    async def aprune(self, thread_ids, *, strategy: str = "keep_latest") -> None:
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: self.prune(list(thread_ids), strategy=strategy)
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
                (parsed_key["task_id"], parsed_key["idx"]): self.table.get_item(Key={"PK": key["PK"], "SK": key["SK"]}, ConsistentRead=True)['Item']
                for key, parsed_key in sorted(
                    zip(matching_keys, parsed_keys), key=lambda x: x[1]["idx"]
                )
            },
        )
        return pending_writes

    def _get_checkpoint_key(self, table, thread_id: str, checkpoint_ns: str, checkpoint_id: Optional[str]) -> Optional[str]:
        logger.debug(f"Getting checkpoint key for thread_id: {thread_id}, checkpoint_ns: {checkpoint_ns}, checkpoint_id: {checkpoint_id}")
        """Determine the DynamoDB key for a checkpoint."""
        if checkpoint_id:
            return _make_dynamodb_checkpoint_key(thread_id, checkpoint_ns, checkpoint_id)
        
        checkpoint_key = DYNAMODB_KEY_SEPARATOR.join([
        "checkpoint", thread_id, checkpoint_ns
        ])

        all_keys = self._get_filtered_items(thread_id, checkpoint_key)
        
        if not all_keys:
            return None
        latest_key = max(
            all_keys,
            key=lambda k: _parse_dynamodb_checkpoint_key(k["checkpoint_key"])["checkpoint_id"],
        )
        return latest_key["checkpoint_key"]
    
    def _get_filtered_items(
    self,
    thread_id,
    checkpoint_key_prefix,
    max_results=1,
    page_size=10):
        """Retrieve filtered items from DynamoDB based on checkpoint key prefix."""
        last_evaluated_key = None
        results = []

        while len(results) < max_results:
            query_params = {
            "KeyConditionExpression": Key('PK').eq(thread_id),
            "ScanIndexForward": False,
            "Limit": page_size,
            "ConsistentRead": True
            }

            if last_evaluated_key:
                query_params["ExclusiveStartKey"] = last_evaluated_key

            response = self.table.query(**query_params)

            for item in response["Items"]:
                if item.get("checkpoint_key", "").startswith(checkpoint_key_prefix):
                    results.append(item)
                    if len(results) >= max_results:
                        break

            last_evaluated_key = response.get("LastEvaluatedKey")
            if not last_evaluated_key:
                break  # no more data to paginate
 
        logger.debug(f"Filtered items: {len(results)} found for thread_id: {thread_id}, checkpoint_key_prefix: {checkpoint_key_prefix}")
        # Sort results by checkpoint_id in descending order
        return results

    async def aget(self, config: RunnableConfig) -> Optional[Checkpoint]:
        if value := await self.aget_tuple(config):
            return value.checkpoint

    async def aget_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        return await asyncio.get_running_loop().run_in_executor(
            None, self.get_tuple, config
        )

    async def alist(self, config: Optional[RunnableConfig], *,
                    filter: Optional[Dict[str, Any]] = None,
                    before: Optional[RunnableConfig] = None,
                    limit: Optional[int] = None) -> AsyncIterator[CheckpointTuple]:
        loop = asyncio.get_running_loop()
        items = await loop.run_in_executor(
            None, lambda: list(self.list(config, filter=filter, before=before, limit=limit))
        )
        for item in items:
            yield item

    async def aput(
        self, config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata,new_versions: ChannelVersions
    ) -> RunnableConfig:
        return await asyncio.get_running_loop().run_in_executor(
            None, self.put, config, checkpoint, metadata, new_versions
        )

    async def aput_writes(
        self, config: RunnableConfig, writes: List[Tuple[str, Any]], task_id: str, task_path: str = ""
    ) -> None:
        await asyncio.get_running_loop().run_in_executor(
            None, self.put_writes, config, writes, task_id, task_path
        )
 