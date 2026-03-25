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
from pool_config import PoolConfig

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
    leverage: float = 1.0           # 레버리지 배수
    funding_received: float = 0.0   # 수령한 펀딩 누적
    realized_pnl: float = 0.0       # 리밸런싱 시 실현된 PnL 누적
    unrealized_pnl: float = 0.0     # 현재 오픈 포지션의 미실현 PnL
    opened_at: float = field(default_factory=time.time)

    @property
    def pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def liquidation_price(self) -> float:
        """Short 청산가. 마진 80% 소진 시점 (Hyperliquid 기준).

        Short PnL = (entry - current) × size
        손실 = (current - entry) × size = 0.8 × margin
        → liq_price = entry + 0.8 × margin / size
        """
        if self.size <= 0:
            return float("inf")
        return self.entry_price + (0.8 * self.margin) / self.size


@dataclass
class RebalanceRecord:
    timestamp: float
    old_size: float
    new_size: float
    price: float
    cost: float


@dataclass
class RangeResetRecord:
    timestamp: float
    old_lower: float
    old_upper: float
    new_lower: float
    new_upper: float
    price: float
    lp_value_before: float
    slippage_cost: float
    tx_cost: float
    perp_cost: float


# ── 메인 엔진 ─────────────────────────────────────────────

