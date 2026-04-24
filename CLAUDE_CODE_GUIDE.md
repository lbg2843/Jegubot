# Claude Code 통합 가이드

Claude Code에서 이 프로젝트를 다루는 가장 효율적인 방법.

---

## 1. 폴더 구조 전략

### 추천: 같은 상위 폴더 내 서브프로젝트

기존 봇 프로젝트가 있다고 가정하면 이렇게:

```
C:\Users\YOUR_NAME\projects\
├── dango_btc_bot\           # 기존 BTC 퍼페추얼 봇
├── hanwha_ticket_alert\     # 기존 티켓 알림 봇
├── fal_ai_pipeline\         # 기존 이미지 생성
└── reflexivity\             # ★ 이번 프로젝트 (새로 추가)
    ├── CLAUDE.md             # 프로젝트 맥락
    ├── requirements.txt
    ├── .env                  # API 키
    ├── trending_scraper.py
    ├── base_scraper.py
    ├── mc_position_manager.py
    ├── orchestrator.py
    ├── data/                 # 스냅샷 + 포지션 JSONL
    ├── logs/
    └── tests/
```

**이 구조의 장점**
- 각 봇의 의존성 독립 (각자 venv)
- Claude Code에서 `cd reflexivity` 하면 딱 이 프로젝트만 컨텍스트
- 봇 간 코드 공유 필요 시 상위 폴더에 `shared/` 추가 가능

---

## 2. Claude Code 시작 방법

### 2-1. 프로젝트 열기
```bash
cd C:\Users\YOUR_NAME\projects\reflexivity
claude
```

Claude Code는 **현재 디렉토리의 `CLAUDE.md`를 자동으로 읽습니다**. 제가 만든 `CLAUDE.md` 파일을 루트에 두면 매번 프로젝트 설명 안 해도 됩니다.

### 2-2. 초기 세팅 명령

Claude Code 세션 내에서:
```
현재 프로젝트 상태 파악하고 .env 파일 템플릿 만들어줘
```

Claude Code가:
- `CLAUDE.md` 읽고 프로젝트 이해
- 필요한 API 키 확인
- `.env` 템플릿 생성
- 누락된 파일 있으면 알림

---

## 3. 효율적 워크플로우 패턴

### 패턴 A: 기능 추가 요청

❌ **비효율**: "Solana 스크래퍼 만들어줘"

✅ **효율**: 
```
base_scraper.py와 비슷한 구조로 Solana 스크래퍼 만들어줘.
Raydium + Orca DEX 데이터 가져오고, Jupiter API로 가격 크로스체크.
mc_position_manager.py의 SOLANA 파라미터와 호환되게.
```

Claude Code는 CLAUDE.md 참고해서 체인 파라미터 자동 반영.

### 패턴 B: 디버깅

❌ **비효율**: "스크래퍼가 안 돼"

✅ **효율**:
```
python base_scraper.py 돌렸는데 이 에러 나옴:
[에러 메시지 붙여넣기]

dexscreener 응답 구조 확인하고 원인 찾아줘
```

Claude Code는 실제로 `python base_scraper.py --dry-run` 같은 걸 **직접 실행해서 재현** 가능.

### 패턴 C: 데이터 분석

✅ **자연스러운 요청**:
```
지난 1주일 data/positions.jsonl 분석해서 
체인별 수익률 / 승률 / 평균 보유시간 리포트 만들어줘
```

Claude Code가 직접 `cat`, `jq`, Python 스크립트로 분석하고 결과 제시.

### 패턴 D: 시뮬레이션 vs 실데이터 비교

✅ **이게 진짜 핵심 가치**:
```
multichain_simulation.py의 예상치와 
실제 data/positions.jsonl 결과를 비교해서
어느 부분이 시뮬레이션과 괴리 있는지 분석해줘
```

---

## 4. 기존 봇들과 리소스 공유

### 4-1. 공유 가상환경 (비추천)
각 봇 의존성이 충돌할 수 있음. 피하세요.

### 4-2. 개별 venv (추천)
```bash
cd reflexivity
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### 4-3. 공통 유틸 공유 (필요 시)
```
projects\
├── shared\
│   ├── telegram_client.py    # 모든 봇이 쓰는 Telegram 헬퍼
│   └── db_utils.py
├── dango_btc_bot\
└── reflexivity\
```

import 방법:
```python
import sys
sys.path.insert(0, "../shared")
from telegram_client import send_message
```

### 4-4. Telegram 봇 공유
여러 프로젝트가 같은 Telegram Bot 토큰 써도 문제 없음. 채널별 `chat_id`만 다르게:
- `TELEGRAM_CHAT_ID_DANGO=...`
- `TELEGRAM_CHAT_ID_REFLEXIVITY=...`
- `TELEGRAM_CHAT_ID_HANWHA=...`

---

## 5. Claude Code 활용 고급 팁

### 5-1. 체크포인트 저장
중요한 작업 전에:
```
현재 상태 git commit 해줘. 메시지는 "Before adding Solana scraper"
```

Claude Code가 자동으로 staging + commit.

### 5-2. 테스트 자동화
```
mc_position_manager.py에 대한 pytest 테스트 만들어줘.
특히 체인별 스탑로스 로직과 Circuit Breaker 동작 검증.
```

### 5-3. 점진적 리팩토링
```
orchestrator.py가 너무 길어지면 
scraping, analysis, execution 모듈로 분리해줘.
단, 한 번에 다 바꾸지 말고 단계별로 진행 + 각 단계 후 테스트.
```

### 5-4. 코드 리뷰 요청
```
base_scraper.py 코드 리뷰해줘.
특히 에러 처리, 재시도 로직, rate limit 준수 관점에서.
```

---

## 6. Claude Code 안에서 파일 관리

### 6-1. 새 파일 생성은 맡기기
직접 코드 복사-붙여넣기 하지 말고:
```
trending_scraper.py랑 같은 인터페이스로 solana_scraper.py 만들어줘.
Raydium + Jupiter API 쓰고, 러그 필터 엄격하게.
```

### 6-2. 수정도 맡기기
```
mc_position_manager.py의 can_open() 함수에 
같은 심볼이 24시간 내 청산됐으면 재진입 거부하는 로직 추가해줘
```

### 6-3. 설정 변경
```
체인별 파라미터 튜닝할게. BSC의 스탑로스를 -18%로, 
Solana의 포지션 크기를 1%로 낮춰줘.
mc_position_manager.py의 CHAIN_CONFIGS 업데이트해줘.
```

---

## 7. 실전 세션 예시

### 세션 1: 초기 세팅 (30분)
```
사용자: "reflexivity 프로젝트 세팅 시작할게. 
        Python 3.11, Windows 환경. requirements.txt 기반으로 venv 만들고,
        .env 템플릿 준비해줘."

