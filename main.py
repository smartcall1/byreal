"""
Byreal 델타 헤지 파밍 봇 — 진입점

실행:
    python main.py

동작 요약:
    1. Hyperliquid API에서 실시간 ETH 가격 조회
    2. CLMM LP 포지션 시뮬레이션 (±20% 범위)
    3. LP 델타만큼 Perps Short 헤지 (방향 무관 수수료 수익 목표)
    4. 매 REBALANCE_INTERVAL마다 델타 체크 → 필요시 Short 조정
    5. 매 LOG_INTERVAL마다 손익 리포트 출력 + CSV 저장
"""
import asyncio
import logging
import os
import sys
import time

# Windows 콘솔 UTF-8 출력 강제 설정
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

import config
from paper_engine import PaperTradingEngine
from pnl_tracker import PnLTracker
from price_feed import PriceFeed

# ── 로깅 설정 ─────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ── 메인 ─────────────────────────────────────────────────

async def run():
    logger.info("=" * 68)
    logger.info(" Byreal 델타 헤지 파밍 봇  [PAPER MODE]")
    logger.info(f" 초기 자본    : ${config.INITIAL_CAPITAL:,.0f} USDC")
    logger.info(f" LP           : {config.LP_TOKEN}/USDC  |  Fee {config.LP_FEE_TIER}%  |  ±{config.LP_RANGE_PCT}% 범위")
    logger.info(f" 리밸런싱 주기: {config.REBALANCE_INTERVAL}s  |  델타 임계값: {config.DELTA_THRESHOLD*100:.0f}%")
    logger.info(f" 리포트 주기  : {config.LOG_INTERVAL}s")
    logger.info("=" * 68)

    price_feed = PriceFeed()
    engine     = PaperTradingEngine(price_feed)
    tracker    = PnLTracker(engine)

    # ── 초기 가격 조회 + 포지션 오픈 ─────────────────────
    logger.info("[BOOT] Hyperliquid에서 현재가 조회 중...")
    initial_price = await price_feed.get_price(config.LP_TOKEN)
    funding_0     = await price_feed.get_funding_rate(config.LP_TOKEN)
    logger.info(f"[BOOT] {config.LP_TOKEN} 현재가: ${initial_price:,.2f}  |  펀딩레이트(1h): {funding_0*100:.4f}%")

    await engine.initialize(initial_price)

    last_log_ts = 0.0

    # ── 메인 루프 ─────────────────────────────────────────
    try:
        while True:
            loop_start = time.time()

            # 가격 + 펀딩레이트 조회 (매 인터벌)
            current_price = await price_feed.get_price(config.LP_TOKEN)
            funding_rate  = await price_feed.get_funding_rate(config.LP_TOKEN)

            # 풀 통계 조회 (내부 캐시로 5분마다 갱신)
            pool_stats = None
            if config.USE_LIVE_POOL_DATA:
                pool_stats = await price_feed.get_byreal_stats()

            # 수수료·펀딩 누적 + 범위 리셋 + 델타 리밸런싱
            await engine.update(current_price, funding_rate, pool_stats)

            # 정기 리포트
            now = time.time()
            if now - last_log_ts >= config.LOG_INTERVAL:
                tracker.report(current_price)
                last_log_ts = now

            # 다음 인터벌까지 대기 (API 호출 시간 제외)
            elapsed = time.time() - loop_start
            sleep_sec = max(0.0, config.REBALANCE_INTERVAL - elapsed)
            await asyncio.sleep(sleep_sec)

    except KeyboardInterrupt:
        logger.info("")
        logger.info("[STOP] Ctrl+C 감지 — 봇 종료 중...")

    except Exception as e:
        logger.error(f"[ERROR] 예상치 못한 오류: {e}", exc_info=True)

    finally:
        try:
            last_price = await price_feed.get_price(config.LP_TOKEN)
        except Exception:
            last_price = 0.0
        tracker.final_report(last_price)
        await price_feed.close()
        logger.info("[STOP] 종료 완료. logs/pnl_history.csv 확인 바랍니다.")


if __name__ == "__main__":
    asyncio.run(run())