class PaperTradingEngine:
    def __init__(self, price_feed, pool_cfg: PoolConfig | None = None):
        self.price_feed = price_feed
        self.pool_cfg   = pool_cfg  # None이면 global config 사용 (단일 풀 모드)
        self.lp:   Optional[LPPosition]  = None
        self.perp: Optional[PerpPosition] = None

        self.initial_capital    = pool_cfg.capital if pool_cfg else config.INITIAL_CAPITAL
        self._original_lp_capital: float = 0.0  # 최초 LP 투입 자본, 불변

        # 리밸런싱 (Perp Short 조정)
        self.rebalance_count    = 0
        self.rebalance_costs    = 0.0
        self.rebalance_history: list[RebalanceRecord] = []
        self._last_rebalance_ts = 0.0

        # 범위 리셋 (LP 재진입)
        self.range_reset_count  = 0
        self.range_reset_history: list[RangeResetRecord] = []

        # 청산 경고 쿨다운 (WS 1초 호출 시 로그 스팸 방지)
        self._liq_warn_ts: float = 0.0
        self._liq_emergency_ts: float = 0.0

    # ── 초기화 ──────────────────────────────────────────

    async def initialize(self, initial_price: float):
        # 레버리지 기반 자본 배분
        # CLMM 중점 기준: LP delta notional ≈ lp_capital × 0.5
        # 필요 마진 = notional / leverage = lp_capital × 0.5 / leverage
        # lp_capital + lp_capital × 0.5 / leverage = total
        # → lp_capital = total / (1 + 0.5 / leverage)
        leverage    = self.pool_cfg.leverage if self.pool_cfg else 1.0
        lp_capital  = self.initial_capital / (1 + 0.5 / leverage)
        perp_margin = self.initial_capital - lp_capital
        self._original_lp_capital = lp_capital

        range_pct   = self.pool_cfg.range_pct if self.pool_cfg else config.LP_RANGE_PCT
        price_lower = initial_price * (1 - range_pct / 100)
        price_upper = initial_price * (1 + range_pct / 100)

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
            asset=self.pool_cfg.hedge_asset if self.pool_cfg else config.LP_TOKEN,
            size=initial_delta,
            entry_price=initial_price,
            margin=perp_margin,
            leverage=leverage,
        )

        liq_price = self.perp.liquidation_price
        liq_buf   = (liq_price - initial_price) / initial_price * 100
        logger.info("[INIT] ─────────────────────────────────────────")
        logger.info(f"[INIT] LP 자본   : ${lp_capital:,.2f} USDC  (레버리지 {leverage}x)")
        logger.info(f"[INIT] 범위      : ${price_lower:,.2f} ~ ${price_upper:,.2f}")
        logger.info(f"[INIT] ETH 보유  : {eth:.6f} ETH  |  USDC 보유: {usdc:.2f}")
        logger.info(f"[INIT] 초기 델타 : {initial_delta:.6f} ETH → Short 진입")
        logger.info(f"[INIT] Perp 마진 : ${perp_margin:,.2f} USDC  |  청산가: ${liq_price:,.2f}  (버퍼 {liq_buf:.1f}%)")
        logger.info("[INIT] ─────────────────────────────────────────")

    # ── 메인 업데이트 루프 ───────────────────────────────

    async def update(
        self,
        current_price: float,
        funding_rate_1h: float,
        pool_stats: dict | None = None,
    ) -> bool:
        """매 인터벌 호출. 수수료·펀딩 누적 → 범위 리셋 → 델타 리밸런싱."""
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
            fee = self._calc_fee_for_interval(lp_value, interval_sec, pool_stats)
            self.lp.fees_accrued += fee
        else:
            # 범위 이탈 — 리셋 여부 판단
            logger.warning(
                f"[OUT OF RANGE] ${current_price:,.2f}  "
                f"범위: ${self.lp.price_lower:,.0f} ~ ${self.lp.price_upper:,.0f}"
            )
            if config.RANGE_RESET_ENABLED:
                await self._reset_range(current_price)

        # 2. Perp 펀딩 누적 (Short 기준: 양수면 수령)
        perp_notional = self.perp.size * current_price
        interval_hours = interval_sec / 3600
        funding_earned = perp_notional * funding_rate_1h * interval_hours
        self.perp.funding_received += funding_earned

        # 3. 델타 리밸런싱 (사이즈 변경 후 unrealized 재계산해야 이중계산 없음)
        rebalanced = self._maybe_rebalance(current_price)

        # 4. Perp 미실현 PnL 갱신 (리밸런싱 이후 최신 size/entry_price 기준)
        self.perp.unrealized_pnl = (self.perp.entry_price - current_price) * self.perp.size

        # 5. 청산 버퍼 점검 (레버리지 > 1인 경우)
        if self.perp.leverage > 1.0:
            self._check_liquidation_buffer(current_price)

        return rebalanced

    # ── 수수료 계산 헬퍼 ─────────────────────────────────

    def _calc_fee_for_interval(
        self,
        position_value: float,
        interval_sec: float,
        pool_stats: dict | None,
    ) -> float:
        """인터벌 동안 내 포지션이 받을 수수료 추정.

        pool_stats가 있으면 DeFi Llama 실제 데이터 사용.
        없으면 config 추정값 사용.

        공식: my_fee = (내 LP 가치 / 풀 TVL) × 풀 일 수수료 × (interval / 86400)
        """
        interval_days = interval_sec / 86_400

        if pool_stats and config.USE_LIVE_POOL_DATA:
            src = pool_stats.get("source", "")
            if src == "geckoterminal":
                # 풀별 GT 데이터: 이미 해당 풀의 수치 → share 비율 불필요
                fee_tier = self.pool_cfg.fee_tier if self.pool_cfg else config.LP_FEE_TIER
                pool_tvl        = pool_stats["tvl"]
                pool_daily_fees = pool_stats["vol_24h"] * (fee_tier / 100) * (1 - config.LP_TREASURY_CUT)
            else:
                # 프로토콜 전체 DeFi Llama 데이터 → share 비율 적용
                pool_tvl        = pool_stats["tvl"] * config.POOL_ETH_USDC_SHARE
                pool_daily_fees = pool_stats["daily_lp_fees"] * config.POOL_ETH_USDC_SHARE
        else:
            fee_tier = self.pool_cfg.fee_tier if self.pool_cfg else config.LP_FEE_TIER
            pool_tvl        = config.ESTIMATED_POOL_TVL
            pool_daily_fees = (
                config.ESTIMATED_POOL_DAILY_VOLUME
                * (fee_tier / 100)
                * (1 - config.LP_TREASURY_CUT)
            )

        if pool_tvl <= 0:
            return 0.0

        my_share = position_value / pool_tvl
        return pool_daily_fees * my_share * interval_days

    # ── LP 범위 리셋 ──────────────────────────────────────

    async def _reset_range(self, current_price: float) -> bool:
        """범위 이탈 시 현재가 중심으로 LP 재진입.

        비용:
          - 출금 슬리피지: LP 가치 × LP_RESET_SLIPPAGE_PCT / 100
          - 입금 슬리피지: 재투입 가치 × LP_RESET_SLIPPAGE_PCT / 100
          - Solana 가스비: SOLANA_TX_COST_USDC × 2 (withdraw + deposit)
          - Perp 조정: |새 델타 - 구 델타| × 현재가 × PERP_TAKER_FEE
        """
        if not self.lp or not self.perp:
            return False

        lp_value_before = clmm_math.get_position_value(
            self.lp.liquidity, current_price,
            self.lp.price_lower, self.lp.price_upper,
        )

        # ① 슬리피지 비용 (출금 + 재입금)
        slippage_cost = lp_value_before * (config.LP_RESET_SLIPPAGE_PCT / 100) * 2
        # ② Solana 가스비 (withdraw + deposit = 2 tx)
        tx_cost = config.SOLANA_TX_COST_USDC * 2
        # ③ 재투입 자본
        new_capital = lp_value_before - slippage_cost - tx_cost

        # 새 범위 (현재가 중심)
        range_pct = self.pool_cfg.range_pct if self.pool_cfg else config.LP_RANGE_PCT
        new_lower = current_price * (1 - range_pct / 100)
        new_upper = current_price * (1 + range_pct / 100)

        new_L   = clmm_math.calc_liquidity_from_deposit(new_capital, current_price, new_lower, new_upper)
        new_eth, new_usdc = clmm_math.get_amounts(new_L, current_price, new_lower, new_upper)
        new_delta = clmm_math.get_delta(new_L, current_price, new_lower, new_upper)

        # ④ Perp 조정: 기존 Short 전체 실현 후 새 델타로 재진입
        old_perp_size = self.perp.size
        realized_close = (self.perp.entry_price - current_price) * old_perp_size
        self.perp.realized_pnl += realized_close

        perp_diff = abs(new_delta - old_perp_size)
        perp_cost = perp_diff * current_price * config.PERP_TAKER_FEE
        # 기존 Short 청산 비용 (항상 발생)
        perp_cost += old_perp_size * current_price * config.PERP_TAKER_FEE

        self.perp.size         = new_delta
        self.perp.entry_price  = current_price
        self.perp.unrealized_pnl = 0.0

        # 범위 리셋 이력 기록
        rec = RangeResetRecord(
            timestamp=time.time(),
            old_lower=self.lp.price_lower,
            old_upper=self.lp.price_upper,
            new_lower=new_lower,
            new_upper=new_upper,
            price=current_price,
            lp_value_before=lp_value_before,
            slippage_cost=slippage_cost,
            tx_cost=tx_cost,
            perp_cost=perp_cost,
        )
        self.range_reset_history.append(rec)
        self.range_reset_count += 1

        # LP 포지션 갱신
        self.lp.price_lower   = new_lower
        self.lp.price_upper   = new_upper
        self.lp.liquidity     = new_L
        self.lp.initial_eth   = new_eth
        self.lp.initial_usdc  = new_usdc
        self.lp.initial_value = new_capital   # IL 기준점 리셋
        self.lp.entry_price   = current_price
        self.lp.in_range      = True
        self.lp.last_reset_price = current_price

        total_cost = slippage_cost + tx_cost + perp_cost
        logger.info(
            f"[RANGE RESET #{self.range_reset_count}] "
            f"${current_price:,.2f}  |  "
            f"새 범위: ${new_lower:,.0f} ~ ${new_upper:,.0f}  |  "
            f"재투입: ${new_capital:.2f}  |  "
            f"총비용: ${total_cost:.4f} (슬리피지 ${slippage_cost:.4f} + 가스 ${tx_cost:.4f} + Perp ${perp_cost:.4f})"
        )
        return True

    # ── 청산 버퍼 관리 ───────────────────────────────────

    def _check_liquidation_buffer(self, current_price: float):
        """청산가까지의 버퍼를 점검하고, 위험 시 마진을 자동 보충한다.

        WS에서 1초마다 호출되므로 쿨다운으로 로그 스팸을 방지한다.
        """
        if not self.perp or self.perp.size <= 0:
            return

        liq_price = self.perp.liquidation_price
        buffer    = (liq_price - current_price) / current_price
        now       = time.time()

        if buffer < 0:
            if now - self._liq_emergency_ts > 5.0:
                logger.critical(
                    f"[LIQ] LIQUIDATED  현재가 ${current_price:,.2f} > 청산가 ${liq_price:,.2f}"
                )
                self._liq_emergency_ts = now
            return

        if buffer < config.LIQUIDATION_BUFFER_EMERGENCY:
            if now - self._liq_emergency_ts > 10.0:
                logger.critical(
                    f"[LIQ] 긴급 마진 추가!  버퍼 {buffer*100:.1f}%  "
                    f"현재가 ${current_price:,.2f}  청산가 ${liq_price:,.2f}"
                )
                self._liq_emergency_ts = now
            self._emergency_topup(current_price)
        elif buffer < config.LIQUIDATION_BUFFER_WARN:
            if now - self._liq_warn_ts > 30.0:
                logger.warning(
                    f"[LIQ] 청산 접근  버퍼 {buffer*100:.1f}%  "
                    f"현재가 ${current_price:,.2f}  청산가 ${liq_price:,.2f}"
                )
                self._liq_warn_ts = now

    def _emergency_topup(self, current_price: float):
        """LP 가치 일부를 Perp 마진으로 이전해 청산을 방어한다.

        LP 유동성을 비례 감소시키고 _original_lp_capital도 동일하게 낮춰
        lp_gain 기준이 흐트러지지 않게 한다. 슬리피지만 비용으로 기록.
        """
        if not self.lp or not self.perp:
            return

        lp_value = self.get_lp_value(current_price)
        topup    = lp_value * config.MARGIN_TOPUP_RATIO
        slip     = topup * (config.LP_RESET_SLIPPAGE_PCT / 100)
        net      = topup - slip

        # LP 유동성 비례 축소
        ratio = (lp_value - topup) / max(lp_value, 1e-8)
        self.lp.liquidity        *= ratio
        self.lp.initial_value    *= ratio
        self._original_lp_capital = max(0.0, self._original_lp_capital - topup)

        # 마진 보충
        self.perp.margin += net
        self.rebalance_costs += slip

        new_liq = self.perp.liquidation_price
        logger.info(
            f"[TOPUP] LP → 마진 이전  +${net:.2f}  (슬리피지 ${slip:.4f})  "
            f"새 마진 ${self.perp.margin:.2f}  새 청산가 ${new_liq:,.2f}"
        )

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
        """총 순손익 = 현재 포트폴리오 가치 - 최초 투입 자본

        구성:
          LP 가치 변화   (vs 최초 LP 자본, 범위 리셋 비용 이미 반영)
        + Perp PnL      (≈ -HODL_gain → LP 방향 노출 상쇄)
        + 수수료 수익    ← 핵심 수익
        + 펀딩 수령
        - Perp 리밸런싱 비용
        ──────────────────────────────────────────
        ≈ IL + 수수료 + 펀딩 (범위 리셋 비용은 LP 가치에 이미 차감)
        """
        lp_gain = self.get_lp_value(current_price) - self._original_lp_capital
        fees    = self.lp.fees_accrued if self.lp else 0.0
        p_pnl   = self.perp.pnl if self.perp else 0.0
        funding = self.perp.funding_received if self.perp else 0.0
        costs   = self.rebalance_costs  # 범위 리셋 비용은 LP 가치에 이미 반영
        return lp_gain + fees + p_pnl + funding - costs

    def get_current_delta(self, current_price: float) -> float:
        if not self.lp:
            return 0.0
        return clmm_math.get_delta(
            self.lp.liquidity, current_price,
            self.lp.price_lower, self.lp.price_upper
        )

    def get_liquidation_buffer(self, current_price: float) -> float:
        """청산가까지 남은 여유 비율 (0.15 = 15%). 음수면 청산 상태."""
        if not self.perp or self.perp.size <= 0:
            return 1.0
        return (self.perp.liquidation_price - current_price) / current_price
