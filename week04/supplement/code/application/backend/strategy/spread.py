"""Two overseas futures legs, driven by a common spread signal.

Leg execution, fill deduplication, reconnect reconciliation and order uncertainty
use the existing RunService/RunPump. The coordinator never fabricates account
fills or resubmits an order with an unknown outcome. All entries start flat.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import math
import statistics
import threading
import time
import uuid
import zipfile
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend import storage
from backend.nh_api.session import current_mode
from backend.strategy import engine, paper, runs, sim
from backend.strategy.journal import Journal
from backend.strategy.pump import RunPump, _open_now
from backend.strategy.service import RunService, StrategyRunError, _order_open
from backend.strategy.store import run_state_dir
from backend.strategy.spread_signals import book_snapshot, breakout, entry_cost, return_correlation, reversal
from backend.strategy.spread_report import execution_report, report_markdown
from backend.strategy.spread_trend import risk_plan, trend_entry, trend_exit, update_trend
from backend.strategy.spread_relative import relative_breakout_entry

VOLATILITY_STYLES = ('trend_pullback', 'relative_breakout')
BOOK_STYLES = ('pullback', 'breakout', *VOLATILITY_STYLES)

logger = logging.getLogger(__name__)
LABELS = {
    "ready": "시작 전", "warming": "표본 수집 중", "watching": "진입 감시 중",
    "entering": "두 레그 진입 중", "holding": "스프레드 보유 중",
    "closing": "두 레그 정리 중", "cooldown": "재진입 대기",
    "stopped": "중지", "attention": "계좌 확인 필요",
    "paused": "손실 후 휴식", "observing": "주문 제한 · 관찰 중",
}


class LegConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    code: str = Field(min_length=1, max_length=48, pattern=r"^[A-Za-z0-9._-]+$")
    quantity: int = Field(default=1, ge=1, le=100, strict=True)


class SpreadConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=80)
    leg_a: LegConfig
    leg_b: LegConfig
    beta: float = Field(default=1, gt=0, le=1000000)
    lookback: int = Field(default=120, ge=20, le=3600, strict=True)
    sample_seconds: int = Field(default=5, ge=1, le=60, strict=True)
    entry_z: float = Field(default=2, gt=0, le=10)
    exit_z: float = Field(default=0.5, ge=0, le=5)
    stop_loss: float = Field(default=300, gt=0, le=1000000)
    take_profit: float = Field(default=200, gt=0, le=1000000)
    max_loss: float = Field(default=1000, gt=0, le=1000000)
    max_hold_seconds: int = Field(default=900, ge=10, le=86400, strict=True)
    leg_timeout_seconds: int = Field(default=15, ge=3, le=120, strict=True)
    cooldown_seconds: int = Field(default=60, ge=5, le=3600, strict=True)
    max_entries: int = Field(default=10, ge=1, le=1000, strict=True)
    quote_age_seconds: int = Field(default=10, ge=1, le=60, strict=True)
    quote_skew_seconds: int = Field(default=3, ge=1, le=30, strict=True)
    end_at: int | None = Field(default=None, gt=0, strict=True)
    cash: float = Field(default=100000, gt=0, le=1000000000)
    fee_per_contract: float = Field(default=0, ge=0, le=10000)
    entry_style: Literal["threshold", "pullback", "breakout", "trend_pullback", "relative_breakout"] = "threshold"
    pullback_z: float = Field(default=0.25, gt=0, le=5)
    max_entry_z: float = Field(default=3.5, gt=0, le=20)
    stop_z: float = Field(default=0, ge=0, le=30)
    min_correlation: float = Field(default=0, ge=0, le=1)
    min_edge_cost_ratio: float = Field(default=0, ge=0, le=20)
    slippage_ticks: float = Field(default=1, ge=0, le=20)
    max_book_spread_ticks: float = Field(default=0, ge=0, le=100)
    loss_streak_limit: int = Field(default=0, ge=0, le=20, strict=True)
    entry_start_at: int | None = Field(default=None, gt=0, strict=True)
    entry_cutoff_at: int | None = Field(default=None, gt=0, strict=True)
    # Zero preserves saved v1 behavior. New presets explicitly opt in.
    min_entry_z: float = Field(default=0, ge=0, le=10)
    history_gap_seconds: int = Field(default=0, ge=0, le=600, strict=True)
    recovery_samples: int = Field(default=3, ge=1, le=20, strict=True)
    breakout_confirm_samples: int = Field(default=2, ge=2, le=10, strict=True)
    breakout_follow_z: float = Field(default=0.1, gt=0, le=2)
    session_seconds: int = Field(default=0, ge=0, le=86400, strict=True)
    loss_pause_seconds: int = Field(default=0, ge=0, le=3600, strict=True)
    trend_bars: int = Field(default=5, ge=3, le=20, strict=True)
    min_trend_efficiency: float = Field(default=.4, gt=0, le=1)
    trend_min_atr: float = Field(default=1.5, gt=0, le=10)
    pullback_atr: float = Field(default=.5, gt=0, le=5)
    max_pullback_atr: float = Field(default=1.5, gt=0, le=10)
    resume_atr: float = Field(default=.35, gt=0, le=5)
    reward_risk: float = Field(default=1.5, ge=1, le=5)
    trail_distance_r: float = Field(default=.75, gt=0, lt=1)
    breakout_buffer_atr: float = Field(default=.15, gt=0, le=2)
    breakout_follow_atr: float = Field(default=.1, gt=0, le=2)
    breakout_max_extension_atr: float = Field(default=1, gt=0, le=5)

    @model_validator(mode="after")
    def check_pair(self) -> SpreadConfig:
        if self.leg_a.code == self.leg_b.code:
            raise ValueError("두 레그는 서로 다른 종목·월물이어야 합니다")
        if self.exit_z >= self.entry_z:
            raise ValueError("청산 Z 기준은 진입 Z 기준보다 작아야 합니다")
        if self.min_entry_z and not self.exit_z < self.min_entry_z <= self.entry_z:
            raise ValueError("되돌림 진입 하한은 청산 Z보다 크고 최초 진입 Z 이하여야 합니다")
        if self.history_gap_seconds and self.history_gap_seconds < 3 * self.sample_seconds:
            raise ValueError("표본 유지 공백은 표본 간격의 3배 이상이어야 합니다")
        if self.entry_style in ("pullback", "breakout") and self.max_entry_z <= self.entry_z:
            raise ValueError("되돌림 진입의 최대 Z는 진입 Z보다 커야 합니다")
        if self.stop_z and self.stop_z <= max(self.entry_z, self.max_entry_z if self.entry_style == "pullback" else 0):
            raise ValueError("비상 청산 Z는 진입 범위보다 커야 합니다")
        if self.entry_style in VOLATILITY_STYLES:
            if self.sample_seconds > 10 or self.max_pullback_atr <= self.pullback_atr:
                raise ValueError("추세 전략은 10초 이하 표본과 눌림 폭보다 큰 최대 눌림이 필요합니다")
            if not self.min_edge_cost_ratio or not self.max_book_spread_ticks:
                raise ValueError("추세 전략은 비용·호가 제한이 필요합니다")
        if self.entry_style == 'relative_breakout' and self.breakout_max_extension_atr <= self.breakout_buffer_atr + self.breakout_follow_atr:
            raise ValueError('최대 추격 폭은 돌파 확인 폭과 추가 진행 폭의 합보다 커야 합니다')
        times = [value for value in (self.entry_start_at, self.entry_cutoff_at, self.end_at) if value is not None]
        if any(left >= right for left, right in zip(times, times[1:])):
            raise ValueError("진입 시작 → 신규 진입 종료 → 정리 시각 순서로 설정하세요")
        return self


def leg_draft(config: SpreadConfig, leg: LegConfig) -> dict[str, Any]:
    # Entry/exit rules remain disabled. Only the common coordinator can enter.
    return {
        "baseBar": "1분봉", "evalMode": "마감", "memo": "스프레드 전략의 관리 레그",
        "rules": {key: {"on": False, "steps": []} for key in ("buyEntry", "sellEntry", "buyExit", "sellExit")},
        "aux": {
            "orderType": "시장가", "priceBasis": "신호가", "session": "해외 CME",
            "fillCond": "", "perOrder": str(leg.quantity), "maxHold": str(leg.quantity),
            "addWhileOpen": False, "sessionEndFlat": False,
            "dailyLoss": str(config.max_loss), "marginBudget": str(config.cash), "stopTicks": "1",
        },
    }


class SpreadService:
    def __init__(self, *, path: Path | None = None, legs: RunService | None = None,
                 is_open: Any = None, occupied: Any = None) -> None:
        base = run_state_dir()
        self.path = path if path is not None else base / "spread-runs.json"
        self.legs = legs if legs is not None else RunService(
            store=runs.RunStore(base / "spread-legs.json"), journal=Journal(base / "spread-leg-records.jsonl"),
        )
        self.is_open = is_open if is_open is not None else _open_now
        self.occupied = occupied if occupied is not None else self._regular_occupied
        self._lock = threading.RLock()
        self._loaded = False
        self._pairs: dict[str, dict[str, Any]] = {}
        self._history: dict[str, deque[float]] = {}
        self._price_history: dict[str, deque[tuple[float, float]]] = {}
        self._time_history: dict[str, deque[int]] = {}
        self._events: dict[str, deque[dict[str, Any]]] = {}
        self._samples: dict[str, deque[dict[str, Any]]] = {}

    @property
    def log_path(self) -> Path:
        return self.path.with_name("spread-events.jsonl")

    @staticmethod
    def _regular_occupied(code: str) -> bool:
        from backend.strategy.service import service as regular
        return any(one.run.code == code and one.run.mode == runs.SIM
                   and one.run.state != runs.ENDED for one in regular.engines())

    def load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            state = storage.ReadState()
            body, _ = storage.read_json(self.path, state=state)
            if state.status == storage.BROKEN:
                raise StrategyRunError("스프레드 상태 파일을 읽지 못했습니다. 상태 폴더를 확인하세요")
            if body and (body.get("version") != 1 or storage.from_other_app(body, current_mode())):
                raise StrategyRunError("스프레드 상태 파일의 실행 환경·형식을 확인하세요")
            for pair in body.get("pairs", []):
                config = SpreadConfig.model_validate(pair["config"])
                pair["config"] = config.model_dump()
                if pair["state"] not in ("ready", "stopped"):
                    pair.update(state="attention", enabled=False, note="앱이 재시작됐습니다. 레그·계좌를 확인한 뒤 새 실행을 만드세요")
                pair["restored"] = bool(pair.get("leg_ids"))
                if pair["restored"] and pair["state"] != "stopped":
                    pair.update(z=None, spread=None)
                self._pairs[pair["id"]] = pair
            self._loaded = True

    def _save(self) -> None:
        storage.write_json(self.path, {"version": 1, storage.ORIGIN: current_mode(), "pairs": list(self._pairs.values())})

    def _event(self, pair: dict[str, Any], kind: str, at: int, **values: Any) -> None:
        row = {"pair_id": pair["id"], "kind": kind, "time": at, "mode": pair.get("mode"), **values}
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if self.log_path.is_file() and self.log_path.stat().st_size >= 64 * 1024 * 1024:
            self.log_path.replace(self.log_path.with_suffix(".jsonl.old"))
        # Fail before sending further orders if evidence cannot be recorded.
        with self.log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        if kind != "sample":
            self._events.setdefault(pair["id"], deque(maxlen=100)).append(row)

    def create(self, config: SpreadConfig) -> dict[str, Any]:
        self.load()
        with self._lock:
            if len(self._pairs) >= 50:
                raise StrategyRunError("최대 50개 실행을 보관합니다. 제출 기록을 내보내고 상태 폴더를 관리하세요")
            pair = {"id": uuid.uuid4().hex, "config": config.model_dump(), "state": "ready",
                    "enabled": False, "leg_ids": [], "mode": None, "entries": 0, "note": "종목과 설정을 확인한 뒤 시작하세요"}
            self._pairs[pair["id"]] = pair
            self._save()
            return self._row(pair)

    def _get(self, pair_id: str) -> dict[str, Any]:
        self.load()
        if pair_id not in self._pairs:
            raise StrategyRunError("그 스프레드 실행을 찾지 못했습니다")
        return self._pairs[pair_id]

    def start(self, pair_id: str, mode: str, approved: bool, at: int) -> dict[str, Any]:
        with self._lock:
            pair = self._get(pair_id)
            if pair["state"] != "ready":
                raise StrategyRunError("시작 전 실행만 시작할 수 있습니다. 다시 돌리려면 새 실행을 만드세요")
            if mode not in (runs.PAPER, runs.SIM):
                raise StrategyRunError("스프레드는 앱 시뮬레이션과 해외 모의계좌만 지원합니다")
            if mode == runs.SIM and not approved:
                raise StrategyRunError("두 레그의 모의계좌 주문을 확인해야 시작할 수 있습니다")
            config = SpreadConfig.model_validate(pair["config"])
            if config.end_at is not None and config.end_at <= at:
                raise StrategyRunError("종료 시각이 지났습니다")
            if config.entry_cutoff_at is not None and config.entry_cutoff_at <= at:
                raise StrategyRunError("신규 진입 종료 시각이 지났습니다. 새 시간으로 설정하세요")
            active = [one for one in self._pairs.values() if one.get("leg_ids") and one["state"] != "stopped"]
            if len(active) >= 2:
                raise StrategyRunError("동시에 두 스프레드까지만 실행할 수 있습니다")
            codes = {config.leg_a.code, config.leg_b.code}
            if mode == runs.SIM and any(self.occupied(code) for code in codes):
                raise StrategyRunError("같은 종목을 사용하는 계좌 전략이 있습니다. 기존 실행부터 종료하세요")
            for other in active:
                if mode == runs.SIM and other.get("mode") == mode:
                    if codes & {other["config"]["leg_a"]["code"], other["config"]["leg_b"]["code"]}:
                        raise StrategyRunError("다른 스프레드가 같은 계좌 종목을 사용하고 있습니다")
            currencies, multipliers = [], []
            for leg in (config.leg_a, config.leg_b):
                facts = self.legs.instrument("overseas", leg.code, "overseas")
                if facts["kind"] != "futures" or not facts["multiplier"] or not facts["tick"]:
                    raise StrategyRunError(f"{leg.code}: 승수·틱을 확인할 수 있는 해외 선물만 지원합니다")
                if facts["daysLeft"] is not None and facts["daysLeft"] <= 1:
                    raise StrategyRunError(f"{leg.code}: 만기가 임박했습니다. 다른 월물을 고르세요")
                currency = self.legs._currency_of("overseas", leg.code)
                currencies.append(currency)
                multipliers.append(facts["multiplier"])
                problems = self.legs.start_problems(
                    draft=leg_draft(config, leg), market="overseas", code=leg.code, session="overseas",
                    qty=leg.quantity, cash=config.cash / 2, fee_per_contract=config.fee_per_contract,
                    mode=mode, environment="paper" if mode == runs.SIM else "", approved=approved, managed=True,
                )
                if problems:
                    raise StrategyRunError(problems[0]["message"], problems)
            if not currencies[0] or currencies[0] != currencies[1]:
                raise StrategyRunError("합산 손익을 계산하려면 두 레그의 결제 통화가 같아야 합니다")
            if config.min_edge_cost_ratio:
                matched_beta = config.leg_b.quantity * multipliers[1] / (config.leg_a.quantity * multipliers[0])
                if not math.isclose(config.beta, matched_beta, rel_tol=1e-6):
                    raise StrategyRunError(f"비용 필터를 쓰려면 수량·승수에 맞게 β를 {matched_beta:g}로 설정하세요")
            pair.update(mode=mode, currency=currencies[0], started_at=at, state="warming", enabled=False,
                        quote_environment=self.legs._environment(),
                        leg_ids=[f"sp-{pair_id}-a", f"sp-{pair_id}-b"], cycle_base=0.0, last_sample=0,
                        session_end_at=at + config.session_seconds * 1000 if config.session_seconds else None)
            self._save()
            self._event(pair, "start", at, config=config.model_dump())
            try:
                for leg, leg_id in zip((config.leg_a, config.leg_b), pair["leg_ids"], strict=True):
                    self.legs.add(run_id=leg_id, version_id=pair_id, draft=leg_draft(config, leg),
                                  market="overseas", code=leg.code, session="overseas", qty=leg.quantity,
                                  cash=config.cash / 2, fee_per_contract=config.fee_per_contract, mode=mode,
                                  approved_at=at if mode == runs.SIM else 0,
                                  environment="paper" if mode == runs.SIM else "", controller="spread")
                if any(one.run.state != runs.ARMED for one in self._engines(pair)):
                    raise StrategyRunError("레그가 계좌 대사를 통과하지 못했습니다. 계좌·미체결을 확인하세요")
            except Exception:
                pair.update(state="attention", enabled=False, note="시작하지 못했습니다. 레그·계좌를 확인하세요")
                self._save()
                raise
            pair.update(enabled=True, note="동기화된 실시간 시세 표본을 모읍니다")
            self._history[pair_id] = deque(maxlen=config.lookback)
            self._price_history[pair_id] = deque(maxlen=config.lookback)
            self._time_history[pair_id] = deque(maxlen=config.lookback)
            self._save()
            return self._row(pair)

    def _engines(self, pair: dict[str, Any]) -> list[engine.RunEngine]:
        known = {one.run.id: one for one in self.legs.engines()}
        return [known[key] for key in pair["leg_ids"] if key in known]

    @staticmethod
    def _net(engines: list[engine.RunEngine]) -> float:
        return sum(one.ledger.realized - one.ledger.fee + one.ledger.unrealized(one.book.last_price) for one in engines)

    def rows(self) -> list[dict[str, Any]]:
        self.load()
        with self._lock:
            return [self._row(one) for one in reversed(list(self._pairs.values()))]

    def _row(self, pair: dict[str, Any]) -> dict[str, Any]:
        legs = []
        for one in self._engines(pair):
            with one.lock:
                legs.append({"id": one.run.id, "code": one.run.code, "state": one.run.state,
                             "quantity": one.ledger.position.quantity, "average": one.ledger.position.avg_price,
                             "price": one.book.last_price, "feed": one.feed, "updated_at": one.last_trade_at,
                             "realized": one.ledger.realized, "fee": one.ledger.fee,
                             "unrealized": runs.open_profit(one.ledger, one.book.last_price),
                             "multiplier": one.run.multiplier, "open_orders": len(one.book.open_orders)})
        count = len(self._history.get(pair["id"], ()))
        config = SpreadConfig.model_validate(pair["config"])
        setup = pair.get("setup")
        setup_z = ((pair["spread"] - setup["mean"]) / setup["deviation"]
                   if setup and pair.get("spread") is not None else None)
        times = self._time_history.get(pair["id"], ())
        return {**pair, "state_label": LABELS.get(pair["state"], pair["state"]), "legs": legs,
                "readiness": {"required": config.lookback, "collected": count,
                              "remaining_seconds": (0 if pair.get("z") is not None else max(1, config.lookback - count + int(count > 0))) * config.sample_seconds,
                              "nominal_seconds": config.lookback * config.sample_seconds,
                              "window_seconds": round((times[-1] - times[0]) / 1000, 1) if len(times) > 1 else 0,
                              "recovery_remaining": pair.get("recovery_remaining", 0)},
                "setup_z": setup_z,
                "setup_retreat": max(0, setup["peak"] - setup_z * setup["sign"]) if config.entry_style != 'trend_pullback' and setup_z is not None and "peak" in setup else None,
                "setup_phase": setup.get('phase') if setup else None,
                "setup_pullback_atr": (setup['peak'] - setup['sign'] * pair['spread']) / setup['atr'] if setup and setup.get('atr') and 'peak' in setup else None,
                "setup_resume_atr": (setup['sign'] * pair['spread'] - setup['trough']) / setup['atr'] if setup and setup.get('atr') and 'trough' in setup else None,
                "setup_confirmations": setup.get("confirmations") if setup else None,
                "setup_follow": (setup["sign"] * (pair["spread"] - setup["trigger"]) / setup["deviation"]
                                 if setup and "trigger" in setup else None),
                "fees": sum(leg["fee"] for leg in legs),
                "samples_count": len(self._history.get(pair["id"], ())),
                "events": list(self._events.get(pair["id"], ())),
                "samples": list(self._samples.get(pair["id"], ())),
                "net": (pair.get("final_net") if pair["state"] == "stopped" else None)
                if pair.get("restored") else self._net(self._engines(pair))}

    @contextmanager
    def regular_start_guard(self, code: str, mode: str):
        """Serialize regular-account starts with pair starts that reserve the same code."""
        with self._lock:
            self.load()
            if mode == runs.SIM and any(
                pair.get("mode") == runs.SIM and pair["state"] != "stopped"
                and code in (pair["config"]["leg_a"]["code"], pair["config"]["leg_b"]["code"])
                for pair in self._pairs.values()
            ):
                raise StrategyRunError("스프레드가 사용하는 종목입니다. 스프레드를 먼저 정리·종료하세요")
            yield

    def pause_entries(self, modes: list[str] | None, at: int) -> list[str]:
        with self._lock:
            self.load()
            paused = []
            for pair in self._pairs.values():
                if not pair["enabled"] or (modes is not None and pair.get("mode") not in modes):
                    continue
                pair["enabled"] = False
                paused.append(pair["id"])
                if pair["state"] != "holding":
                    self._close(pair, at, "전체 신규 진입 중지로 레그를 정리합니다", stop=True)
                else:
                    pair["note"] = "신규 진입을 중지했습니다. 보유는 수렴·손절·익절 기준으로 정리합니다"
                self._event(pair, "pause_entries", at)
            self._save()
            return paused

    def retire(self, pair_id: str, at: int) -> dict[str, Any]:
        """After a manual account cleanup, prove both symbols flat before releasing them."""
        with self._lock:
            pair = self._get(pair_id)
            if pair["state"] != "attention":
                raise StrategyRunError("계좌 확인이 필요한 실행만 이 방법으로 종료할 수 있습니다")
            engines = self._engines(pair)
            for one in engines:
                if one.run.mode == runs.SIM:
                    if self.legs._environment() != "paper":
                        raise StrategyRunError("모의 환경으로 접속한 뒤 계좌 비움을 확인하세요")
                    account = self.legs._account(one.run)
                    if not isinstance(account, dict) or not all(isinstance(account.get(key), list) for key in ("orders", "positions")):
                        raise StrategyRunError("계좌 주문·보유를 확인하지 못했습니다")
                    orders = [row for row in account["orders"] if row.get("code") == one.run.code]
                    positions = [row for row in account["positions"] if row.get("code") == one.run.code]
                    if any(_order_open(row) for row in orders) or any(float(row.get("quantity") or 0) != 0 for row in positions):
                        raise StrategyRunError(f"{one.run.code}: 모의계좌에 미체결·보유가 남아 있습니다. 계좌 화면에서 정리하세요")
                elif one.ledger.position.quantity or one.book.open_orders:
                    raise StrategyRunError("앱 시뮬레이션에 남은 보유·주문부터 정리해야 합니다")
            self._event(pair, "manual_flat_verified", at, reason="계좌 비움을 조회로 확인했습니다. 수동 정리 손익은 계좌 내역에서 확인하세요")
            for one in engines:
                with one.lock:
                    # Manual account trades are not strategy fills. Do not invent their P&L.
                    for order in one.book.open_orders:
                        one.book.close_cancelled(order, by="manual-flat")
                    one.ledger.position = runs.Position()
                    one.run.state = runs.PAUSED
                self.legs.end(one.run.id, at)
            pair.update(state="stopped", enabled=False, final_net=None, restored=True,
                        note="계좌 비움을 확인하고 종료했습니다. 수동 정리 손익은 계좌 내역을 확인하세요")
            self._save()
            return self._row(pair)

    def stop(self, pair_id: str, at: int) -> dict[str, Any]:
        with self._lock:
            pair = self._get(pair_id)
            if pair["state"] == "ready":
                pair.update(state="stopped", enabled=False, note="시작 전에 중지했습니다")
                self._save()
            elif pair["state"] != "stopped":
                if pair.get("restored"):
                    raise StrategyRunError("재시작 전 레그는 계좌 화면에서 확인·정리해야 합니다")
                self._close(pair, at, "사용자가 정리 후 중지를 요청했습니다", stop=True)
            return self._row(pair)

    def _close(self, pair: dict[str, Any], at: int, reason: str, *, stop: bool = False) -> None:
        if stop:
            pair["enabled"] = False
        if pair["state"] != "closing":
            pair.update(state="closing", close_at=at, note=reason)
            self._event(pair, "exit_signal", at, reason=reason)
            self._save()

    def beat(self, at: int) -> None:
        self.load()
        with self._lock:
            for pair in self._pairs.values():
                if pair["state"] in ("ready", "stopped") or pair.get("restored"):
                    continue
                try:
                    self._step(pair, at)
                except Exception:
                    logger.exception("스프레드 실행 %s 의 박자를 처리하지 못했습니다", pair["id"])
                    pair.update(state="attention", enabled=False, note="실행 오류가 발생했습니다. 레그·계좌를 확인하세요")
                    self._save()

    def _step(self, pair: dict[str, Any], at: int) -> None:
        config = SpreadConfig.model_validate(pair["config"])
        engines = self._engines(pair)
        if config.session_seconds and at - pair.get('heartbeat_at', 0) >= 30_000:
            pair['heartbeat_at'] = at
            self._event(pair, 'heartbeat', at, state=pair['state'], enabled=pair['enabled'],
                        session_end_at=pair.get('session_end_at'), last_sample=pair.get('last_sample'),
                        quote_age_seconds=(at - pair['last_sample']) / 1000 if pair.get('last_sample') else None,
                        entry_lock=pair.get('entry_lock'), pause_until=pair.get('pause_until'))
        if len(engines) != 2:
            pair.update(state="attention", enabled=False, note="두 레그 실행을 찾지 못했습니다")
            return
        # Same lock order as leg ids, so fills cannot race an entry preflight.
        with engines[0].lock, engines[1].lock:
            self._locked_step(pair, config, engines, at)

    def _locked_step(self, pair: dict[str, Any], config: SpreadConfig,
                     engines: list[engine.RunEngine], at: int) -> None:
        uncertain = any(one.run.state == runs.ATTENTION for one in engines)
        if uncertain:
            if pair["state"] != "attention":
                pair.update(state="attention", enabled=False, note="레그 주문·계좌 대사 결과를 확인해야 합니다")
                self._event(pair, "attention", at, reason=pair["note"])
                self._save()
            # Clean up only the peer whose orders and fills are known.
            for one in engines:
                if one.run.state not in runs.QUIET_STATES:
                    one.managed_unwind(at)
            return
        if any(one.run.state == runs.RECONCILING for one in engines):
            pair["note"] = "재연결 후 레그·계좌 대사를 기다립니다"
            return
        if pair["state"] == "attention":
            return
        net = self._net(engines)
        observed = self._observe(pair, config, engines, at, net)
        flat = all(not one.ledger.position.quantity and not one.book.open_orders for one in engines)
        if pair.get('session_end_at') and at >= pair['session_end_at']:
            self._close(pair, at, "3시간 운용 세션 종료" if config.session_seconds == 10800 else "운용 세션 종료", stop=True)
        if config.end_at is not None and at >= config.end_at:
            if config.session_seconds:
                pair.setdefault('entry_lock', '주문 운영시간 종료 · 세션 관찰 유지')
            else:
                self._close(pair, at, "설정한 종료 시각입니다", stop=True)
        liquidation = self._liquidation_net(engines, config, at) if config.entry_style in VOLATILITY_STYLES and not flat else None
        risk_net = liquidation if liquidation is not None else net
        if risk_net <= -config.max_loss:
            if config.session_seconds:
                pair.setdefault('entry_lock', '누적 손실 한도 · 신규 주문 차단')
            else:
                self._close(pair, at, "실행 손실 한도에 도달했습니다", stop=True)
        if config.session_seconds and flat and pair['entries'] >= config.max_entries:
            pair.setdefault('entry_lock', '최대 진입 횟수 · 세션 관찰 유지')
        if pair.get('entry_lock') and not flat:
            self._close(pair, at, pair['entry_lock'])
        if pair["state"] == "closing":
            done = [one.managed_unwind(at) for one in engines]
            if all(done):
                if pair.pop("cycle_open", False):
                    cycle = net - pair["cycle_base"]
                    pair["loss_streak"] = pair.get("loss_streak", 0) + 1 if cycle < -1e-9 else 0
                    pair["completed_trades"] = pair.get("completed_trades", 0) + 1
                    self._event(pair, "trade_closed", at, net=net, cycle_net=cycle,
                                loss_streak=pair["loss_streak"])
                    if config.loss_streak_limit and pair["loss_streak"] >= config.loss_streak_limit:
                        if config.loss_pause_seconds:
                            if pair['enabled'] and pair['loss_streak'] % config.loss_streak_limit == 0:
                                pair.update(pause_until=at + config.loss_pause_seconds * 1000,
                                            note="연속 손실 후 휴식 · 시세 수집 유지")
                                self._event(pair, 'risk_pause', at, reason=pair['note'],
                                            pause_until=pair['pause_until'], loss_streak=pair['loss_streak'])
                        else:
                            pair.update(enabled=False, note="연속 손실 한도에 도달해 실행을 종료했습니다")
                pair.pop("setup", None)
                next_state = 'observing' if pair.get('entry_lock') else 'paused' if pair.get('pause_until', 0) > at else 'cooldown'
                pair.update(final_net=net, state=next_state if pair["enabled"] else "stopped",
                            next_entry_at=at + config.cooldown_seconds * 1000)
                self._event(pair, "flat", at, net=net)
                if not pair["enabled"]:
                    for one in engines:
                        self.legs.end(one.run.id, at)
                self._save()
            elif at - pair["close_at"] > config.leg_timeout_seconds * 1000:
                pair["note"] = "정리가 지연되고 있습니다. 두 레그 미체결·연결 상태를 확인하세요"
            return
        if pair.get('entry_lock') and flat:
            pair.update(state='observing', note=pair['entry_lock'])
            pair.pop('setup', None)
            return
        if pair["state"] == "entering":
            wanted = [config.leg_a.quantity * pair["direction"], -config.leg_b.quantity * pair["direction"]]
            complete = all(one.ledger.position.quantity == qty and not one.book.open_orders
                           for one, qty in zip(engines, wanted, strict=True))
            failed = any(one.run.state in (runs.PAUSED, runs.ENDED) for one in engines)
            if complete:
                pair.update(state="holding", held_at=at, note="두 레그가 모두 체결됐습니다")
                self._event(pair, "hedged", at, net=net, quantities=wanted)
                self._save()
            elif failed or at - pair["entry_at"] >= config.leg_timeout_seconds * 1000:
                self._close(pair, at, "레그 거절·미체결 시간 초과로 체결분을 정리합니다")
            return
        if pair["state"] == "holding":
            wanted = [config.leg_a.quantity * pair["direction"], -config.leg_b.quantity * pair["direction"]]
            if any(one.ledger.position.quantity != qty or one.book.open_orders
                   for one, qty in zip(engines, wanted, strict=True)):
                if config.session_seconds:
                    pair['entry_lock'] = '레그 보유 비율 불일치 · 정리 후 주문 차단'
                self._close(pair, at, "레그 보유 비율이 달라져 정리합니다", stop=not config.session_seconds)
                return
            cycle = net - pair["cycle_base"]
            if config.entry_style in VOLATILITY_STYLES:
                if liquidation is not None:
                    pair['liquidation_cycle'] = liquidation - pair['cycle_base']
                    reason = trend_exit(pair, config, pair['liquidation_cycle'], pair['spread'] if observed else None)
                    if config.entry_style == 'relative_breakout' and observed and pair['direction'] * (pair['spread'] - pair['trade_failure_spread']) <= 0:
                        reason = '돌파 실패 · 기준 범위 복귀'
                    if reason:
                        self._close(pair, at, reason)
                        return
                elif cycle <= -pair['trade_risk']:
                    self._close(pair, at, '시세 지연 중 평가손실 한도')
                    return
            elif cycle <= -config.stop_loss or cycle >= config.take_profit:
                self._close(pair, at, "스프레드 손절" if cycle < 0 else "스프레드 익절")
                return
            if at - pair["held_at"] >= config.max_hold_seconds * 1000:
                self._close(pair, at, "최대 보유 시간에 도달했습니다")
                return
        if pair['state'] == 'paused':
            if at < pair.get('pause_until', 0):
                pair['note'] = '연속 손실 후 휴식 · 시세 수집 유지'
                return
            pair.pop('pause_until', None)
            pair.pop('setup', None)
            pair['state'] = 'watching'
            self._event(pair, 'risk_resume', at, reason='휴식 종료 · 새 조건으로 재평가')
        if pair["state"] == "cooldown":
            if at < pair["next_entry_at"]:
                return
            if pair["entries"] >= config.max_entries:
                if config.session_seconds:
                    pair.update(entry_lock='최대 진입 횟수 · 세션 관찰 유지', state='observing', note='최대 진입 횟수 · 세션 관찰 유지')
                else:
                    self._close(pair, at, "최대 진입 횟수에 도달했습니다", stop=True)
                return
            pair["state"] = "watching"
        if any(one.run.state in (runs.PAUSED, runs.ENDED) for one in engines):
            if config.session_seconds:
                pair['entry_lock'] = '레그 중지 · 주문 차단 후 계좌 확인 필요'
            self._close(pair, at, "레그 실행이 중지돼 함께 정리합니다", stop=not config.session_seconds)
            return
        if not observed:
            return
        z = pair["z"]
        if pair["state"] == "holding":
            if config.entry_style in VOLATILITY_STYLES:
                return
            if config.entry_style in ("pullback", "breakout"):
                z = (pair["spread"] - pair["entry_mean"]) / pair["entry_deviation"]
            if z is None:
                return
            if config.entry_style == "breakout":
                if pair["direction"] * z <= config.exit_z:
                    self._close(pair, at, "가격차 돌파의 힘이 약해져 두 레그를 정리합니다")
                return
            if config.stop_z and -pair["direction"] * z >= config.stop_z:
                self._close(pair, at, "가격차가 비상 청산 Z를 넘어 정리합니다")
            elif (pair["direction"] > 0 and z >= -config.exit_z) or (pair["direction"] < 0 and z <= config.exit_z):
                self._close(pair, at, "가격차가 청산 기준으로 수렴했습니다")
            return
        if z is None:
            return
        if pair.get("recovery_remaining", 0):
            pair["note"] = f"짧은 시세 공백 뒤 새 표본 {pair['recovery_remaining']}개를 더 확인합니다"
            return
        pair.update(state="watching", note="진입 Z 기준을 감시합니다")
        if not pair["enabled"] or not flat:
            return
        if config.entry_start_at is not None and at < config.entry_start_at:
            pair.pop("setup", None)
            pair["note"] = "설정한 시작 시각까지 시세만 수집합니다"
            return
        if config.entry_cutoff_at is not None and at >= config.entry_cutoff_at:
            pair.pop("setup", None)
            pair["note"] = "신규 진입 시간이 끝났습니다. 정리 시각까지 감시합니다"
            return
        if config.min_correlation and (pair.get("correlation") is None or pair["correlation"] < config.min_correlation):
            pair.pop("setup", None)
            self._entry_blocked(pair, at, "두 종목의 단기 움직임이 충분히 비슷하지 않아 기다립니다")
            return
        signal = {"z": z, "mean": pair["mean"], "deviation": pair["deviation"]}
        if config.entry_style in BOOK_STYLES:
            previous_setup = pair.get("setup", {}).get("at")
            signal_fn = relative_breakout_entry if config.entry_style == 'relative_breakout' else trend_entry if config.entry_style == 'trend_pullback' else breakout if config.entry_style == 'breakout' else reversal
            signal, reason = signal_fn(pair, config, at)
            current_setup = pair.get("setup", {}).get("at")
            if current_setup is not None and current_setup != previous_setup:
                pair.pop("last_block_reason", None)
                pair.update(round_trip_cost=None, convergence_value=None)
                self._event(pair, "signal_armed", at, reason=("5분 가격차 범위 돌파 · 지속 확인" if config.entry_style == 'relative_breakout' else "상대 추세 확인 · 눌림 대기" if config.entry_style == 'trend_pullback' else "가격차 범위 돌파 — 지속 움직임을 확인합니다" if config.entry_style == "breakout" else "가격차 이탈 확인 — 되돌림을 기다립니다"), z=z,
                            reference_mean=pair["mean"], reference_deviation=pair["deviation"])
            elif previous_setup is not None and current_setup is None:
                self._event(pair, "signal_cancelled", at, reason=reason, z=z)
            if signal is None:
                pair["note"] = reason
                return
        elif abs(z) < config.entry_z:
            return
        z = signal["z"]
        cost, edge = None, None
        if config.min_edge_cost_ratio or config.max_book_spread_ticks:
            cost, reason = entry_cost(engines, config, at)
            if reason:
                self._entry_blocked(pair, at, reason)
                return
            edge = ((signal["range"] if config.entry_style in ('breakout', *VOLATILITY_STYLES) else (abs(z) - config.exit_z) * signal["deviation"])
                    * config.leg_a.quantity * engines[0].run.multiplier)
            pair.update(round_trip_cost=cost, convergence_value=edge)
            if config.min_edge_cost_ratio and edge < cost * config.min_edge_cost_ratio:
                self._entry_blocked(pair, at, "최근 가격차 변동폭이 왕복 비용에 비해 작습니다" if config.entry_style in ('breakout', *VOLATILITY_STYLES) else "수렴을 가정한 가격차 이익이 예상 왕복 비용에 비해 작습니다")
                return
            if config.entry_style == "breakout" and config.take_profit < 2 * cost:
                self._entry_blocked(pair, at, "설정한 익절 금액이 왕복 비용의 2배보다 작습니다")
                return
        plan = None
        if config.entry_style in VOLATILITY_STYLES:
            plan = risk_plan(signal, config, cost, config.leg_a.quantity * engines[0].run.multiplier)
            if plan is None:
                self._entry_blocked(pair, at, '변동폭·비용을 포함한 거래 위험이 한도를 넘습니다')
                return
        direction = signal['direction'] if config.entry_style in VOLATILITY_STYLES else (1 if z > 0 else -1) if config.entry_style == "breakout" else (-1 if z > 0 else 1)
        sides = [paper.BUY, paper.SELL] if direction > 0 else [paper.SELL, paper.BUY]
        why = [one.managed_entry_reason(side, at) for one, side in zip(engines, sides, strict=True)]
        if any(why):
            self._entry_blocked(pair, at, " / ".join(value for value in why if value))
            return
        pair.pop("setup", None)
        pair.update(state="entering", direction=direction, entry_at=at, cycle_base=net, cycle_open=True,
                    entry_mean=signal["mean"], entry_deviation=signal["deviation"],
                    entries=pair["entries"] + 1, note="두 레그에 반대 방향으로 진입합니다")
        if plan:
            pair.update(trade_risk=plan['risk'], trade_target=plan['target'], trade_stop_spread=plan['stop_spread'],
                        trade_peak=-plan['entry_cost'], trade_trail=None, liquidation_cycle=None)
            if config.entry_style == 'relative_breakout':
                pair.update(trade_failure_spread=signal['failure_spread'], trade_boundary=signal['boundary'])
        self._event(pair, "entry_signal", at, z=z, spread=pair["spread"], sides=sides,
                    reference_mean=signal["mean"], reference_deviation=signal["deviation"],
                    round_trip_cost=cost, convergence_value=edge, correlation=pair.get("correlation"), risk_plan=plan,
                    breakout_boundary=signal.get('boundary'), failure_spread=signal.get('failure_spread'))
        self._save()
        for one, side in zip(engines, sides, strict=True):
            if one.managed_enter(side, at) is None:
                if one.run.state in runs.QUIET_STATES:
                    pair.update(state="attention", enabled=False, note="주문 결과가 불명확합니다. 계좌를 확인하세요")
                else:
                    self._close(pair, at, "한 레그의 진입이 거절돼 나머지 레그도 정리합니다")
                break
        self.legs.store.save([one.run for one in self.legs.engines()])
        self._save()

    @staticmethod
    def _liquidation_net(engines: list[engine.RunEngine], config: SpreadConfig, at: int) -> float | None:
        total = 0.0
        quote_times = []
        for one in engines:
            qty = one.ledger.position.quantity
            total += one.ledger.realized - one.ledger.fee
            if not qty:
                continue
            book = book_snapshot(one, at, config.quote_age_seconds)
            if book is None:
                return None
            quote_times.append(book['quoteAt'])
            price = book['bid1'] - config.slippage_ticks * one.tick if qty > 0 else book['ask1'] + config.slippage_ticks * one.tick
            total += one.ledger.unrealized(price) - abs(qty) * config.fee_per_contract
        if quote_times and max(quote_times) - min(quote_times) > config.quote_skew_seconds * 1000:
            return None
        return total

    def _entry_blocked(self, pair: dict[str, Any], at: int, reason: str) -> None:
        pair["note"] = reason
        counts = pair.setdefault("block_counts", {})
        counts[reason] = counts.get(reason, 0) + 1
        if pair.get("last_block_reason") != reason:
            self._event(pair, "entry_blocked", at, reason=reason, z=pair.get("z"),
                        round_trip_cost=pair.get("round_trip_cost"), convergence_value=pair.get("convergence_value"))
            pair["last_block_reason"] = reason

    def _observe(self, pair: dict[str, Any], config: SpreadConfig, engines: list[engine.RunEngine],
                 at: int, net: float) -> bool:
        """Keep sampling during cooldown/entry/exit as well as while watching."""
        fresh = all(one.feed == "live" for one in engines)
        if config.entry_style == "threshold":
            fresh = fresh and all(one.book.last_price is not None and one.last_trade_at > 0
                                  and 0 <= at - one.last_trade_at <= config.quote_age_seconds * 1000 for one in engines)
            fresh = fresh and abs(engines[0].last_trade_at - engines[1].last_trade_at) <= config.quote_skew_seconds * 1000
        if not fresh:
            pair["note"] = "두 레그의 최신 시세·시각 동기화를 기다립니다"
            return False
        if not all(self.is_open("overseas", one.run.code, "overseas") for one in engines):
            pair["note"] = "두 레그가 모두 거래 중일 때 판정합니다"
            return False
        if at - pair.get("last_sample", 0) < config.sample_seconds * 1000:
            return False
        prices = []
        if config.entry_style in BOOK_STYLES:
            books = [book_snapshot(one, at, config.quote_age_seconds) for one in engines]
            if any(book is None for book in books) or abs(books[0]["quoteAt"] - books[1]["quoteAt"]) > config.quote_skew_seconds * 1000:
                pair["note"] = "최신 양쪽 매수·매도 호가를 기다립니다"
                return False
            prices = [(book["bid1"] + book["ask1"]) / 2 for book in books]
        else:
            prices = [float(one.book.last_price) for one in engines]
        if any(not math.isfinite(price) for price in prices):
            return False
        spread = prices[0] - config.beta * prices[1]
        if not math.isfinite(spread):
            return False
        history = self._history.setdefault(pair["id"], deque(maxlen=config.lookback))
        price_history = self._price_history.setdefault(pair["id"], deque(maxlen=config.lookback))
        times = self._time_history.setdefault(pair["id"], deque(maxlen=config.lookback))
        gap = at - pair["last_sample"] if pair.get("last_sample") else 0
        short_gap = max(3 * config.sample_seconds, config.quote_age_seconds) * 1000
        reset_gap = config.history_gap_seconds * 1000 if config.history_gap_seconds else short_gap
        if config.entry_style in BOOK_STYLES and gap > reset_gap:
            history.clear()
            price_history.clear()
            times.clear()
            pair.pop("setup", None)
            pair.pop('trend_history', None)
            pair.pop('trend_open_bar', None)
            pair["recovery_remaining"] = 0
            self._event(pair, "sample_reset", at, reason="긴 시세 공백 뒤 기준 표본을 다시 모읍니다", gap_seconds=gap / 1000)
        elif config.entry_style in BOOK_STYLES and gap > short_gap:
            pair.pop("setup", None)
            pair["recovery_remaining"] = config.recovery_samples
            self._event(pair, "sample_recovery", at, reason="기준 표본을 유지하고 새 시세를 확인합니다", gap_seconds=gap / 1000)
        # Retaining short gaps must never turn a five-minute baseline into old data.
        if config.history_gap_seconds:
            oldest = at - config.lookback * config.sample_seconds * 1500
            while times and times[0] < oldest:
                times.popleft()
                history.popleft()
                price_history.popleft()
        z = None
        mean = statistics.fmean(history) if len(history) == config.lookback else None
        deviation = statistics.pstdev(history) if mean is not None else None
        if deviation is not None and deviation > 1e-12:
            z = (spread - mean) / deviation
        if z is not None and not math.isfinite(z):
            z = None
        # The current observation is not part of its own reference window.
        correlation = return_correlation(list(price_history)) if config.min_correlation else None
        pair.update(range_high=max(history) if mean is not None else None,
                    range_low=min(history) if mean is not None else None)
        history.append(spread)
        price_history.append((prices[0], prices[1]))
        times.append(at)
        pair.update(last_sample=at, spread=spread, z=z, mean=mean, deviation=deviation, correlation=correlation,
                    price_a=prices[0], price_b=prices[1])
        if config.entry_style in VOLATILITY_STYLES:
            update_trend(pair, config, at, spread)
        if pair.get("recovery_remaining", 0):
            pair["recovery_remaining"] -= 1
        position_z = ((spread - pair["entry_mean"]) / pair["entry_deviation"]
                      if config.entry_style in ("pullback", "breakout") and pair.get("cycle_open") and pair.get("entry_deviation") else None)
        pair["position_z"] = position_z
        sample = {"time": at, "price_a": prices[0], "price_b": prices[1], "spread": spread, "z": z, "net": net,
                  "correlation": correlation, "position_z": position_z, "mean": mean, "deviation": deviation}
        if config.entry_style in BOOK_STYLES:
            sample["books"] = [{key: book[key] for key in ("bid1", "ask1", "bid1Qty", "ask1Qty", "quoteAt")} for book in books]
        if config.entry_style in VOLATILITY_STYLES:
            sample.update(trend_atr=pair.get('trend_atr'), trend_efficiency=pair.get('trend_efficiency'),
                          trend_direction=pair.get('trend_direction'), liquidation_cycle=pair.get('liquidation_cycle'))
        self._event(pair, "sample", at, **{key: value for key, value in sample.items() if key != "time"})
        self._samples.setdefault(pair["id"], deque(maxlen=120)).append(sample)
        if z is None:
            pair["note"] = "기준 표본이 부족하거나 가격차의 표준편차가 0입니다"
            if pair["state"] in ("warming", "watching"):
                pair["state"] = "warming"
        return True

    def export(self, pair_id: str) -> bytes:
        with self._lock:
            pair = self._get(pair_id)
            rows = []
            for path in (self.log_path.with_suffix(".jsonl.old"), self.log_path,
                         self.legs.journal.path.with_suffix(".jsonl.old"), self.legs.journal.path):
                if not path.is_file():
                    continue
                with path.open(encoding="utf-8") as file:
                    for line in file:
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if row.get("pair_id") == pair_id or row.get("runId") in pair["leg_ids"]:
                            rows.append(row)
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("config.json", json.dumps(pair["config"], ensure_ascii=False, indent=2))
                archive.writestr("snapshot.json", json.dumps(self._row(pair), ensure_ascii=False, indent=2))
                archive.writestr("records.jsonl", "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
                samples = [row for row in rows if row.get("kind") == "sample"]
                csv_out = io.StringIO(newline="")
                writer = csv.DictWriter(csv_out, fieldnames=["time", "price_a", "price_b", "spread", "z", "net", "correlation", "position_z"], extrasaction="ignore")
                writer.writeheader()
                writer.writerows(samples)
                archive.writestr("samples.csv", "\ufeff" + csv_out.getvalue())
                report = execution_report(pair["config"], self._row(pair), rows)
                archive.writestr("report.json", json.dumps(report, ensure_ascii=False, indent=2))
                archive.writestr("report.md", report_markdown(report))
            return output.getvalue()


class SpreadPump(RunPump):
    def _send(self, payload: Any, made: Any, at: int) -> bool:
        # The existing strategy broadcast replaces that screen's run list.
        # Spread rows are polled by their own screen and must not overwrite it.
        return False


class SpreadRuntime:
    def __init__(self, service: SpreadService) -> None:
        self.service = service
        self.pump = SpreadPump(service.legs, name="spread-strategy-engine")
        self.task: asyncio.Task[None] | None = None
        self.work: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.pump.start()
        self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.work:
            # Cancelling an asyncio await does not stop a broker call in a worker.
            # Wait for it before tearing down the feed/account connections.
            await asyncio.gather(self.work, return_exceptions=True)
        await self.pump.stop()

    async def _loop(self) -> None:
        while True:
            try:
                self.work = asyncio.create_task(asyncio.to_thread(self.service.beat, int(time.time() * 1000)))
                await asyncio.shield(self.work)
            except Exception:
                logger.exception("스프레드 감시 박자를 처리하지 못했습니다")
            await asyncio.sleep(0.5)


service = SpreadService()
