"""
Byreal 4개 풀 통합 페이퍼 트레이딩 봇.

  SOL/USDC   → Hyperliquid SOL Short 헤지
  XAUt0/USDT → Hyperliquid PAXG Short 헤지 (금 프록시)
  WETH/USDC  → Hyperliquid ETH Short 헤지
  HYPE/USDC  → Hyperliquid HYPE Short 헤지

실행:
    python main_multi.py

종료: Ctrl+C
"""
import asyncio
import logging
import os
import sys
import time

# Windows UTF-8
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-12s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/multi_bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

import config
from pool_config import POOL_CONFIGS
from price_feed import PriceFeed
from multi_runner import MultiPoolRunner


async def run():
    logger.info("=" * 80)
    logger.info(" Byreal 4-Pool 델타 헤지 파밍 봇  [PAPER MODE]")
    logger.info(f" 총 자본: ${sum(c.capital for c in POOL_CONFIGS):,.0f} USDC  "
                f"| 풀 수: {len(POOL_CONFIGS)}")
    for cfg in POOL_CONFIGS:
        logger.info(f"   {cfg.name:<14} ${cfg.capital:,.0f}  range=±{cfg.range_pct}%  "
                    f"hedge={cfg.hedge_asset}")
    logger.info(f" 리밸런싱: {config.REBALANCE_INTERVAL}s  "
                f"| 델타 임계값: {config.DELTA_THRESHOLD*100:.0f}%  "
                f"| 리포트: {config.LOG_INTERVAL}s")
    logger.info("=" * 80)

    price_feed = PriceFeed()
    runner     = MultiPoolRunner(price_feed)

    # ── 초기화 ───────────────────────────────────────────
    logger.info("[BOOT] 풀 초기화 중 (가격·풀 데이터 조회)...")
    await runner.initialize()

    last_log_ts = 0.0

    # ── 메인 루프 ─────────────────────────────────────────
    try:
        while True:
            loop_start = time.time()

            await runner.update_all()

            now = time.time()
            if now - last_log_ts >= config.LOG_INTERVAL:
                runner.report()
                last_log_ts = now

            sleep_sec = max(0.0, config.REBALANCE_INTERVAL - (time.time() - loop_start))
            await asyncio.sleep(sleep_sec)

    except KeyboardInterrupt:
        print()
        logger.info("[STOP] Ctrl+C — 봇 종료 중...")

    except Exception as e:
        logger.error(f"[ERROR] {e}", exc_info=True)

    finally:
        runner.final_report()
        await price_feed.close()
        logger.info("[STOP] 종료. logs/multi_bot.log / logs/pnl_history.csv 확인.")


if __name__ == "__main__":
    asyncio.run(run())