Claude Code: [venv 생성, 의존성 설치, .env.template 생성]

사용자: "API 키 다 넣었어. base_scraper 단독 테스트 해봐줘."

Claude Code: [python base_scraper.py --dry-run 실행, 결과 확인, 문제 있으면 수정]
```

### 세션 2: 페이퍼 트레이딩 1주 후 (1시간)
```
사용자: "1주일 돌렸어. 결과 분석하고 시뮬레이션과 비교해줘."

Claude Code: [data/positions.jsonl 읽어서 분석, 시뮬레이션 예측과 대조, 
             괴리 원인 추적, 파라미터 조정 제안]

사용자: "네 제안대로 파라미터 조정해줘. 그리고 이 내용 CLAUDE.md에 기록해둬."

Claude Code: [수정, 테스트, 문서 업데이트, git commit]
```

### 세션 3: Divergence 엔진 추가 (2-3시간)
```
사용자: "이제 2주치 데이터 쌓였어. Divergence 엔진 만들자.
        CLAUDE.md에 있는 Module 2 구현 방향대로."

Claude Code: [기존 대화 컨텍스트 활용해서 Perception vs Fundamental 계산,
             phase classifier 구현, 기존 orchestrator.py에 통합]
```

---

## 8. 기존 봇과의 시너지 체크리스트

### Dango DEX BTC 퍼페추얼 봇이 있다면
- **공통 지식 활용**: BTC 퍼페추얼에서 배운 진입/청산 타이밍을 밈코인에 적용 가능
- **자본 배분**: BTC 봇 + Reflexivity 봇에 각각 할당 비율 결정 필요
- **리스크 분산**: 두 봇이 동시에 크게 당하지 않도록 포지션 상관관계 모니터링

### Hanwha 티켓 봇이 있다면
- **Telegram 인프라 재사용**: 봇 토큰 공유
- **systemd timer / 작업 스케줄러 패턴 재사용**

### fal-ai 이미지 생성 파이프라인이 있다면
- **직접 연관은 없지만** 리포트용 차트 생성에 활용 가능
  - 일일/주간 성과 차트를 이미지로 만들어 Telegram 전송

---

## 9. 시작 체크리스트

오늘 Claude Code 세션에서:

- [ ] `projects/reflexivity/` 폴더 생성
- [ ] 받은 파일들 (trending_scraper, base_scraper, mc_position_manager, orchestrator) 복사
- [ ] `CLAUDE.md`와 `requirements.txt` 배치
- [ ] `claude` 실행 후 다음 프롬프트:

```
CLAUDE.md 읽고 프로젝트 이해해줘. 
그 다음 venv 세팅, 의존성 설치, .env 템플릿 생성까지 해줘.
Windows 환경이야.
```

세션 내내 자연어로 작업 요청하면 됩니다. 직접 코드 수정하지 마세요 — Claude Code에게 맡기는 게 더 정확하고 빠릅니다.

---

## 10. 효율 극대화 트릭

### 10-1. 자주 쓰는 프롬프트 저장
`PROMPTS.md` 파일 만들어두면 재활용 편함:
```
# 자주 쓰는 Claude Code 프롬프트

## 일일 상태 체크
"오늘 포지션 현황 요약해줘. 오픈 포지션, 어제 청산 결과, 
 현재 MDD, 포트폴리오 가치 변화."

## 새 체인 추가
"base_scraper.py와 같은 구조로 {체인명} 스크래퍼 만들어줘.
 mc_position_manager.py의 해당 체인 파라미터 참고."

## 파라미터 A/B 테스트
"현재 data/positions.jsonl 기준으로 스탑로스 -15% vs -20% 성과 비교해줘."
```

### 10-2. 주간 회고 자동화
매주 토요일 실행:
```
지난 7일 거래 분석하고 다음을 포함한 리포트 만들어줘:
1. 총 PnL, 승률, 샤프
2. 체인별 기여도
3. 시뮬레이션 예측과의 괴리
4. 다음 주 조정 제안
결과는 WEEKLY_REVIEW_{날짜}.md로 저장.
```
