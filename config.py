import os
from dotenv import load_dotenv

load_dotenv()

# ── 모드 ────────────────────────────────────────────────
PAPER_TRADING = True  # False 로 바꾸면 실거래 모드 (미구현)

# ── 자본 ────────────────────────────────────────────────
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "1000"))  # USDC

# ── 단일 풀 설정 (main.py 전용 — main_multi.py는 pool_config.py 사용) ──────
LP_TOKEN          = os.getenv("LP_TOKEN", "ETH")
LP_FEE_TIER       = float(os.getenv("LP_FEE_TIER", "0.3"))   # 수수료율 %
LP_RANGE_PCT      = float(os.getenv("LP_RANGE_PCT", "20"))    # 진입가 기준 ±%
LP_CAPITAL_RATIO  = float(os.getenv("LP_CAPITAL_RATIO", "0.7"))  # 총 자본 중 LP 비율

# ── Perps 설정 (Hyperliquid) ─────────────────────────────
PERP_TAKER_FEE    = 0.00045   # 0.045% taker
PERP_MAKER_FEE    = -0.00015  # -0.015% maker rebate (사용 안 함, 페이퍼에서 taker 가정)

# ── 리밸런싱 ─────────────────────────────────────────────
REBALANCE_INTERVAL = int(os.getenv("REBALANCE_INTERVAL", "300"))   # 초
DELTA_THRESHOLD    = float(os.getenv("DELTA_THRESHOLD", "0.05"))   # 델타 편차 5% 이상이면 리밸런싱

# ── 풀 볼륨 추정 (페이퍼 수수료 시뮬레이션용) ──────────────
# Byreal 전체 일 거래량 ~$25M 중 ETH/USDC 풀 약 30% 가정
ESTIMATED_POOL_DAILY_VOLUME = float(os.getenv("ESTIMATED_POOL_DAILY_VOLUME", "7_500_000"))
ESTIMATED_POOL_TVL          = float(os.getenv("ESTIMATED_POOL_TVL", "3_000_000"))
LP_TREASURY_CUT             = 0.12  # Byreal treasury 12%, LP 88% 귀속

# ── 실시간 풀 데이터 (DeFi Llama) ───────────────────────
USE_LIVE_POOL_DATA          = os.getenv("USE_LIVE_POOL_DATA", "true").lower() == "true"
# Byreal 전체 TVL·수수료 중 ETH/USDC 풀 비중 (DeFi Llama 값에 적용)
POOL_ETH_USDC_SHARE         = float(os.getenv("POOL_ETH_USDC_SHARE", "0.30"))
POOL_STATS_REFRESH_INTERVAL = int(os.getenv("POOL_STATS_REFRESH_INTERVAL", "300"))  # 초

# ── LP 범위 리셋 ─────────────────────────────────────────
RANGE_RESET_ENABLED    = os.getenv("RANGE_RESET_ENABLED", "true").lower() == "true"
LP_RESET_SLIPPAGE_PCT  = float(os.getenv("LP_RESET_SLIPPAGE_PCT",  "0.50"))  # 출금+입금 각 0.50% (보수적: CLMM 좁은 범위 실제 슬리피지)
LP_RESET_SWAP_PCT      = float(os.getenv("LP_RESET_SWAP_PCT",      "0.15"))  # 재진입 시 토큰 재배분 스왑 비용 (fee_tier × 0.5)
LP_ENTRY_SLIPPAGE_PCT  = float(os.getenv("LP_ENTRY_SLIPPAGE_PCT",  "0.20"))  # 초기 LP 진입 슬리피지 (보수적)
SOLANA_TX_COST_USDC    = float(os.getenv("SOLANA_TX_COST_USDC",    "0.003")) # Solana 가스비 추정

# ── 청산 버퍼 관리 ───────────────────────────────────────
# Short 포지션 청산가까지의 여유 비율
LIQUIDATION_BUFFER_WARN      = float(os.getenv("LIQUIDATION_BUFFER_WARN",      "0.15"))  # 15% 이하 → 경고
LIQUIDATION_BUFFER_EMERGENCY = float(os.getenv("LIQUIDATION_BUFFER_EMERGENCY", "0.08"))  # 8%  이하 → 긴급 마진 추가
MARGIN_TOPUP_RATIO           = float(os.getenv("MARGIN_TOPUP_RATIO",           "0.05"))  # 긴급 시 LP 가치의 5% 마진 이전

# ── 리포팅 ───────────────────────────────────────────────
LOG_INTERVAL = int(os.getenv("LOG_INTERVAL", "60"))  # 콘솔 출력 주기 (초)

# ── 텔레그램 알림 (선택) ─────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
