import asyncio
import time
from asyncio import Queue
from collections import defaultdict
from collections import deque
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any
from uuid import uuid4

import pysignalr
import pysignalr.exceptions
from pysignalr.messages import CompletionMessage

from dipdup.config import HttpConfig
from dipdup.config.evm_node import EvmNodeDatasourceConfig
from dipdup.datasources import EvmHistoryProvider
from dipdup.datasources import EvmRealtimeProvider
from dipdup.datasources import IndexDatasource
from dipdup.datasources._web3 import create_web3_client
from dipdup.exceptions import DatasourceError
from dipdup.exceptions import FrameworkException
from dipdup.models._subsquid import SubsquidMessageType
from dipdup.models.evm import EvmEventData
from dipdup.models.evm import EvmTransactionData
from dipdup.models.evm_node import EvmNodeHeadData
from dipdup.models.evm_node import EvmNodeHeadSubscription
from dipdup.models.evm_node import EvmNodeLogsSubscription
from dipdup.models.evm_node import EvmNodeSubscription
from dipdup.models.evm_node import EvmNodeSyncingData
from dipdup.models.evm_node import EvmNodeSyncingSubscription
from dipdup.performance import metrics
from dipdup.pysignalr import Message
from dipdup.pysignalr import WebsocketMessage
from dipdup.pysignalr import WebsocketProtocol
from dipdup.pysignalr import WebsocketTransport
from dipdup.utils import Watchdog

if TYPE_CHECKING:
    from web3 import AsyncWeb3


NODE_LEVEL_TIMEOUT = 0.1
NODE_LAST_MILE = 128


HeadCallback = Callable[['EvmNodeDatasource', EvmNodeHeadData], Awaitable[None]]
LogsCallback = Callable[['EvmNodeDatasource', tuple[EvmEventData, ...]], Awaitable[None]]
TransactionsCallback = Callable[['EvmNodeDatasource', tuple[EvmTransactionData, ...]], Awaitable[None]]
SyncingCallback = Callable[['EvmNodeDatasource', EvmNodeSyncingData], Awaitable[None]]


@dataclass
class LevelData:
    head: dict[str, Any] | None = None
    events: deque[dict[str, Any]] = field(default_factory=deque)
    fetch_transactions: bool = False

    created_at: float = field(default_factory=time.time)

    async def get_head(self) -> dict[str, Any]:
        await self.wait_level()
        if not self.head:
            raise FrameworkException('LevelData event is set, but head is None')
        return self.head

    async def wait_level(self) -> None:
        to_wait = NODE_LEVEL_TIMEOUT - (time.time() - self.created_at)
        if to_wait > 0:
            await asyncio.sleep(to_wait)


