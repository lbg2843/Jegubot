# -*- coding: utf-8 -*-
"""phantom_tracker.py — 게이트에서 '떨어낸' 진입을 샀다 치고 추적해 가상 실현손익 측정.

목적: passes_safety_gate 가 reject 한 후보는 진입을 안 하므로 실현손익이 영영 안 남는다.
"막은 게 사실 좋았으면 게이트를 손봐야 한다"를 검증하려면 그 손익을 알아야 한다.
→ reject 된 후보를 size_usd 100 짜리 '팬텀 포지션'으로 열고, 라이브와 100% 동일한
   ExitSignalEngine/Position 으로 매 사이클 시세를 따라가며 청산해 가상 PnL 을 남긴다.
   라이브 거래엔 0 영향(완전 분리). reason 별로 집계하면 어떤 게이트 규칙이 위너를 버리는지 보인다.

신뢰도 주의(설계상 제외):
  - 추적 대상(soft/전략 컷): divergence / falling_knife / eth_down / already_pumped
    → 실제로 살 수 있던 토큰이라 가상 PnL 이 의미 있음.
  - 비추적(hard/안전 컷): low_liquidity / low_liq_ratio / low_tx_count / audit_flags
    → 허니팟·미체결로 '실현 불가능한 거짓 수익'이 됨. 아예 기록하지 않는다(용량도 절약).

용량: 오픈 시 1줄(phantom_rejects.jsonl) + 청산 시 1줄(phantom_positions.jsonl)만 append.
매 사이클 평가가는 작은 state 파일(phantom_state.json)에 덮어쓰기 → 파일 폭증 없음.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from mc_position_manager import ExitReason, ExitSignalEngine, Position
from trading.time_utils import iso_utc_now, parse_iso_utc, utc_now

log = logging.getLogger("phantom")

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

# reject reason 문자열 prefix -> soft 태그(추적 대상). 그 외 reason 은 비추적.
SOFT_PREFIXES = {
    "divergence_below": "divergence",
    "sweet_spot_falling": "falling_knife",
    "eth_4h_down": "eth_down",
    "already_pumped": "already_pumped",
}

PHANTOM_SIZE_USD = 100.0          # 가상 명목금(상대비교용 고정)
REOPEN_COOLDOWN_HOURS = 24.0      # 같은 토큰 팬텀 청산 후 재오픈 금지(끝없는 재생성 방지)


def classify_reason(reason: str) -> Optional[str]:
    r = str(reason or "")
    for prefix, tag in SOFT_PREFIXES.items():
        if r.startswith(prefix):
            return tag
    return None


class PhantomTracker:
    def __init__(self, chain_configs: dict,
                 rejects_path: str | Path = DATA / "phantom_rejects.jsonl",
                 positions_path: str | Path = DATA / "phantom_positions.jsonl",
                 state_path: str | Path = DATA / "phantom_state.json"):
        self.engine = ExitSignalEngine(chain_configs)
        self.chains = set(chain_configs.keys())
        self.rejects_path = Path(rejects_path)
        self.positions_path = Path(positions_path)
        self.state_path = Path(state_path)
        self.open: dict[tuple, Position] = {}
        self.meta: dict[tuple, str] = {}            # key -> reason 태그
        self.cooldown: dict[tuple, str] = {}        # key -> 마지막 청산 iso
        self._load_state()

    # ---- 상태 영속화 (bat 재시작에도 팬텀 포지션 유지) ----
    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"[phantom] state load fail (fresh start): {e}")
            return
        for rec in d.get("open", []):
            key = (rec["chain"], rec["contract"])
            self.meta[key] = rec.pop("reason_tag", "?")
            rec.pop("chain", None); rec.pop("contract", None)
            try:
                self.open[key] = Position(**rec)
            except Exception:
                continue
        self.cooldown = {tuple(k.split("|", 1)): v for k, v in d.get("cooldown", {}).items()}

    def _save_state(self) -> None:
        open_recs = []
        for key, pos in self.open.items():
            r = asdict(pos)
            r["chain"], r["contract"] = key[0], key[1]
            r["reason_tag"] = self.meta.get(key, "?")
            open_recs.append(r)
        payload = {
            "open": open_recs,
            "cooldown": {f"{k[0]}|{k[1]}": v for k, v in self.cooldown.items()},
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    def _append(self, path: Path, rec: dict) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ---- 게이트 reject 훅 ----
    def record_reject(self, cand: dict, chain: str, reason: str) -> None:
        tag = classify_reason(reason)
        if tag is None or chain not in self.chains:
            return  # 비추적(신뢰불가/안전 컷) → 기록조차 안 함
        contract = str(cand.get("contract_address") or "").strip().lower()
        if not contract:
            return
        key = (chain, contract)
        if key in self.open:
            return  # 이미 팬텀 추적 중(토큰 단위 중복 제거)
        last_close = self.cooldown.get(key)
        if last_close:
            try:
                if (utc_now() - parse_iso_utc(last_close)).total_seconds() / 3600 < REOPEN_COOLDOWN_HOURS:
                    return  # 최근 팬텀 청산 → 쿨다운
            except Exception:
                pass
        price = cand.get("price_usd")
        if not isinstance(price, (int, float)) or price <= 0:
            return  # 가격 없으면 추적 불가
        now = iso_utc_now()
        liq = cand.get("liquidity_usd") or 0.0
        pos = Position(
            chain=chain, symbol=str(cand.get("symbol") or "?"), contract_address=contract,
            entry_timestamp=now, entry_price=float(price), entry_liquidity=float(liq),
            entry_b_holders=0,  # 홀더-엑소더스 규칙 비활성(스냅샷 홀더델타 신뢰 낮음)
            size_usd=PHANTOM_SIZE_USD, current_price=float(price), peak_price=float(price),
            current_liquidity=float(liq), last_update=now,
            entry_path=str(cand.get("entry_path") or "reflexivity"),
        )
        self.open[key] = pos
        self.meta[key] = tag
        self._append(self.rejects_path, {
            "opened_at": now, "chain": chain, "contract": contract,
            "symbol": pos.symbol, "reason_tag": tag, "reason": str(reason),
            "entry_path": pos.entry_path, "entry_price": pos.entry_price,
            "entry_liquidity": pos.entry_liquidity,
        })
        self._save_state()

    # ---- 매 사이클 시세 갱신 + 청산 ----
    def update(self, snapshots_by_chain: dict) -> int:
        if not self.open:
            return 0
        price_by = {}
        for chain, snaps in (snapshots_by_chain or {}).items():
            m = {}
            for s in snaps or []:
                c = str(s.get("contract_address") or "").strip().lower()
                if c and isinstance(s.get("price_usd"), (int, float)) and s["price_usd"] > 0:
                    m[c] = s
            price_by[chain] = m

        closed = 0
        for key in list(self.open.keys()):
            pos = self.open[key]
            snap = price_by.get(key[0], {}).get(key[1])
            if snap is not None:
                # 라이브 update_all 과 동일한 갱신
                pos.current_price = snap["price_usd"]
                pos.peak_price = max(pos.peak_price, snap["price_usd"])
                pos.current_liquidity = snap.get("liquidity_usd") or pos.current_liquidity
                pos.last_update = iso_utc_now()
            # 스냅샷이 없어도(트렌딩 이탈) hold_hours 는 now 기준이라 TIME_EXIT 안전망 작동
            should_exit, reason, _msg = self.engine.evaluate(pos)
            if should_exit:
                self._close(key, pos, reason)
                closed += 1
        self._save_state()
        return closed

    def _close(self, key: tuple, pos: Position, reason: ExitReason) -> None:
        now = iso_utc_now()
        pos.is_closed = True
        pos.exit_timestamp = now
        pos.exit_price = pos.current_price
        pos.exit_reason = reason.value
        pnl = pos.unrealized_pnl_pct
        pos.realized_pnl_pct = pnl
        pos.realized_pnl_usd = pos.size_usd * pnl / 100.0
        tag = self.meta.get(key, "?")
        self._append(self.positions_path, {
            "chain": pos.chain, "contract": pos.contract_address, "symbol": pos.symbol,
            "reason_tag": tag, "entry_path": pos.entry_path,
            "entry_timestamp": pos.entry_timestamp, "exit_timestamp": now,
            "entry_price": pos.entry_price, "exit_price": pos.exit_price,
            "peak_pnl_pct": round(pos.peak_pnl_pct, 3),
            "realized_pnl_pct": round(pnl, 3),
            "realized_pnl_usd": round(pos.realized_pnl_usd, 4),
            "exit_reason": pos.exit_reason,
            "hold_hours": round(pos.hold_hours, 2),
        })
        self.cooldown[key] = now
        self.open.pop(key, None)
        self.meta.pop(key, None)
        # 쿨다운 prune(7일 경과분 제거)
        if len(self.cooldown) > 500:
            cutoff = utc_now()
            self.cooldown = {
                k: v for k, v in self.cooldown.items()
                if (cutoff - parse_iso_utc(v)).total_seconds() / 3600 < REOPEN_COOLDOWN_HOURS * 7
            }
