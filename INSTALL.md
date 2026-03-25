# Byreal 봇 — 설치 및 실행 가이드

---

## 아키텍처 구조

```
[ Byreal DEX (Solana) ]          [ Hyperliquid ]
  SOL/USDC  WETH/USDC              REST API (가격 · 펀딩레이트)
  XAUt0/USDT  HYPE/USDC            WebSocket (실시간 가격 스트림)
        │                                  │
        ▼                                  ▼
  LP 포지션 시뮬레이션  ←────  PaperTradingEngine  ────→  Perp Short 시뮬레이션
  (수수료·IL 계산)                                        (델타 헤지·펀딩 계산)
        │
        ▼
  GeckoTerminal API (풀 TVL · 거래량 실측)
```

### 헷징 거래소: Hyperliquid (페이퍼 모드)

| 항목 | 내용 |
|------|------|
| LP 풀 | Byreal DEX (Solana) — 시뮬레이션 |
| 헷지 Perp | **Hyperliquid** — 가격·펀딩 실측, 포지션은 가상 |
| 가격 소스 | Hyperliquid REST + WebSocket |
| 풀 데이터 | GeckoTerminal API (TVL, 거래량) |

> **왜 Byreal Perps(byreal.io/perps)가 아닌가?**
> Byreal Perps REST/WS API가 미공개 상태임.
> 또한 PAXG(XAUt0 헷지용)가 Hyperliquid에는 상장되어 있음.
> 실거래 전환 시에도 Hyperliquid 헷지가 유동성·투명성 면에서 유리함.

---

## 요구 사항

| 항목 | 버전 |
|------|------|
| Python | 3.11 이상 |
| aiohttp | 3.9.0 이상 |
| websockets | 12.0 이상 |
| python-dotenv | 1.0.0 이상 |

---

## 설치 방법

### A. Termux (Android)

```bash
# 1. 패키지 업데이트 및 Python 설치
pkg update && pkg upgrade -y
pkg install python -y

# 2. pip 업그레이드
pip install --upgrade pip

# 3. 봇 코드 클론
pkg install git -y
git clone <repo-url> byreal
cd byreal

# 4. 의존성 설치
pip install -r requirements.txt

# 5. (중요) 백그라운드 실행 시 Android 절전 차단
termux-wake-lock

# 6. 실행
python main_multi.py
```

> **Termux 주의사항**
> - `termux-wake-lock` 없이 화면을 끄면 프로세스가 죽을 수 있음
> - 장시간 실행은 `nohup` 또는 `tmux` 사용 권장:
>   ```bash
>   pkg install tmux -y
>   tmux new -s byreal
>   python main_multi.py
>   # Ctrl+B, D  → 세션 유지하며 분리
>   # tmux attach -t byreal  → 재접속
>   ```
> - WebSocket(websockets 라이브러리)은 Termux에서 완전 호환됨

---

### B. VPS / Linux 서버

```bash
# 1. Python 3.11+ 확인
python3 --version

# 2. 가상환경 생성 (권장)
python3 -m venv venv
source venv/bin/activate

# 3. 의존성 설치
pip install -r requirements.txt

# 4. 백그라운드 실행 (nohup)
nohup python main_multi.py > logs/bot.log 2>&1 &

# 또는 tmux
tmux new -s byreal
python main_multi.py
```

---

### C. Windows (로컬)

```powershell
# 1. 의존성 설치
pip install -r requirements.txt

# 2. UTF-8 환경 설정 (PowerShell)
$env:PYTHONIOENCODING = "utf-8"

# 3. 실행
python main_multi.py
```

---

## 환경 변수 (.env)

프로젝트 루트에 `.env` 파일 생성 (선택 사항):

```env
# 자본 설정 (풀당 기본 $250)
INITIAL_CAPITAL=1000

# 업데이트 주기
REBALANCE_INTERVAL=300    # 초 (기본 5분)
LOG_INTERVAL=60           # 초 (기본 1분)
DELTA_THRESHOLD=0.05      # 델타 편차 5% 이상 시 리밸런싱

# 청산 버퍼
LIQUIDATION_BUFFER_WARN=0.15       # 15% 이하 경고
LIQUIDATION_BUFFER_EMERGENCY=0.08  # 8% 이하 긴급 마진 이전

# 풀 데이터
USE_LIVE_POOL_DATA=true
POOL_STATS_REFRESH_INTERVAL=300
```

---

## 실행 후 대시보드 예시

```
================================================================================
 BYREAL MULTI-POOL PAPER  |  2026-03-25 14:30:00  |  경과: 1h 23m  |  #16
================================================================================
 POOL           Pool TVL    자본       LP     수수료     Perp    순P&L    APR   LiqBuf  상태
 ──────────────────────────────────────────────────────────────────────────────
 SOL/USDC         $320k   $250  $214.23  +$0.0142  +$0.0231  +$0.0373  +18.2%  28.8%  ✓ IN
 XAUt0/USDT        $48k   $250  $222.18  +$0.0031  +$0.0089  +$0.0120   +5.9%  20.8%  ✓ IN
 WETH/USDC        $155k   $250  $214.23  +$0.0098  +$0.0187  +$0.0285  +13.9%  29.5%  ✓ IN
 HYPE/USDC         $92k   $250  $200.00  +$0.0201  +$0.0312  +$0.0513  +25.1%  45.4%  ✓ IN
 ════════════════════════════════════════════════════════════════════════════════
 TOTAL                   $1000                  +$0.0472  +$0.0819  +$0.1291  +15.8%         4/4 ✓
================================================================================
```

**LiqBuf 컬럼 의미:**
- `28.8%` — 현재가에서 28.8% 상승 시 청산 (정상)
- `*15.0%` — 15% 미만, 경고 상태
- `!7.5%`  — 8% 미만, 긴급 마진 자동 이전 중

---

## 로그 파일

| 파일 | 내용 |
|------|------|
| `logs/multi_bot.log` | 전체 실행 로그 |
| `logs/pnl_history.csv` | 시간별 P&L 기록 |