class EvmNodeDatasource(IndexDatasource[EvmNodeDatasourceConfig], EvmHistoryProvider, EvmRealtimeProvider):
    _default_http_config = HttpConfig(
        batch_size=10,
        ratelimit_sleep=1,
        polling_interval=1.0,
    )

    def __init__(self, config: EvmNodeDatasourceConfig, merge_subscriptions: bool = False) -> None:
        super().__init__(config, merge_subscriptions)
        self._web3_client: AsyncWeb3 | None = None
        self._ws_client: WebsocketTransport | None = None
        self._requests: dict[str, tuple[asyncio.Event, Any]] = {}
        self._subscription_ids: dict[str, EvmNodeSubscription] = {}
        self._emitter_queue: Queue[LevelData] = Queue()
        self._level_data: defaultdict[str, LevelData] = defaultdict(LevelData)
        self._watchdog: Watchdog = Watchdog(self._http_config.connection_timeout)

        self._on_head_callbacks: set[HeadCallback] = set()
        self._on_events_callbacks: set[LogsCallback] = set()
        self._on_transactions_callbacks: set[TransactionsCallback] = set()
        self._on_syncing_callbacks: set[SyncingCallback] = set()

    @property
    def web3(self) -> 'AsyncWeb3':
        if not self._web3_client:
            raise FrameworkException('web3 client is not initialized; is datasource running?')
        return self._web3_client

    async def initialize(self) -> None:
        self._web3_client = await create_web3_client(self)
        level = await self.get_head_level()
        self.set_sync_level(None, level)

    async def run(self) -> None:
        if self.realtime:
            await asyncio.gather(
                self._ws_loop(),
                self._emitter_loop(),
                self._watchdog.run(),
            )
        else:
            while True:
                level = await self.get_head_level()
                self.set_sync_level(None, level)
                await asyncio.sleep(self._http_config.polling_interval)

    async def _emitter_loop(self) -> None:
        known_level = 0

        while True:
            level_data = await self._emitter_queue.get()
            head = EvmNodeHeadData.from_json(
                await level_data.get_head(),
            )

            self._logger.info('New head: %s -> %s', known_level, head.level)
            await self.emit_head(head)

            # NOTE: Push rollback to all EVM indexes, but continue processing.
            if head.level <= known_level:
                for type_ in (
                    SubsquidMessageType.blocks,
                    SubsquidMessageType.logs,
                    SubsquidMessageType.traces,
                    SubsquidMessageType.transactions,
                ):
                    await self.emit_rollback(
                        type_,
                        from_level=known_level,
                        to_level=head.level - 1,
                    )

            known_level = head.level

            if raw_events := level_data.events:
                events = tuple(
                    EvmEventData.from_node_json(event, head.timestamp) for event in raw_events if not event['removed']
                )
                if events:
                    self._logger.debug('Emitting %s events', len(events))
                    await self.emit_events(events)
            if level_data.fetch_transactions:
                full_block = await self.get_block_by_level(
                    block_number=head.level,
                    full_transactions=True,
                )
                transactions = tuple(
                    EvmTransactionData.from_node_json(transaction, head.timestamp)
                    for transaction in full_block['transactions']
                )
                if transactions:
                    self._logger.debug('Emitting %s transactions', len(transactions))
                    await self.emit_transactions(transactions)

            del self._level_data[head.hash]

    async def _ws_loop(self) -> None:
        self._logger.info('Establishing realtime connection')
        client = self._get_ws_client()
        retry_sleep = self._http_config.retry_sleep

        for _ in range(1, self._http_config.retry_count + 1):
            try:
                await client.run()
            except pysignalr.exceptions.ConnectionError as e:
                self._logger.error('Websocket connection error: %s', e)
                await self.emit_disconnected()
                await asyncio.sleep(retry_sleep)
                retry_sleep *= self._http_config.retry_multiplier

        raise DatasourceError('Websocket connection failed', self.name)

    @property
    def realtime(self) -> bool:
        return self._config.ws_url is not None

    async def subscribe(self) -> None:
        if not self.realtime:
            return

        missing_subscriptions = self._subscriptions.missing_subscriptions
        if not missing_subscriptions:
            return

        self._logger.info('Subscribing to %s channels', len(missing_subscriptions))
        for subscription in missing_subscriptions:
            if isinstance(subscription, EvmNodeSubscription):
                await self._subscribe(subscription)

    async def emit_head(self, head: EvmNodeHeadData) -> None:
        for fn in self._on_head_callbacks:
            await fn(self, head)

    async def emit_events(self, events: tuple[EvmEventData, ...]) -> None:
        for fn in self._on_events_callbacks:
            await fn(self, events)

    async def emit_syncing(self, syncing: EvmNodeSyncingData) -> None:
        for fn in self._on_syncing_callbacks:
            await fn(self, syncing)

    async def emit_transactions(self, transactions: tuple[EvmTransactionData, ...]) -> None:
        for fn in self._on_transactions_callbacks:
            await fn(self, transactions)

    def call_on_head(self, fn: HeadCallback) -> None:
        self._on_head_callbacks.add(fn)

    def call_on_events(self, fn: LogsCallback) -> None:
        self._on_events_callbacks.add(fn)

    def call_on_transactions(self, fn: TransactionsCallback) -> None:
        self._on_transactions_callbacks.add(fn)

    def call_on_syncing(self, fn: SyncingCallback) -> None:
        self._on_syncing_callbacks.add(fn)

    async def get_block_by_hash(self, block_hash: str) -> dict[str, Any]:
        return await self._jsonrpc_request('eth_getBlockByHash', [block_hash, True])  # type: ignore[no-any-return]

    async def get_block_by_level(self, block_number: int, full_transactions: bool = False) -> dict[str, Any]:
        return await self._jsonrpc_request('eth_getBlockByNumber', [hex(block_number), full_transactions])  # type: ignore[no-any-return]

    async def get_events(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return await self._jsonrpc_request('eth_getLogs', [params])  # type: ignore[no-any-return]

    async def get_head_level(self) -> int:
        return int((await self._jsonrpc_request('eth_blockNumber', [])), 16)

    async def _subscribe(self, subscription: EvmNodeSubscription) -> None:
        self._logger.debug('Subscribing to %s', subscription)
        response = await self._jsonrpc_request(
            method='eth_subscribe',
            params=subscription.get_params(),
            ws=True,
        )
        self._subscription_ids[response] = subscription
        # NOTE: Is's likely unnecessary and/or unreliable, but node doesn't return sync level.
        level = await self.get_head_level()
        self._subscriptions.set_sync_level(subscription, level)

    async def _jsonrpc_request(
        self,
        method: str,
        params: Any,
        raw: bool = False,
        ws: bool = False,
    ) -> Any:
        request_id = uuid4().hex
        request = {
            'jsonrpc': '2.0',
            'id': request_id,
            'method': method,
            'params': params,
        }

        if ws:
            started_at = time.time()
            event = asyncio.Event()
            self._requests[request_id] = (event, None)

            message = WebsocketMessage(request)
            client = self._get_ws_client()

            async def _request() -> None:
                await client.send(message)
                await event.wait()

            await asyncio.wait_for(
                _request(),
                timeout=self._http_config.request_timeout,
            )
            data = self._requests[request_id][1]
            del self._requests[request_id]

            metrics.time_in_requests[self.name] += time.time() - started_at
            metrics.requests_total[self.name] += 1
        else:
            data = await self.request(
                method='post',
                url='',
                json=request,
            )

        if raw:
            return data

        if 'error' in data:
            raise DatasourceError(data['error']['message'], self.name)
        return data['result']

    async def _on_message(self, message: Message) -> None:
        # NOTE: pysignalr will eventually get a raw client
        if not isinstance(message, WebsocketMessage):
            raise FrameworkException(f'Unknown message type: {type(message)}')

        data = message.data
        self._watchdog.reset()

        if 'id' in data:
            request_id = data['id']
            self._logger.debug('Received response for request %s', request_id)
            if request_id not in self._requests:
                raise DatasourceError(f'Unknown request ID: {data["id"]}', self.name)

            event = self._requests[request_id][0]
            # NOTE: Don't unpack; processed later
            self._requests[request_id] = (event, data)
            event.set()
        elif 'method' in data:
            if data['method'] == 'eth_subscription':
                subscription_id = data['params']['subscription']
                if subscription_id not in self._subscription_ids:
                    raise FrameworkException(f'{self.name}: Unknown subscription ID: {subscription_id}')
                subscription = self._subscription_ids[subscription_id]
                await self._handle_subscription(subscription, data['params']['result'])
            else:
                raise DatasourceError(f'Unknown method: {data["method"]}', self.name)
        else:
            raise DatasourceError(f'Unknown message: {data}', self.name)

    async def _handle_subscription(self, subscription: EvmNodeSubscription, data: Any) -> None:
        if isinstance(subscription, EvmNodeHeadSubscription):
            level_data = self._level_data[data['hash']]
            level_data.head = data
            if subscription.transactions:
                level_data.fetch_transactions = True
            self._emitter_queue.put_nowait(level_data)
        elif isinstance(subscription, EvmNodeLogsSubscription):
            level_data = self._level_data[data['blockHash']]
            level_data.events.append(data)
        elif isinstance(subscription, EvmNodeSyncingSubscription):
            syncing = EvmNodeSyncingData.from_json(data)
            await self.emit_syncing(syncing)
        else:
            raise NotImplementedError

    async def _on_error(self, message: CompletionMessage) -> None:
        raise DatasourceError(f'Node error: {message}', self.name)

    async def _on_connected(self) -> None:
        self._logger.info('Realtime connection established')
        # NOTE: Subscribing here will block WebSocket loop, don't do it.
        await self.emit_connected()

    async def _on_disconnected(self) -> None:
        self._logger.info('Realtime connection lost, resetting subscriptions')
        self._subscriptions.reset()
        await self.emit_disconnected()

    def _get_ws_client(self) -> WebsocketTransport:
        if self._ws_client:
            return self._ws_client

        self._logger.debug('Creating Websocket client')

        url = self._config.ws_url
        if not url:
            raise FrameworkException('Spawning node datasource, but `ws_url` is not set')
        self._ws_client = WebsocketTransport(
            url=url,
            protocol=WebsocketProtocol(),
            callback=self._on_message,
            skip_negotiation=True,
            connection_timeout=self._http_config.connection_timeout,
        )

        self._ws_client.on_open(self._on_connected)
        self._ws_client.on_close(self._on_disconnected)
        self._ws_client.on_error(self._on_error)

        return self._ws_client
