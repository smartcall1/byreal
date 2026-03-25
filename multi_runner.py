"""
멀티 풀 병렬 실행 엔진.

4개 풀을 하나의 asyncio 이벤트 루프에서 동시에 실행하고
단일 대시보드로 통합 리포팅한다.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

import config
from paper_engine import PaperTradingEngine
from pool_config import POOL_CONFIGS, PoolConfig
from price_feed import PriceFeed

logger = logging.getLogger(__name__)


class MultiPoolRunner:
    def __init__(self, price_feed: PriceFeed):
        self.price_feed = price_feed
        self.engines:     dict[str, PaperTradingEngine] = {}
        self._last_prices: dict[str, float] = {}   # 풀별 마지막 가격 캐시
        self.start_time  = time.time()
        self.interval_count = 0

    # ── 초기화 ─────────────────────────────────────────────

    async def initialize(self):
        tasks = [self._init_pool(cfg) for cfg in POOL_CONFIGS]
        await asyncio.gather(*tasks)
        logger.info(f"[MULTI] {len(self.engines)}개 풀 초기화 완료")

    async def _init_pool(self, cfg: PoolConfig):
        try:
            price  = await self.price_feed.get_price(cfg.lp_token)
            engine = PaperTradingEngine(self.price_feed, cfg)
            await engine.initialize(price)
            self.engines[cfg.name] = engine
            self._last_prices[cfg.name] = price
            logger.info(f"[{cfg.name}] 초기화 완료  ${price:,.2f}")
        except Exception as e:
            logger.error(f"[{cfg.name}] 초기화 실패: {e}")

    # ── 메인 업데이트 ───────────────────────────────────────

    async def update_all(self):
        """4개 풀을 병렬 업데이트."""
        tasks = [self._update_pool(name, engine)
                 for name, engine in self.engines.items()]
        await asyncio.gather(*tasks)
        self.interval_count += 1

    async def _update_pool(self, name: str, engine: PaperTradingEngine):
        cfg = engine.pool_cfg
        try:
            price    = await self.price_feed.get_price(cfg.lp_token)
            funding  = await self.price_feed.get_funding_rate(cfg.hedge_asset)
            pool_stats = await self.price_feed.get_pool_stats_gt(cfg.gt_pool_id)
            await engine.update(price, funding, pool_stats)
            self._last_prices[name] = price
        except Exception as e:
            logger.error(f"[{name}] 업데이트 오류: {e}")

    # ── 통합 리포트 ─────────────────────────────────────────

    def report(self):
        if not self.engines:
            return

        elapsed = time.time() - self.start_time
        elapsed_str = _fmt_elapsed(elapsed)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # ── 헤더 ───────────────────────────────────────────
        W = 92
        print("=" * W)
        print(f" BYREAL MULTI-POOL PAPER  |  {now_str}  |  경과: {elapsed_str}  |  #{self.interval_count}")
        print("=" * W)
        print(f" {'POOL':<14} {'Pool TVL':>9} {'자본':>7} {'LP':>8} {'수수료':>8} {'Perp':>8} {'순P&L':>8} {'APR':>7} {'LiqBuf':>7} {'상태':>6}")
        print(" " + "─" * (W - 2))

        total_capital = total_net = total_fees = total_perp = 0.0
        all_in_range = True

        rows = []
        for name, engine in self.engines.items():
            if not engine.lp or not engine.perp:
                continue
            cfg = engine.pool_cfg

            try:
                price    = self._last_prices.get(name, engine.lp.entry_price)
                lp_val   = engine.get_lp_value(price)
                net_pnl  = engine.get_net_pnl(price)
                fees     = engine.lp.fees_accrued
                perp_pnl = engine.perp.pnl
                capital  = cfg.capital
                in_range = engine.lp.in_range
                resets   = engine.range_reset_count

                apr_pct = (net_pnl / capital) * (8760 / max(elapsed / 3600, 0.001)) * 100
                liq_buf = engine.get_liquidation_buffer(price) * 100
            except Exception:
                continue

            all_in_range = all_in_range and in_range
            total_capital += capital
            total_net     += net_pnl
            total_fees    += fees
            total_perp    += perp_pnl

            # 풀 TVL (GeckoTerminal 캐시에서)
            gt_cache = self.price_feed._gt_pool_cache.get(cfg.gt_pool_id)
            pool_tvl = gt_cache[1]["tvl"] if gt_cache else 0

            status = "✓ IN" if in_range else f"⚠ OUT(R{resets})"
            rows.append((name, pool_tvl, capital, lp_val, fees, perp_pnl, net_pnl, apr_pct, liq_buf, status))

        for name, pool_tvl, cap, lp_val, fees, perp, net, apr, liq_buf, status in rows:
            tvl_str = f"${pool_tvl/1000:.0f}k" if pool_tvl >= 1000 else f"${pool_tvl:.0f}"
            # 청산 버퍼 표시: 8% 이하 빨강(!), 15% 이하 주의(*), 정상(공백)
            buf_flag = "!" if liq_buf < 8 else ("*" if liq_buf < 15 else " ")
            print(
                f" {name:<14} {tvl_str:>9} "
                f"${cap:>6,.0f} "
                f"${lp_val:>7,.2f} "
                f"${fees:>+7.4f} "
                f"${perp:>+7.4f} "
                f"${net:>+7.4f} "
                f"{apr:>+6.1f}% "
                f"{buf_flag}{liq_buf:>5.1f}% "
                f" {status}"
            )

        # ── 합계 ───────────────────────────────────────────
        total_apr = (total_net / max(total_capital, 1)) * (8760 / max(elapsed / 3600, 0.001)) * 100
        in_str = f"{sum(1 for _,e in self.engines.items() if e.lp and e.lp.in_range)}/{len(self.engines)} ✓"
        print(" " + "═" * (W - 2))
        print(
            f" {'TOTAL':<14} {'':>9} "
            f"${total_capital:>6,.0f} "
            f"{'':>8} "
            f"${total_fees:>+7.4f} "
            f"${total_perp:>+7.4f} "
            f"${total_net:>+7.4f} "
            f"{total_apr:>+6.1f}% "
            f"{'':>7} "
            f" {in_str}"
        )
        print("=" * W)

    def final_report(self):
        logger.info("━" * 80)
        logger.info(" 최종 결과 (봇 종료)")
        self.report()


# ── 유틸 ───────────────────────────────────────────────────

def _get_last_price(engine: PaperTradingEngine) -> float:
    """마지막 업데이트 시점의 price를 Perp 포지션에서 역산."""
    if not engine.perp or not engine.lp:
        return 0.0
    # entry_price 또는 lp.entry_price 사용
    return engine.lp.entry_price


def _fmt_elapsed(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h {m}m"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"
