"""
Hyperliquid WebSocket 실시간 가격 피드.

allMids 채널 구독 → 가격을 메모리에 유지.
REST 폴링 대비 ~50ms 이내 반응으로 청산 버퍼 실시간 감시에 사용.

구조:
  WSPriceFeed.run()  ← asyncio.Task 로 영구 구동 (재연결 포함)
  WSPriceFeed.get_price(asset) ← 즉시 반환 (동기)
  WSPriceFeed.wait_ready()     ← 첫 메시지 수신까지 대기
"""
import asyncio
import json
import logging
import time

import websockets
from websockets.exceptions import ConnectionClosed

from pool_config import ASSET_PRICE_MAP

logger = logging.getLogger(__name__)

HL_WS_URL     = "wss://api.hyperliquid.xyz/ws"
RECONNECT_BASE = 3    # 첫 재연결 대기(초)
RECONNECT_MAX  = 60   # 최대 백오프(초)
PING_INTERVAL  = 20   # WS keepalive ping 간격(초)


class WSPriceFeed:
    def __init__(self):
        self._prices:   dict[str, float] = {}
        self._ready   = asyncio.Event()
        self._running = False
        self.last_msg_ts: float = 0.0
        self.reconnect_count: int = 0

    # ── 가격 조회 (동기, 즉시) ──────────────────────────

    def get_price(self, asset: str) -> float | None:
        """ASSET_PRICE_MAP 경유 HL 티커로 최신 가격 반환. 미수신 시 None."""
        hl = ASSET_PRICE_MAP.get(asset, asset)
        return self._prices.get(hl)

    def is_stale(self, max_age: float = 30.0) -> bool:
        """마지막 메시지로부터 max_age 초 이상 경과 시 True."""
        return (time.time() - self.last_msg_ts) > max_age

    # ── 준비 대기 ────────────────────────────────────────

    async def wait_ready(self, timeout: float = 10.0):
        """첫 allMids 메시지 수신까지 대기. 타임아웃 시 TimeoutError."""
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)

    # ── 영구 실행 루프 ───────────────────────────────────

    async def run(self):
        """재연결 포함 영구 실행. asyncio.create_task 로 구동."""
        self._running = True
        backoff = RECONNECT_BASE
        while self._running:
            try:
                await self._connect()
                backoff = RECONNECT_BASE  # 정상 연결 후 초기화
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.reconnect_count += 1
                logger.warning(
                    f"[WS] 연결 끊김 ({e.__class__.__name__}: {e})  "
                    f"{backoff}s 후 재연결 (#{self.reconnect_count})"
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)

        logger.info("[WS] 피드 종료")

    async def stop(self):
        self._running = False

    # ── 내부: 단일 연결 세션 ─────────────────────────────

    async def _connect(self):
        logger.info("[WS] Hyperliquid 연결 시도...")
        async with websockets.connect(
            HL_WS_URL,
            ping_interval=PING_INTERVAL,
            ping_timeout=10,
        ) as ws:
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "allMids"},
            }))
            logger.info("[WS] allMids 구독 완료 — 실시간 가격 수신 중")

            async for raw in ws:
                self._handle(raw)

    def _handle(self, raw: str):
        try:
            msg = json.loads(raw)
            if msg.get("channel") != "allMids":
                return
            mids = msg["data"]["mids"]
            self._prices.update({k: float(v) for k, v in mids.items()})
            self.last_msg_ts = time.time()
            if not self._ready.is_set():
                self._ready.set()
        except Exception as e:
            logger.debug(f"[WS] 파싱 오류: {e}")
