"""
손익 추적 및 리포팅.

매 LOG_INTERVAL마다 콘솔에 현황을 출력하고,
logs/pnl_history.csv 에 누적 기록한다.
"""
import csv
import logging
import os
import time
from datetime import datetime, timezone

import config

logger = logging.getLogger(__name__)


class PnLTracker:
    def __init__(self, engine):
        self.engine     = engine
        self.start_time = time.time()
        os.makedirs("logs", exist_ok=True)
        self._csv_path  = "logs/pnl_history.csv"
        self._csv_initialized = False

    # ── 정기 리포트 ──────────────────────────────────────

    def report(self, current_price: float):
        if not self.engine.lp or not self.engine.perp:
            return

        lp      = self.engine.lp
        perp    = self.engine.perp

        lp_value   = self.engine.get_lp_value(current_price)
        lp_gain    = lp_value - lp.initial_value   # LP 가치 변화 (HODL gain + IL 포함)
        il         = self.engine.get_il(current_price)  # IL만 따로 (분석용)
        fees       = lp.fees_accrued
        perp_pnl   = perp.pnl
        funding    = perp.funding_received
        rb_costs   = self.engine.rebalance_costs
        net_pnl    = self.engine.get_net_pnl(current_price)  # lp_gain + perp_pnl + fees + funding - costs

        capital    = config.INITIAL_CAPITAL
        net_pct    = net_pnl / capital * 100

        elapsed_h  = (time.time() - self.start_time) / 3600
        annualized = (net_pnl / capital) * (8760 / max(elapsed_h, 0.001)) * 100

        hedge_delta = perp.size
        lp_delta    = self.engine.get_current_delta(current_price)
        hedge_diff  = lp_delta - hedge_delta  # 양수면 under-hedged

        range_tag   = "IN RANGE" if lp.in_range else "OUT OF RANGE !"

        row = {
            "timestamp":       datetime.now(timezone.utc).isoformat(),
            "price":           round(current_price, 4),
            "lp_value":        round(lp_value, 4),
            "il":              round(il, 4),
            "fees":            round(fees, 4),
            "perp_pnl":        round(perp_pnl, 4),
            "funding":         round(funding, 4),
            "rebalance_costs": round(rb_costs, 4),
            "net_pnl":         round(net_pnl, 4),
            "net_pct":         round(net_pct, 4),
            "annualized_apr":  round(annualized, 2),
            "in_range":        lp.in_range,
            "rebalance_count": self.engine.rebalance_count,
        }
        self._append_csv(row)

        # ── 콘솔 출력 ──────────────────────────────────
        elapsed_str = _fmt_elapsed(elapsed_h * 3600)
        logger.info("=" * 68)
        logger.info(
            f" PAPER REPORT  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  "
            f"경과: {elapsed_str}"
        )
        logger.info(f" ETH 가격  : ${current_price:>10,.2f}   [{range_tag}]")
        logger.info(f" LP 가치   : ${lp_value:>10,.2f}   (초기 ${lp.initial_value:,.2f}  |  변화 ${lp_gain:>+.2f})")
        logger.info(" ──────────────────────────────────────────────────────────")
        logger.info(f" LP 가치변화: ${lp_gain:>+10.4f}  (IL {il:>+.4f}  |  HODL gain {lp_gain-il:>+.4f})")
        logger.info(f" Perp PnL  : ${perp_pnl:>+10.4f}  (Short {perp.size:.6f} ETH  ≈ HODL 상쇄)")
        logger.info(f" 수수료 수익: ${fees:>+10.4f}  ← 핵심 수익원")
        logger.info(f" 펀딩 수령  : ${funding:>+10.4f}")
        logger.info(f" 리밸런스비 : ${-rb_costs:>+10.4f}  ({self.engine.rebalance_count}회)")
        logger.info(" ══════════════════════════════════════════════════════════")
        logger.info(
            f" 순손익    : ${net_pnl:>+10.4f}  "
            f"({net_pct:>+.2f}%)  |  연환산 APR: {annualized:>+.1f}%"
        )
        if abs(hedge_diff) > 1e-6:
            logger.info(
                f" 델타 잔여  : {hedge_diff:>+.6f} ETH  "
                f"(LP {lp_delta:.6f} vs Hedge {hedge_delta:.6f})"
            )

        # ── 포지션 상태 정보 ───────────────────────────────
        if self.engine.lp and self.engine.perp:
            lp = self.engine.lp
            perp = self.engine.perp

            # 범위까지 거리 (%)
            dist_lower = (current_price - lp.price_lower) / current_price * 100
            dist_upper = (lp.price_upper - current_price) / current_price * 100

            # Perp 레버리지 (notional / margin)
            notional = perp.size * current_price
            leverage = notional / max(perp.margin, 1.0)

            # 풀 데이터 소스
            stats_source = getattr(self.engine, '_last_pool_source', 'unknown')

            logger.info(
                f" 범위 여유  : 하단 -{dist_lower:.1f}%  상단 +{dist_upper:.1f}%"
                f"  |  리셋 {self.engine.range_reset_count}회"
            )
            logger.info(
                f" Perp 레버리지: {leverage:.2f}x  "
                f"(노셔널 ${notional:,.0f} / 마진 ${perp.margin:,.0f})"
            )
            if config.USE_LIVE_POOL_DATA:
                logger.info(f" 풀 데이터  : DeFi Llama 실시간")
            else:
                logger.info(f" 풀 데이터  : config 추정값")

        logger.info("=" * 68)

    # ── 최종 리포트 ──────────────────────────────────────

    def final_report(self, current_price: float):
        logger.info("━" * 68)
        logger.info(" 최종 결과 (봇 종료)")
        logger.info("━" * 68)
        self.report(current_price)

    # ── CSV 저장 ─────────────────────────────────────────

    def _append_csv(self, row: dict):
        write_header = not os.path.exists(self._csv_path)
        with open(self._csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=row.keys())
            if write_header:
                writer.writeheader()
            writer.writerow(row)


# ── 유틸 ─────────────────────────────────────────────────

def _fmt_elapsed(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"
