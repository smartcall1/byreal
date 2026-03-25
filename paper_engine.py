"""
페이퍼 트레이딩 엔진.

LP 포지션(CLMM)과 Perps Short 포지션을 시뮬레이션하며,
매 업데이트마다 델타를 계산하고 필요시 헤지 규모를 자동 조정한다.

자본 배분:
  LP_CAPITAL_RATIO(기본 70%)  → CLMM 풀에 예치
  나머지 30%                  → Perps Short 마진 (증거금)
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import clmm_math
import config

logger = logging.getLogger(__name__)


# ── 데이터 클래스 ─────────────────────────────────────────

@dataclass
class LPPosition:
    entry_price: float
    price_lower: float
    price_upper: float
    liquidity: float
    initial_eth: float       # 진입 시 ETH 보유량
    initial_usdc: float      # 진입 시 USDC 보유량
    initial_value: float     # 진입 시 총 가치 (USDC)
    fees_accrued: float = 0.0
    in_range: bool = True
    opened_at: float = field(default_factory=time.time)
    last_reset_price: float = 0.0  # 범위 리셋 시 기준가


@dataclass
class PerpPosition:
    asset: str
    size: float          # ETH 수량 (Short, 현재 오픈 잔량)
    entry_price: float   # 오픈 포지션 평균 진입가
    margin: float        # 증거금 (USDC)
    funding_received: float = 0.0   # 수령한 펀딩 누적
    realized_pnl: float = 0.0       # 리밸런싱 시 실현된 PnL 누적
    unrealized_pnl: float = 0.0     # 현재 오픈 포지션의 미실현 PnL
    opened_at: float = field(default_factory=time.time)

    @property
    def pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl


@dataclass
class RebalanceRecord:
    timestamp: float
    old_size: float
    new_size: float
    price: float
    cost: float


# ── 메인 엔진 ─────────────────────────────────────────────

class PaperTradingEngine:
    def __init__(self, price_feed):
        self.price_feed = price_feed
        self.lp:   Optional[LPPosition]  = None
        self.perp: Optional[PerpPosition] = None

        self.initial_capital   = config.INITIAL_CAPITAL
        self.rebalance_count   = 0
        self.rebalance_costs   = 0.0
        self.rebalance_history: list[RebalanceRecord] = []
        self._last_rebalance_ts = 0.0

    # ── 초기화 ──────────────────────────────────────────

    async def initialize(self, initial_price: float):
        lp_capital  = self.initial_capital * config.LP_CAPITAL_RATIO
        perp_margin = self.initial_capital * (1 - config.LP_CAPITAL_RATIO)

        price_lower = initial_price * (1 - config.LP_RANGE_PCT / 100)
        price_upper = initial_price * (1 + config.LP_RANGE_PCT / 100)

        L = clmm_math.calc_liquidity_from_deposit(
            lp_capital, initial_price, price_lower, price_upper
        )
        eth, usdc = clmm_math.get_amounts(L, initial_price, price_lower, price_upper)

        self.lp = LPPosition(
            entry_price=initial_price,
            price_lower=price_lower,
            price_upper=price_upper,
            liquidity=L,
            initial_eth=eth,
            initial_usdc=usdc,
            initial_value=lp_capital,
            last_reset_price=initial_price,
        )

        # 초기 델타만큼 Short 진입
        initial_delta = clmm_math.get_delta(L, initial_price, price_lower, price_upper)

        self.perp = PerpPosition(
            asset=config.LP_TOKEN,
            size=initial_delta,
            entry_price=initial_price,
            margin=perp_margin,
        )

        logger.info("[INIT] ─────────────────────────────────────────")
        logger.info(f"[INIT] LP 자본   : ${lp_capital:,.2f} USDC")
        logger.info(f"[INIT] 범위      : ${price_lower:,.2f} ~ ${price_upper:,.2f}")
        logger.info(f"[INIT] ETH 보유  : {eth:.6f} ETH  |  USDC 보유: {usdc:.2f}")
        logger.info(f"[INIT] 초기 델타 : {initial_delta:.6f} ETH → Short 진입")
        logger.info(f"[INIT] Perp 마진 : ${perp_margin:,.2f} USDC")
        logger.info("[INIT] ─────────────────────────────────────────")

    # ── 메인 업데이트 루프 ───────────────────────────────

    async def update(self, current_price: float, funding_rate_1h: float) -> bool:
        """매 인터벌 호출. 수수료·펀딩 누적 → 델타 체크 → 리밸런싱 여부 반환."""
        if not self.lp or not self.perp:
            return False

        interval_sec = config.REBALANCE_INTERVAL
        in_range = clmm_math.is_in_range(
            current_price, self.lp.price_lower, self.lp.price_upper
        )
        self.lp.in_range = in_range

        # 1. LP 수수료 누적 (범위 안에 있을 때만)
        if in_range:
            lp_value = clmm_math.get_position_value(
                self.lp.liquidity, current_price,
                self.lp.price_lower, self.lp.price_upper
            )
            fee = clmm_math.estimate_fee_for_interval(
                position_value=lp_value,
                interval_seconds=interval_sec,
                fee_tier_pct=config.LP_FEE_TIER,
                pool_daily_volume=config.ESTIMATED_POOL_DAILY_VOLUME,
                pool_tvl=config.ESTIMATED_POOL_TVL,
                lp_treasury_cut=config.LP_TREASURY_CUT,
            )
            self.lp.fees_accrued += fee
        else:
            logger.warning(
                f"[OUT OF RANGE] ${current_price:,.2f}  "
                f"범위: ${self.lp.price_lower:,.0f} ~ ${self.lp.price_upper:,.0f}  "
                f"수수료 0"
            )

        # 2. Perp 펀딩 누적 (Short 기준: 양수면 수령)
        perp_notional = self.perp.size * current_price
        interval_hours = interval_sec / 3600
        funding_earned = perp_notional * funding_rate_1h * interval_hours
        self.perp.funding_received += funding_earned

        # 3. 델타 리밸런싱 (사이즈 변경 후 unrealized 재계산해야 이중계산 없음)
        rebalanced = self._maybe_rebalance(current_price)

        # 4. Perp 미실현 PnL 갱신 (리밸런싱 이후 최신 size/entry_price 기준)
        self.perp.unrealized_pnl = (self.perp.entry_price - current_price) * self.perp.size

        return rebalanced

    # ── 델타 리밸런싱 ────────────────────────────────────

    def _maybe_rebalance(self, current_price: float) -> bool:
        if not self.lp or not self.perp:
            return False

        current_delta = clmm_math.get_delta(
            self.lp.liquidity, current_price,
            self.lp.price_lower, self.lp.price_upper
        )
        hedge_size = self.perp.size
        diff = abs(current_delta - hedge_size)

        # 분모 0 방지
        diff_pct = diff / max(hedge_size, 1e-8)

        now = time.time()
        cooldown_ok = (now - self._last_rebalance_ts) > 60  # 최소 1분 간격

        if diff_pct < config.DELTA_THRESHOLD or not cooldown_ok:
            return False

        # 리밸런싱 실행
        trade_value = diff * current_price
        cost = trade_value * config.PERP_TAKER_FEE

        old_size = self.perp.size

        if current_delta < old_size:
            # Short 축소: 일부 커버 → 해당분 PnL 실현, entry_price 유지
            covered = old_size - current_delta
            self.perp.realized_pnl += (self.perp.entry_price - current_price) * covered
        else:
            # Short 확대: 추가 진입 → 가중평균 entry_price 갱신
            added = current_delta - old_size
            self.perp.entry_price = (
                self.perp.entry_price * old_size + current_price * added
            ) / current_delta

        self.perp.size = current_delta
        self.rebalance_costs += cost
        self._last_rebalance_ts = now
        self.rebalance_count += 1

        rec = RebalanceRecord(
            timestamp=now,
            old_size=old_size,
            new_size=current_delta,
            price=current_price,
            cost=cost,
        )
        self.rebalance_history.append(rec)

        direction = "축소" if current_delta < old_size else "확대"
        logger.info(
            f"[REBAL #{self.rebalance_count}] "
            f"Short {direction}: {old_size:.6f} → {current_delta:.6f} ETH  |  "
            f"비용: ${cost:.4f}  |  가격: ${current_price:,.2f}"
        )
        return True

    # ── 조회 헬퍼 ────────────────────────────────────────

    def get_lp_value(self, current_price: float) -> float:
        if not self.lp:
            return 0.0
        return clmm_math.get_position_value(
            self.lp.liquidity, current_price,
            self.lp.price_lower, self.lp.price_upper
        )

    def get_il(self, current_price: float) -> float:
        """비영구적 손실 (음수 = 손실)."""
        if not self.lp:
            return 0.0
        return clmm_math.calc_il(
            self.lp.liquidity, current_price,
            self.lp.entry_price,
            self.lp.price_lower, self.lp.price_upper,
        )

    def get_net_pnl(self, current_price: float) -> float:
        """총 순손익 = LP 가치 변화 + Perp PnL + 수수료 + 펀딩 - 비용

        LP 가치 변화 + Perp PnL ≈ IL (방향 노출 상쇄)
        → 순손익 ≈ IL + 수수료 + 펀딩 (수수료가 IL을 초과하면 수익)
        """
        lp_gain = self.get_lp_value(current_price) - (self.lp.initial_value if self.lp else 0.0)
        fees    = self.lp.fees_accrued if self.lp else 0.0
        p_pnl   = self.perp.pnl if self.perp else 0.0
        funding = self.perp.funding_received if self.perp else 0.0
        costs   = self.rebalance_costs
        return lp_gain + fees + p_pnl + funding - costs

    def get_current_delta(self, current_price: float) -> float:
        if not self.lp:
            return 0.0
        return clmm_math.get_delta(
            self.lp.liquidity, current_price,
            self.lp.price_lower, self.lp.price_upper
        )
