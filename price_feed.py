"""
Hyperliquid 공개 API를 통해 실시간 가격 및 펀딩레이트를 조회합니다.
인증 불필요 — 페이퍼 트레이딩에서 실제 시장 데이터를 사용합니다.
"""
import logging
import aiohttp

logger = logging.getLogger(__name__)

HL_API = "https://api.hyperliquid.xyz/info"

# Byreal Perps는 Hyperliquid 엔진 위에서 동작하므로 동일 데이터 사용
ASSET_MAP = {
    "ETH": "ETH",
    "BTC": "BTC",
    "SOL": "SOL",
}


class PriceFeed:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def get_price(self, asset: str) -> float:
        """현재 mid 가격 조회 (USDC 기준)"""
        hl_asset = ASSET_MAP.get(asset, asset)
        session = await self._get_session()
        try:
            async with session.post(HL_API, json={"type": "allMids"}) as resp:
                data = await resp.json()
                price = float(data[hl_asset])
                return price
        except Exception as e:
            logger.error(f"[PriceFeed] 가격 조회 실패 ({asset}): {e}")
            raise

    async def get_funding_rate(self, asset: str) -> float:
        """시간당 펀딩레이트 조회 (소수점, 예: 0.0001 = 0.01%/h)

        Short 포지션 기준:
          양수 → Short이 수령 (longs→shorts)
          음수 → Short이 지급 (shorts→longs)
        """
        hl_asset = ASSET_MAP.get(asset, asset)
        session = await self._get_session()
        try:
            async with session.post(HL_API, json={"type": "metaAndAssetCtxs"}) as resp:
                meta, asset_ctxs = await resp.json()
                universe = meta["universe"]
                for i, coin in enumerate(universe):
                    if coin["name"] == hl_asset:
                        # funding 필드는 8시간 기준 → 시간당으로 변환
                        funding_8h = float(asset_ctxs[i]["funding"])
                        return funding_8h / 8.0
                logger.warning(f"[PriceFeed] {asset} 펀딩레이트 없음, 0 반환")
                return 0.0
        except Exception as e:
            logger.error(f"[PriceFeed] 펀딩레이트 조회 실패 ({asset}): {e}")
            return 0.0

    async def get_snapshot(self, asset: str) -> dict:
        """가격 + 펀딩레이트 한 번에 조회"""
        price = await self.get_price(asset)
        funding = await self.get_funding_rate(asset)
        return {"price": price, "funding_rate_1h": funding}

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
