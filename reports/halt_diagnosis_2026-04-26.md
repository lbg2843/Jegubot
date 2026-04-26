## Halt Diagnosis - 2026-04-26

### Actual halt state file
- `data/trading_safety_state.json`
- `data/trade_executor_state.json` only stores pending approvals and was not the halt source.

### What triggered the halt
- Halt reason: `daily_loss_limit`
- Log trigger: `2026-04-26 16:06:56` local time
- Trade that triggered it:
  - `LASTMAN` on `solana`
  - exit reason: `STOP_LOSS`
  - realized PnL: `-$82.80`

### Why timestamps looked inconsistent
- `reflexivity.log` uses local time (Asia/Seoul)
- `positions.jsonl` exit timestamps for executor-managed closes were written in UTC
- Example:
  - log close time: `2026-04-26 16:06:56`
  - positions file close time: `2026-04-26T07:06:56`

### Why the halt kept reappearing
- After manual clear, the bot later re-halted again at `2026-04-26 19:33:29`
- Same root cause: repeated `LASTMAN` stop-loss exits drove `daily_pnl_usd` below the daily limit

### Dry-run vs live finding
- Runtime `.env` was `TRADING_MODE=dry_run`
- Old safety logic still counted dry-run closes toward halt state
- That caused simulated losses to contaminate live-style safety state

