"""Reproducible summaries of observed spread executions, never invented trades."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any


def execution_report(config: dict[str, Any], snapshot: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(records, key=lambda row: row.get("time", 0))
    samples = [row for row in rows if row.get("kind") == "sample"]
    closed = [row for row in rows if row.get("kind") == "trade_closed"]
    pnl = [float(row["cycle_net"]) for row in closed]
    wins = sum(value > 1e-9 for value in pnl)
    losses = sum(value < -1e-9 for value in pnl)
    gain, loss = sum(max(value, 0) for value in pnl), -sum(min(value, 0) for value in pnl)
    peak = drawdown = 0.0
    for row in rows:
        if row.get("kind") in ("sample", "trade_closed", "flat") and row.get("net") is not None:
            value = float(row["net"])
            peak = max(peak, value)
            drawdown = max(drawdown, peak - value)
    volatility = config.get('entry_style') in ('trend_pullback', 'relative_breakout')
    first_ready = next((row["time"] for row in samples if row.get("z") is not None
                        and (not volatility or (row.get('trend_atr') or 0) > 0)), None)
    started = snapshot.get("started_at")
    ended = max((row.get("time", 0) for row in rows), default=started or 0)
    kinds = Counter(row.get("kind", "unknown") for row in rows)
    fills = [row for row in rows if row.get("kind") == "fill"]
    return {
        "schema": 1, "pair_id": snapshot["id"], "name": config["name"],
        "execution_mode": snapshot.get("mode"), "state": snapshot["state"],
        "quote_environment": snapshot.get('quote_environment'),
        "broker_execution": snapshot.get('mode') == 'sim',
        "return_percent": snapshot['net'] / config['cash'] * 100 if snapshot.get('net') is not None and config.get('cash') else None,
        "started_at": started, "observed_until": ended,
        "observed_seconds": round((ended - started) / 1000, 1) if started else None,
        "session_end_at": snapshot.get('session_end_at'),
        "heartbeat_records": kinds['heartbeat'],
        "last_heartbeat_at": snapshot.get('heartbeat_at'),
        "last_valid_quote_at": samples[-1]['time'] if samples else None,
        "loss_pauses": kinds['risk_pause'], "pause_resumptions": kinds['risk_resume'],
        "entry_lock": snapshot.get('entry_lock'),
        "valid_sample_seconds": len(samples) * config.get('sample_seconds', 5),
        "warmup_seconds": round((first_ready - started) / 1000, 1) if first_ready and started else None,
        "samples": len(samples), "valid_z_samples": sum(row.get("z") is not None for row in samples),
        "entry_signals": kinds["entry_signal"], "completed_trades": len(pnl), "wins": wins, "losses": losses,
        "win_rate": wins / len(pnl) if pnl else None,
        "profit_factor": gain / loss if loss > 0 else None,
        "closed_net": sum(pnl), "marked_net": snapshot.get("net"),
        "max_observed_drawdown": round(drawdown, 6),
        "fees_estimated": sum(float(row.get("fee") or 0) for row in fills), "fill_records": len(fills),
        "order_result_records": kinds["order"],
        "signal_candidates": kinds["signal_armed"], "signal_cancellations": kinds["signal_cancelled"],
        "history_resets": kinds["sample_reset"], "short_gap_recoveries": kinds["sample_recovery"],
        "entry_block_checks": snapshot.get("block_counts", {}),
        "block_reason_changes": dict(Counter(row.get("reason", "") for row in rows if row.get("kind") == "entry_blocked")),
        "remaining_positions": [{"code": leg["code"], "quantity": leg["quantity"], "open_orders": leg["open_orders"]}
                                for leg in snapshot.get("legs", [])],
        "limitations": [
            "App paper fills are simulated; sim fills originate from the NH mock account.",
            "Fees use the configured assumption. Account equity must be reconciled separately.",
            "Drawdown uses recorded marks and can miss moves between samples.",
            "An unfinished run is an interim observation, not a completed three-hour test.",
            "No trades means profitability is unmeasured; zero P/L is not proof of safety.",
            "Heartbeat duration measures service availability, not continuous market data or trading hours.",
        ],
    }


def report_markdown(report: dict[str, Any]) -> str:
    def stamp(at: int | None) -> str:
        return datetime.fromtimestamp(at / 1000, timezone(timedelta(hours=9))).isoformat(timespec="seconds") if at else "미측정"

    mode = "NH 해외 모의계좌" if report["execution_mode"] == "sim" else "실전 시세 · 가상 체결" if report.get('quote_environment') == 'real' else "앱 시뮬레이션"
    win = "미측정" if report["win_rate"] is None else f"{report['win_rate']:.1%}"
    return "\n".join([
        f"# {report['name']} — 실행 기록", "", f"실행 방식: **{mode}** / 상태: `{report['state']}`",
        f"관측 구간: {stamp(report['started_at'])} ~ {stamp(report['observed_until'])}", "",
        "| 항목 | 관측값 |", "|---|---:|",
        f"| 준비 시간 | {report['warmup_seconds']}초 |",
        f"| 운용 기록 / 유효 표본 환산 | {report['observed_seconds']}초 / {report.get('valid_sample_seconds')}초 |",
        f"| 감시 박자 / 손실 휴식 | {report.get('heartbeat_records', 0)}회 / {report.get('loss_pauses', 0)}회 |",
        f"| 전체 표본 / Z 계산 표본 | {report['samples']} / {report['valid_z_samples']} |",
        f"| 진입 신호 / 완료 거래 | {report['entry_signals']} / {report['completed_trades']} |",
        f"| 이익 / 손실 / 승률 | {report['wins']} / {report['losses']} / {win} |",
        f"| 완료 거래 순손익 | {report['closed_net']} |",
        f"| 미실현 포함 추정 순손익 | {report['marked_net']} |",
        f"| 관측한 최대 낙폭 | {report['max_observed_drawdown']} |",
        f"| 체결 기록 | {report['fill_records']} |",
        f"| 짧은 공백 복구 / 기준 초기화 | {report['short_gap_recoveries']} / {report['history_resets']} |", "",
        "수수료는 설정값입니다. " + ("계좌 수익률은 시작·종료 평가금액으로 확인하세요." if report['execution_mode'] == 'sim' else "이 기록은 가상 체결이며 계좌 주문·체결 및 대회 수익률이 아닙니다."),
        "표본 사이의 손실은 최대 낙폭에 빠질 수 있습니다. 진행 중인 실행은 중간 기록입니다.",
        "거래가 없으면 수익성은 미측정입니다. 기능 시험과 전략 성과를 구분해 제출하세요.", "",
        "## 진입을 보류한 이유", "",
        *[f"- {reason}: {count}회 판정" for reason, count in report["entry_block_checks"].items()], "",
    ])
