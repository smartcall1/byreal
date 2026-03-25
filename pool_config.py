"""
4개 타겟 풀 정의.

GeckoTerminal 실측 주소 기반 — 2026-03-25 기준.
XAUt0는 Hyperliquid에 없으므로 PAXG(금 토큰)를 헤지 프록시로 사용.
"""
from dataclasses import dataclass


@dataclass
class PoolConfig:
    name:        str    # 표시명 "SOL/USDC"
    lp_token:    str    # Hyperliquid 기준 가격 조회 키
    hedge_asset: str    # Hyperliquid Perp 티커
    range_pct:   float  # CLMM LP 범위 ±%
    capital:     float  # 배정 자본 (USDC)
    fee_tier:    float  # 수수료율 %
    gt_pool_id:  str    # GeckoTerminal Solana 풀 주소
    leverage:    float = 1.0  # Perps 헤지 레버리지 (자본 효율화)


# ── 4개 풀 설정 ────────────────────────────────────────────
#
# 범위 설정 근거:
#   SOL   → 일 변동성 ~5%,  ±15% ≈ 3일치 버퍼
#   XAUt0 → 일 변동성 ~0.8%, ±8% ≈ 10일치 버퍼 (저변동 = 범위 좁게 = 수수료 극대화)
#   WETH  → 일 변동성 ~4%,  ±20% ≈ 5일치 버퍼
#   HYPE  → 일 변동성 ~8%,  ±25% ≈ 3일치 버퍼 (고변동 = 범위 넓게)

POOL_CONFIGS: list[PoolConfig] = [
    PoolConfig(
        name        = "SOL/USDC",
        lp_token    = "SOL",
        hedge_asset = "SOL",
        range_pct   = 15.0,
        capital     = 2500.0,
        fee_tier    = 0.3,
        gt_pool_id  = "9GTj99g9tbz9U6UYDsX6YeRTgUnkYG6GTnHv3qLa5aXq",
        leverage    = 3.0,   # WS 실시간 감시 → 버퍼 ~27%, 자본효율 극대화
    ),
    PoolConfig(
        name        = "XAUt0/USDT",
        lp_token    = "XAUt0",   # → PAXG 가격 사용 (금 토큰 프록시)
        hedge_asset = "PAXG",    # Hyperliquid PAXG Short으로 금 델타 헤지
        range_pct   = 8.0,
        capital     = 2500.0,
        fee_tier    = 0.3,
        gt_pool_id  = "9KWAAyaYF7nmMWzirnBmVhE1q4YXPHcXjzfi6YreNtDY",  # TVL 큰 풀
        leverage    = 4.0,   # 저변동성 금 → 버퍼 ~20%, WS 즉시 대응으로 안전
    ),
    PoolConfig(
        name        = "WETH/USDC",
        lp_token    = "WETH",    # → ETH 가격 사용
        hedge_asset = "ETH",
        range_pct   = 20.0,
        capital     = 2500.0,
        fee_tier    = 0.3,
        gt_pool_id  = "HGxMfonx2vMRGVpHNvj6JbVM5JUjN8xYFS1UGXMYeaAo",
        leverage    = 3.0,   # WS 실시간 감시 → 버퍼 ~27%, 자본효율 극대화
    ),
    PoolConfig(
        name        = "HYPE/USDC",
        lp_token    = "HYPE",
        hedge_asset = "HYPE",
        range_pct   = 25.0,
        capital     = 2500.0,
        fee_tier    = 0.3,
        gt_pool_id  = "DF5SshvX3XTKcvJNxi384FYvfeog7ih9kb89hGbaQBpA",
        leverage    = 2.0,   # 고변동성 DEX 토큰 → 버퍼 ~40%, WS로 관리
    ),
]

# LP 토큰 → Hyperliquid 가격 조회 키 매핑
ASSET_PRICE_MAP: dict[str, str] = {
    "SOL":   "SOL",
    "WETH":  "ETH",   # Wrapped ETH = ETH 가격
    "ETH":   "ETH",
    "HYPE":  "HYPE",
    "XAUt0": "PAXG",  # Tokenized gold ≈ PAXG
    "BTC":   "BTC",
    "PAXG":  "PAXG",
}
