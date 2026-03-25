"""
Hyperliquid 공개 API를 통해 실시간 가격 및 펀딩레이트를 조회합니다.
인증 불필요 — 페이퍼 트레이딩에서 실제 시장 데이터를 사용합니다.
"""
import logging
import time
import aiohttp

import config

logger = logging.getLogger(__name__)

HL_API             = "https://api.hyperliquid.xyz/info"
DEFILLAMA_TVL_API  = "https://api.llama.fi/tvl/byreal"
DEFILLAMA_FEES_API = "https://api.llama.fi/summary/fees/byreal"
GT_POOL_API        = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool_id}"

# Byreal Perps는 Hyperliquid 엔진 위에서 동작하므로 동일 데이터 사용
ASSET_MAP = {
    "ETH": "ETH",
    "BTC": "BTC",
    "SOL": "SOL",
}


class PriceFeed:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        # DeFi Llama 프로토콜 전체 캐시
        self._stats_cache: dict = {}
        self._stats_cache_ts: float = 0.0
        # GeckoTerminal 풀별 캐시 {pool_id: (ts, data)}
        self._gt_pool_cache: dict[str, tuple[float, dict]] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def get_price(self, asset: str) -> float:
        """현재 mid 가격 조회 (USDC 기준).

        pool_config.ASSET_PRICE_MAP을 통해
        WETH→ETH, XAUt0→PAXG 등 자동 매핑.
        """
        from pool_config import ASSET_PRICE_MAP
        hl_asset = ASSET_PRICE_MAP.get(asset) or ASSET_MAP.get(asset, asset)
        session = await self._get_session()
        try:
            async with session.post(HL_API, json={"type": "allMids"}) as resp:
                data = await resp.json()
                price = float(data[hl_asset])
                return price
        except Exception as e:
            logger.error(f"[PriceFeed] 가격 조회 실패 ({asset} → {hl_asset}): {e}")
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

    async def get_pool_stats_gt(self, pool_id: str) -> dict:
        """GeckoTerminal에서 특정 풀의 TVL + 24h 수수료 조회 (5분 캐시).

        반환값:
          tvl            : 풀 TVL (USD)
          daily_lp_fees  : LP 귀속 24h 수수료 (USD)
          vol_24h        : 24h 거래량
          source         : "geckoterminal" | "fallback"
        """
        now = time.time()
        cached = self._gt_pool_cache.get(pool_id)
        if cached and now - cached[0] < config.POOL_STATS_REFRESH_INTERVAL:
            return cached[1]

        session = await self._get_session()
        url = GT_POOL_API.format(pool_id=pool_id)
        try:
            async with session.get(
                url,
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                data = await r.json()
                attr = data["data"]["attributes"]
                tvl    = float(attr.get("reserve_in_usd") or 0)
                vol24h = float((attr.get("volume_usd") or {}).get("h24") or 0)
                lp_fees = vol24h * (1 - config.LP_TREASURY_CUT) * 0.003  # 0.3% fee
                stats = {
                    "tvl":           tvl,
                    "vol_24h":       vol24h,
                    "daily_lp_fees": lp_fees,
                    "source":        "geckoterminal",
                }
                self._gt_pool_cache[pool_id] = (now, stats)
                return stats
        except Exception as e:
            logger.warning(f"[PoolStats/GT] {pool_id[:8]}... 조회 실패 ({e})")
            fallback = {
                "tvl":           config.ESTIMATED_POOL_TVL,
                "vol_24h":       config.ESTIMATED_POOL_DAILY_VOLUME,
                "daily_lp_fees": config.ESTIMATED_POOL_DAILY_VOLUME * 0.003 * (1 - config.LP_TREASURY_CUT),
                "source":        "fallback",
            }
            self._gt_pool_cache[pool_id] = (now - config.POOL_STATS_REFRESH_INTERVAL + 30, fallback)
            return fallback

    async def get_byreal_stats(self) -> dict:
        """Byreal 프로토콜 TVL + 24h LP 수수료 수익 조회 (DeFi Llama, 캐시 적용).

        반환값:
          tvl            : 프로토콜 전체 TVL (USD)
          daily_lp_fees  : LP 귀속 24h 수수료 (USD) — treasury 제외
          source         : "defillama" | "fallback"
        """
        now = time.time()
        if now - self._stats_cache_ts < config.POOL_STATS_REFRESH_INTERVAL and self._stats_cache:
            return self._stats_cache

        session = await self._get_session()
        try:
            async with session.get(DEFILLAMA_TVL_API, timeout=aiohttp.ClientTimeout(total=8)) as r:
                tvl = float(await r.json())

            async with session.get(DEFILLAMA_FEES_API, timeout=aiohttp.ClientTimeout(total=8)) as r:
                fees_data = await r.json()
                # total24h = 트레이더가 낸 수수료 합계 (protocol + LP)
                total_fees_24h = float(fees_data.get("total24h") or 0)
                # LP 귀속분 (treasury 12% 제외)
                lp_fees_24h = total_fees_24h * (1 - config.LP_TREASURY_CUT)

            stats = {
                "tvl":           tvl,
                "daily_lp_fees": lp_fees_24h,
                "source":        "defillama",
            }
            self._stats_cache    = stats
            self._stats_cache_ts = now
            logger.info(
                f"[PoolStats] TVL: ${tvl:,.0f}  |  24h LP 수수료: ${lp_fees_24h:,.2f}  (DeFi Llama)"
            )
            return stats

        except Exception as e:
            logger.warning(f"[PoolStats] DeFi Llama 조회 실패 ({e}) — config 기본값 사용")
            # 폴백: config의 추정값으로 계산
            est_daily_fees = (
                config.ESTIMATED_POOL_DAILY_VOLUME
                * (config.LP_FEE_TIER / 100)
                * (1 - config.LP_TREASURY_CUT)
                / config.POOL_ETH_USDC_SHARE  # 역산으로 프로토콜 전체 추정
            )
            fallback = {
                "tvl":           config.ESTIMATED_POOL_TVL / config.POOL_ETH_USDC_SHARE,
                "daily_lp_fees": est_daily_fees,
                "source":        "fallback",
            }
            # 실패해도 캐시해서 매번 재시도 하지 않음 (30초만 캐시)
            self._stats_cache    = fallback
            self._stats_cache_ts = now - config.POOL_STATS_REFRESH_INTERVAL + 30
            return fallback

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
