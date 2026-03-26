"""
멀티 풀 병렬 실행 엔진.

4개 풀을 하나의 asyncio 이벤트 루프에서 동시에 실행하고
단일 대시보드로 통합 리포팅한다.
"""
import asyncio
import csv
import logging
import os
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
        os.makedirs("logs", exist_ok=True)
        self._csv_path = "logs/multi_pnl_history.csv"

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

    def report(self, clear: bool = True):
        if not self.engines:
            return

        elapsed = time.time() - self.start_time
        elapsed_str = _fmt_elapsed(elapsed)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        elapsed_h = elapsed / 3600

        if clear:
            os.system("cls" if os.name == "nt" else "clear")

        W = 96
        HDR = (f" {'POOL':<14} {'TVL':>7} {'Cap':>6} {'LP Val':>8}"
               f" {'Fees':>9} {'Perp':>9} {'Net PnL':>9} {'APR':>8} {'Liq':>6}  Status")
        print("=" * W)
        print(f" BYREAL MULTI-POOL PAPER  |  {now_str}  |  elapsed: {elapsed_str}  |  #{self.interval_count}")
        print("=" * W)
        print(HDR)
        print(" " + "─" * (W - 2))

        total_capital = total_net = total_fees = total_perp = 0.0
        rows = []
        csv_rows = []

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
                funding  = engine.perp.funding_received
                il       = engine.get_il(price)
                entry_c  = engine.entry_costs
                rebal_c  = engine.rebalance_costs
                reset_c  = engine.reset_costs
                total_c  = entry_c + rebal_c + reset_c
                capital  = cfg.capital
                in_range = engine.lp.in_range
                resets   = engine.range_reset_count
                apr_pct  = (
                    (net_pnl / capital) * (8760 / elapsed_h) * 100
                    if elapsed >= 600 else None
                )
                liq_buf  = engine.get_liquidation_buffer(price) * 100
            except Exception:
                continue

            total_capital += capital
            total_net     += net_pnl
            total_fees    += fees
            total_perp    += perp_pnl

            gt_cache = self.price_feed._gt_pool_cache.get(cfg.gt_pool_id)
            pool_tvl = gt_cache[1]["tvl"] if gt_cache else 0
            status   = "IN" if in_range else f"OUT R{resets}"
            rows.append((name, pool_tvl, capital, lp_val, fees, perp_pnl, net_pnl,
                         apr_pct, liq_buf, status, il, entry_c, rebal_c, reset_c, total_c, funding))
            csv_rows.append({
                "timestamp":    datetime.now(timezone.utc).isoformat(),
                "elapsed_h":    round(elapsed_h, 4),
                "pool":         name,
                "price":        round(price, 4),
                "lp_value":     round(lp_val, 4),
                "il":           round(il, 4),
                "fees":         round(fees, 4),
                "perp_pnl":     round(perp_pnl, 4),
                "funding":      round(funding, 4),
                "entry_costs":  round(entry_c, 4),
                "rebal_costs":  round(rebal_c, 4),
                "reset_costs":  round(reset_c, 4),
                "total_costs":  round(total_c, 4),
                "net_pnl":      round(net_pnl, 4),
                "apr_pct":      round(apr_pct, 2) if apr_pct is not None else None,
                "resets":       resets,
                "in_range":     in_range,
            })

        # ── 메인 테이블 출력 ───────────────────────────────
        for row in rows:
            name, pool_tvl, cap, lp_val, fees, perp, net, apr, liq_buf, status = row[:10]
            tvl_str  = f"${pool_tvl/1000:.0f}k" if pool_tvl >= 1000 else f"${pool_tvl:.0f}"
            cap_str  = f"${cap:>5,.0f}"
            lp_str   = f"${lp_val:>7.2f}"
            fees_str = _fmt_pnl(fees)
            perp_str = _fmt_pnl(perp)
            net_str  = _fmt_pnl(net)
            apr_str  = f"{apr:>+7.1f}%" if apr is not None else "    N/A "
            buf_flag = "!" if liq_buf < 8 else ("*" if liq_buf < 15 else " ")
            liq_str  = f"{buf_flag}{liq_buf:>4.1f}%"
            print(f" {name:<14} {tvl_str:>7} {cap_str:>6} {lp_str:>8}"
                  f" {fees_str:>9} {perp_str:>9} {net_str:>9} {apr_str:>8} {liq_str:>6}  {status}")

        # ── TOTAL ──────────────────────────────────────────
        total_apr = (
            (total_net / max(total_capital, 1)) * (8760 / elapsed_h) * 100
            if elapsed >= 600 else None
        )
        in_cnt  = sum(1 for _, e in self.engines.items() if e.lp and e.lp.in_range)
        in_str  = f"{in_cnt}/{len(self.engines)} IN"
        apr_str = f"{total_apr:>+7.1f}%" if total_apr is not None else "    N/A "
        print(" " + "═" * (W - 2))
        print(f" {'TOTAL':<14} {'':>7} ${total_capital:>5,.0f} {'':>8}"
              f" {_fmt_pnl(total_fees):>9} {_fmt_pnl(total_perp):>9}"
              f" {_fmt_pnl(total_net):>9} {apr_str:>8} {'':>6}  {in_str}")
        print("=" * W)

        # ── IL / 비용 분석 섹션 ────────────────────────────
        IL_HDR = (f" {'POOL':<14} {'IL':>9} {'Fees':>9} {'EntryCst':>9}"
                  f" {'RstCst':>8} {'RblCst':>8} {'TotCost':>9}  Fee/Cost")
        print(f"\n IL & COST BREAKDOWN  (보수적 비용 반영 — 슬리피지 0.5%/side, 진입비 0.2%)")
        print(" " + "─" * (W - 2))
        print(IL_HDR)
        print(" " + "─" * (W - 2))
        for row in rows:
            name = row[0]
            fees = row[4]
            il, entry_c, rebal_c, reset_c, total_c = row[10], row[11], row[12], row[13], row[14]
            fee_cost_ratio = (fees / total_c) if total_c > 1e-8 else float("inf")
            fc_str = f"{fee_cost_ratio:.1f}x" if fee_cost_ratio != float("inf") else "  ∞"
            print(f" {name:<14} {_fmt_pnl(il):>9} {_fmt_pnl(fees):>9} {_fmt_pnl(-entry_c):>9}"
                  f" {_fmt_pnl(-reset_c):>8} {_fmt_pnl(-rebal_c):>8} {_fmt_pnl(-total_c):>9}  {fc_str}")
        print(" " + "─" * (W - 2))

        # ── CSV 저장 ───────────────────────────────────────
        if csv_rows:
            self._append_csv(csv_rows)

    # ── WS 실시간 청산 감시 ────────────────────────────────

    async def ws_monitor(self, ws_feed):
        """WS 가격 기반으로 매 1초 청산 버퍼 점검.

        REST 폴링(5분)과 별개 Task로 동시 실행되므로
        가격 급변 시 즉각 _emergency_topup이 트리거된다.
        """
        logger.info("[WS-MON] 청산 버퍼 실시간 감시 시작")
        while True:
            for name, engine in list(self.engines.items()):
                if not engine.perp or engine.perp.leverage <= 1.0:
                    continue
                price = ws_feed.get_price(engine.pool_cfg.lp_token)
                if price is None:
                    continue
                self._last_prices[name] = price
                engine._check_liquidation_buffer(price)
            await asyncio.sleep(1)

    def _append_csv(self, rows: list[dict]):
        write_header = not os.path.exists(self._csv_path)
        with open(self._csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            if write_header:
                writer.writeheader()
            writer.writerows(rows)

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


def _fmt_pnl(v: float) -> str:
    """부호 포함 금액 문자열. 예: +$0.0038 / -$0.0032 (8자 고정)"""
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):.4f}"


def _fmt_elapsed(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h {m}m"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"
