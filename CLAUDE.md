# Reflexivity Trading System

Binance Web3 Wallet의 Trending 페이지를 기반으로 소로스 재귀성 이론을 적용한 멀티체인 암호화폐 트레이딩 시스템.

## 프로젝트 개요

- **목표**: BSC + Base + Solana 3개 체인에서 Divergence 기반 진입/청산 신호 생성
- **전략**: Quarter Kelly sizing + -20%스탑/트레일링15% + 체인별 파라미터 차등
- **현재 단계**: Base + BSC 스크래퍼 + 포지션 매니저 완성. Solana와 Divergence 엔진은 미구현.
- **실행 환경**: Windows 데스크톱 (집 PC 상시 가동)

## 아키텍처

```
[스크래퍼] → [SQLite/JSONL] → [재귀성 엔진] → [포지션 매니저] → [Telegram 알림]
   ↓              ↓                ↓                  ↓
 trending_     data/*.jsonl    detect_entry_     mc_position_
 scraper.py                     signals()         manager.py
 base_scraper
```

## 파일 구조

| 파일 | 역할 | 상태 |
|---|---|---|
| `trending_scraper.py` | BSC Trending 스크래퍼 | 완료 |
| `base_scraper.py` | Base Trending 스크래퍼 | 완료 |
| `mc_position_manager.py` | 멀티체인 포지션 관리 (-20% + 트레일링 15%) | 완료 |
| `orchestrator.py` | 메인 루프, 모든 모듈 통합 | 완료 |
| `solana_scraper.py` | Solana Trending (미구현) | TODO |
| `divergence_engine.py` | 재귀성 지표 계산 (Module 2) | TODO |
| `swap_executor.py` | PancakeSwap/Jupiter 실제 매매 (Module 4) | TODO |

## 체인별 파라미터 (시뮬레이션 도출 최적값)

| 체인 | 스탑 | 트레일링 | 익절 | 포지션% | 동시보유 | 보유시간 |
|---|---|---|---|---|---|---|
| BSC | -20% | 15% | 80% | 4% | 3 | 72h |
| Solana | -25% | 18% | 100% | 1.5% | 4 | 48h |
| Base | -15% | 12% | 60% | 6% | 2 | 96h |

## 자본 배분 (안전 중심 권장)

Base 55% + BSC 25% + Solana 20% → 연 예상 +95%, MDD -8%

## 실행 명령어

```bash
# 단일 사이클 실행
python orchestrator.py --once

# 루프 시작 (600초 = 10분 주기)
python orchestrator.py --interval 600

# 페이퍼 트레이딩 (실제 거래 없이)
python orchestrator.py --dry-run

# 개별 스크래퍼 테스트
python base_scraper.py
python trending_scraper.py --chain bsc
```

## 환경 변수 (.env 파일)

```
BSCSCAN_API_KEY=
BASESCAN_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

## 중요 맥락

1. **이 시스템은 시그널 생성까지만**. 실제 swap 트랜잭션은 수동 또는 Module 4 추가 필요.
2. **스크래핑 경로 우선순위**: Binance 내부 API → Playwright → Dexscreener (fallback).
3. **데이터 축적**: `data/` 폴더에 JSONL로 시계열 누적. Divergence 엔진은 최소 2주치 데이터 필요.
4. **Safety Gate**: 유동성 / 거래수 / 이미 급등 여부 등 3중 필터.
5. **Circuit Breaker**: 포트폴리오 MDD -15% 경고, -25% 전체 중단.

## 다음 작업 (우선순위)

- [ ] VPS/Windows 환경 세팅 완료
- [ ] 2주 페이퍼 트레이딩 데이터 수집
- [ ] Divergence Engine (Module 2) 구현 — Perception vs Fundamental 괴리 측정
- [ ] Solana 스크래퍼 추가
- [ ] 실제 swap executor (Module 4) 구현

## 현재 작업 관행

- 각 기능 추가 전 테스트 작성
- `data/positions.jsonl`은 append-only 로그 (감사 추적)
- Telegram 알림은 핵심 이벤트만 (진입/청산/에러)
- 로그는 `reflexivity.log`에 누적

## 자주 쓰는 유틸 명령

```bash
# 오픈 포지션 확인
cat data/positions.jsonl | jq 'select(.is_closed == false)'

# 오늘 청산된 포지션
cat data/positions.jsonl | jq 'select(.exit_timestamp | startswith("2026-04-17"))'

# 체인별 성과
cat data/positions.jsonl | jq -s 'group_by(.chain) | map({chain: .[0].chain, count: length, pnl: map(.realized_pnl_usd // 0) | add})'
```

## 트러블슈팅

- **Dexscreener 403**: User-Agent 로테이션, 주기 늘리기
- **BSCScan rate limit**: 5 req/sec 준수 확인
- **Playwright 메모리**: 각 실행 후 즉시 종료 (long-running 금지)
- **포지션 복구 실패**: `data/positions.jsonl` 마지막 줄 JSON 유효성 체크
