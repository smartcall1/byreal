"""
Uniswap V3 스타일 CLMM(Concentrated Liquidity Market Maker) 수학 계산.

핵심 공식:
  - 유동성 L이 주어졌을 때 [Pa, Pb] 범위 내 가격 P에서의 토큰 보유량
    x (ETH)  = L × (1/√P  - 1/√Pb)
    y (USDC) = L × (√P - √Pa)
  - 델타(ETH 노출) = x amount
"""
import math


# ── 기본 계산 ────────────────────────────────────────────

def sqrt(p: float) -> float:
    return math.sqrt(p)


def calc_liquidity_from_deposit(
    total_value_usdc: float,
    price: float,
    price_lower: float,
    price_upper: float,
) -> float:
    """총 자본(USDC)과 가격 범위로 유동성 L을 계산.

    가격이 범위 안에 있을 때 L=1 당 자산 가치를 구하고,
    총 자본으로 스케일링한다.
    """
    if price <= price_lower:
        # 범위 아래 — 전부 ETH
        sp_a, sp_b = sqrt(price_lower), sqrt(price_upper)
        eth_per_L = 1 / sp_a - 1 / sp_b
        return total_value_usdc / (eth_per_L * price)

    if price >= price_upper:
        # 범위 위 — 전부 USDC
        sp_a, sp_b = sqrt(price_lower), sqrt(price_upper)
        usdc_per_L = sp_b - sp_a
        return total_value_usdc / usdc_per_L

    sp, sp_a, sp_b = sqrt(price), sqrt(price_lower), sqrt(price_upper)
    eth_per_L  = (sp_b - sp) / (sp * sp_b)   # = 1/√P - 1/√Pb
    usdc_per_L = sp - sp_a
    value_per_L = eth_per_L * price + usdc_per_L
    return total_value_usdc / value_per_L


def get_amounts(
    L: float,
    price: float,
    price_lower: float,
    price_upper: float,
) -> tuple[float, float]:
    """현재 가격에서 LP 포지션의 (ETH, USDC) 보유량 반환."""
    sp_a, sp_b = sqrt(price_lower), sqrt(price_upper)

    if price <= price_lower:
        eth  = L * (1 / sp_a - 1 / sp_b)
        usdc = 0.0
    elif price >= price_upper:
        eth  = 0.0
        usdc = L * (sp_b - sp_a)
    else:
        sp   = sqrt(price)
        eth  = L * (1 / sp - 1 / sp_b)
        usdc = L * (sp - sp_a)

    return eth, usdc


def get_position_value(
    L: float,
    price: float,
    price_lower: float,
    price_upper: float,
) -> float:
    """포지션 총 가치 (USDC 환산)."""
    eth, usdc = get_amounts(L, price, price_lower, price_upper)
    return eth * price + usdc


def get_delta(
    L: float,
    price: float,
    price_lower: float,
    price_upper: float,
) -> float:
    """델타 = 현재 ETH 노출량 (헤지해야 할 Short 규모)."""
    eth, _ = get_amounts(L, price, price_lower, price_upper)
    return eth


def is_in_range(price: float, price_lower: float, price_upper: float) -> bool:
    return price_lower <= price <= price_upper


# ── 손익 계산 ────────────────────────────────────────────

def calc_il(
    L: float,
    current_price: float,
    entry_price: float,
    price_lower: float,
    price_upper: float,
) -> float:
    """비영구적 손실(IL).

    IL = 현재 LP 가치 - HODL 가치 (음수 = 손실)
    HODL: 진입 시점의 ETH/USDC 그대로 보유했을 때의 가치
    """
    eth_entry, usdc_entry = get_amounts(L, entry_price, price_lower, price_upper)
    hodl_value = eth_entry * current_price + usdc_entry
    lp_value   = get_position_value(L, current_price, price_lower, price_upper)
    return lp_value - hodl_value


def estimate_fee_apy(
    fee_tier_pct: float,
    pool_daily_volume: float,
    pool_tvl: float,
    lp_treasury_cut: float = 0.12,
) -> float:
    """연율화 수수료 APY 추정.

    공식: (pool_daily_volume × fee_tier) × (1 - treasury_cut) / pool_tvl × 365
    """
    lp_share = 1.0 - lp_treasury_cut
    daily_yield = (fee_tier_pct / 100) * pool_daily_volume * lp_share / pool_tvl
    return daily_yield * 365


def estimate_fee_for_interval(
    position_value: float,
    interval_seconds: float,
    fee_tier_pct: float,
    pool_daily_volume: float,
    pool_tvl: float,
    lp_treasury_cut: float = 0.12,
) -> float:
    """특정 시간 구간 동안 내 포지션이 받을 수수료 추정.

    가정: 내 포지션 비중 ≈ position_value / pool_tvl
    """
    if pool_tvl <= 0:
        return 0.0
    lp_share = 1.0 - lp_treasury_cut
    daily_fee_total = (fee_tier_pct / 100) * pool_daily_volume * lp_share
    my_share = position_value / pool_tvl
    interval_days = interval_seconds / 86_400
    return daily_fee_total * my_share * interval_days
