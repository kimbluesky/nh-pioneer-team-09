"""Local controls for the already released, single-contract November 20 NH paper session."""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from backend.nh_api.session import replay_enabled
from backend.sessions.hours import KST, now_kst

router = APIRouter(prefix="/api/strategy/hynix-paper", tags=["strategy"])
DAY = date(2026, 11, 20)
TICKER = "A506C000"
RELEASE = "3102ad1b197ef413d5fa10d42b88e083be733c5e8ba93d68a9d30a225c1d6414"
START_PROGRAM = "d2dac981242e1a4e4eed373452cba6d278584aa9a803622bd3d99fe24881dbfc"
SESSION = Path("week05/results/hynix_paper_20261120")
TASKS = {"run": "NH-Week05-Hynix-Paper-20261120", "probe": "NH-Week05-Hynix-Health-20261120"}
SCRIPTS = {"run": "hynix_paper_standby.py", "probe": "hynix_paper_health.py"}
EVENTS = ("signal", "paper_exit_signal", "order_intent", "ack", "confirmed_fill", "order_refused",
          "ack_unknown", "ack_recovered_from_history", "unfilled_terminal", "cancel_intent", "session_end")

# The task service returns numerical states independently of Windows' display language.
# The interactive user owns both tasks. All arguments to Run() come from the registered task.
_TASK = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$scheduler = New-Object -ComObject Schedule.Service
$scheduler.Connect()
$task = $scheduler.GetFolder('\').GetTask($env:NH_DESK_TASK)
$d = $task.Definition
$a = $d.Actions.Item(1)
$root = $a.WorkingDirectory
$script = Join-Path $root ('week05\code\' + $env:NH_DESK_SCRIPT)
$arg = '-B "' + $script + '"'
if ($env:NH_DESK_KIND -eq 'run') { $arg += ' run' }
$matchesAction = $d.Actions.Count -eq 1 -and $a.Type -eq 0 -and $a.Path -eq (Join-Path $root '.venv\Scripts\pythonw.exe') -and $a.Arguments -ceq $arg -and $d.Settings.MultipleInstances -eq 2 -and $d.Principal.RunLevel -eq 0
$started = $false
if ($env:NH_DESK_START -eq 'yes') {
    if (!$matchesAction -or !$task.Enabled -or $root -ne $env:NH_DESK_ROOT) { throw 'Different task' }
    if ($task.State -notin @(2,4)) { [void]$task.Run($null); $started=$true }
}
$next = $task.NextRunTime
$nextText = if ($next.Year -gt 2000) { $next.ToString('yyyy-MM-ddTHH:mm:sszzz') } else { $null }
[ordered]@{root=$root; verified=$matchesAction; enabled=$task.Enabled; state=[int]$task.State; next=$nextText; started=$started} | ConvertTo-Json -Compress
"""


def task_info(kind="run", start_root=None):
    if os.name != "nt":
        return {}
    env = dict(os.environ, NH_DESK_TASK=TASKS[kind], NH_DESK_SCRIPT=SCRIPTS[kind], NH_DESK_KIND=kind,
               NH_DESK_START="yes" if start_root else "no", NH_DESK_ROOT=str(start_root or ""))
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        process = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-Command", _TASK],
                                 env=env, capture_output=True, timeout=6, creationflags=subprocess.CREATE_NO_WINDOW)
        if process.returncode == 0:
            return json.loads(process.stdout.decode("utf-8-sig"))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return {}


def project_root(task):
    # A packaged EXE may live outside the checkout. Its registered local worker still knows the checkout.
    base = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
    candidates = list(base.parents)[:4]
    if task.get("verified") and task.get("root"):
        candidates.insert(0, Path(task["root"]))
    return next((p for p in candidates if (p / SESSION / "dawn_release.json").is_file()), None)


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def optional(path, field, expected):
    try:
        item = load(path)
        return item if isinstance(item, dict) and item.get(field) == expected else {}
    except (OSError, ValueError):
        return {}


def verified_models(root):
    """Read the exact NPZ weights and the same validation/test receipts used by the worker."""
    folder = root / SESSION
    research = folder.parent / "hynix_tuning"
    trained = folder.parent / "hynix_stopping"
    dawn_path = folder / "dawn_release.json"
    if digest(dawn_path) != RELEASE:
        raise ValueError("release")
    if digest(root / "week05/code" / SCRIPTS["run"]) != START_PROGRAM:
        raise ValueError("paper runner")
    dawn = load(dawn_path)
    order_path = folder / "execution_release_v2.json"
    execution = research / "execution"
    for file, key in ((order_path, "order_release_sha256"), (root/"week05/code/hynix_dawn.py", "launcher"),
                      (root/"week05/code/hynix_tuning_shadow.py", "observer"), (research/"policies.json", "weights"),
                      (execution/"results.json", "tuning_results"), (execution/"selection.json", "tuning_selection")):
        if digest(file) != dawn[key]:
            raise ValueError(key)
    broker = load(order_path)
    exit_lock = trained / "exit/selection.json"
    if digest(exit_lock) != broker["selection_sha256"]:
        raise ValueError("exit selection")
    exits = load(exit_lock)
    if digest(trained / "selection.json") != exits["sources"]["entry_lock"]:
        raise ValueError("entry selection")
    entry, exit_name = broker["entry"], broker["exit"]
    en, ex = load(trained/"selection.json")["rows"][entry], exits["candidates"][exit_name]
    if (digest(trained/entry/"weights.npz") != en["weights"] or digest(trained/"exit"/exit_name/"exit.npz") != ex["weights"]
            or (broker["environment"], broker["instrument"]["symbol"], broker["quantity"]) != ("paper", TICKER, 1)):
        raise ValueError("weights")
    test, validation = load(execution/"results.json"), load(execution/"selection.json")
    history = []
    for key in (dawn["official_policy"], dawn["quote_only_candidate"]):
        a, b = validation["rows"][key]["summary"], test["rows"][key]["summary"]
        history.append(dict(validation=a["net_krw"], test=b["net_krw"], wins=b["wins"], days=b["days"]))
    return dawn, dict(entry=entry, exit=exit_name, entryDays=en["training_days"], exitDays=ex["training_days"],
                      frozen=dawn["sealed_kst"], history=history)


def phase(now):
    if now.date() < date(2026, 11, 19):
        return "closed"
    if now < datetime(2026, 11, 20, 0, 5, tzinfo=KST):
        return "before"
    if now < datetime(2026, 11, 20, 8, 35, tzinfo=KST):
        return "overnight"
    if now < datetime(2026, 11, 20, 16, tzinfo=KST):
        return "day"
    return "closed"


def recent_events(path, release):
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.4)) as db:
        db.execute("BEGIN")
        state = db.execute("SELECT v FROM state WHERE id=1").fetchone()
        if not state or json.loads(state[0]).get("release") != release:
            return []
        slots = ",".join("?" for _ in EVENTS)
        rows = db.execute(f"SELECT id,kst,kind,data FROM events WHERE kind IN ({slots}) ORDER BY id DESC LIMIT 20", EVENTS)
        items = []
        for i, at, kind, data in rows:
            row = json.loads(data)
            keep = {k: row[k] for k in ("action", "side", "role", "quantity", "price", "exit", "phase",
                                        "order_id", "broker_fill_time") if k in row}
            items.append(dict(id=i, at=at, kind=kind, **keep))
        return items


def connection(folder, release, session, night, evening, now):
    probe = optional(folder/"preflight.json", "release", release)
    checks = {k: v.get("ok") for k, v in probe.get("operations", {}).items()}
    seen = probe.get("observed_kst")
    if evening.get("checked_kst") and (not seen or evening["checked_kst"] > seen):
        seen = evening["checked_kst"]
        checks = {k: v.get("ok") for k, v in evening.get("checks", {}).items()}
    overnight_seen = night.get("checked_kst")
    if overnight_seen and (not seen or overnight_seen > seen):
        seen, checks = overnight_seen, night.get("checks", {})
    quote, position, orders = (checks.get(k) for k in ("paper_chart", "paper_position", "paper_order_history"))
    if (session.get("as_of_kst") and (not seen or session["as_of_kst"] >= seen)
            and ("account_ok" in session or "quotes_ok" in session)):
        seen = session["as_of_kst"]
        quote, position, orders = session.get("quotes_ok"), session.get("account_ok"), session.get("account_ok")
    late = bool(seen and phase(now) == "day" and (now - datetime.fromisoformat(seen)).total_seconds() > 120)
    return dict(at=seen, quote=quote, position=position, orders=orders, stale=late)


def desk_state(now):
    task = task_info()
    root = project_root(task)
    if root is None:
        raise HTTPException(503, "이 PC에서 11월 20일 전략 폴더를 찾지 못했습니다.")
    try:
        dawn, model = verified_models(root)
    except (OSError, KeyError, ValueError, TypeError):
        raise HTTPException(409, "확정한 모델 파일과 현재 파일이 일치하는지 확인해 주세요.") from None
    folder, release = root / SESSION, dawn["order_release_sha256"]
    session = optional(folder/"status.json", "release_sha256", release)
    night = optional(folder/"overnight_status.json", "release", RELEASE)
    evening = optional(folder/"preopen_runtime.json", "release_sha256", release)
    shadow = optional(folder/"tuned_forward/status.json", "comparison_release", RELEASE)
    candidate = shadow.get("strategies", {}).get(dawn["quote_only_candidate"], {})
    try:
        events = recent_events(folder/"session.sqlite3", release)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        events = None
    stage = phase(now)
    available = bool(task.get("verified") and task.get("enabled") and Path(task["root"]).resolve() == root.resolve())
    task_state = {2: "queued", 3: "ready", 4: "running"}.get(task.get("state"), "unknown") if available else "unavailable"
    action = None
    if available and not replay_enabled() and session.get("phase") not in ("finished_flat", "finished_no_trade"):
        if stage == "before" and task_state == "running":
            action = "probe"
        elif stage in ("before", "overnight", "day") and task_state == "ready":
            action = "run"
    live = {k: session.get(k) for k in ("as_of_kst", "phase", "confirmed_fills", "round_trips", "local_position",
            "unresolved_order", "estimated_net_from_confirmed_fills_krw", "five_minute_bars", "entry_decisions")}
    return root, dict(now=now.isoformat(), phase=stage, task=task_state, next=task.get("next"), action=action,
                      model=model, connection=connection(folder, release, session, night, evening, now), live=live, events=events,
                      started=evening.get("started_kst"),
                      heartbeat=evening.get("heartbeat_kst") if stage == "before" else night.get("heartbeat_kst"),
                      shadow=dict(outcome=candidate.get("outcome"),
                      at=shadow.get("as_of_kst"), net=candidate.get("simulated_net_krw")))


def archive_folder():
    root = Path(__file__).resolve().parents[2]
    folder = root / "week05/results"
    if (folder / "study.json").is_file() and not (root / SESSION / "dawn_release.json").is_file():
        return folder
    return None


def archive_state(folder):
    """Display the submitted session on a fresh copy of the host app."""
    study = load(folder / "study.json")
    journal = load(folder / "strategy-runs.json")
    run, = journal["runs"]
    if digest(folder / "trades.jsonl") != run["trade_log_sha256"]:
        raise ValueError("fill journal")
    for name, model in study["models"].items():
        if digest(folder / "models" / f"{name}.npz") != model["sha256"]:
            raise ValueError(name)
    if run["environment"] != "paper" or run["code"] != TICKER or not run["broker_final_flat"]:
        raise ValueError("paper session")
    with (folder / "trades.jsonl").open(encoding="utf-8") as stream:
        fills = [json.loads(line) for line in stream]
    combined = sorted(journal["events"] + fills, key=lambda e: e["id"], reverse=True)
    items = [dict(id=e["id"], at=e["time"], kind=e["kind"], **e["event"])
             for e in combined if e["kind"] in EVENTS]
    last = next(e for e in combined if e["kind"] == "five_minute_heartbeat")
    query = last["event"]
    models = study["models"]
    selection = study["selection"]
    table = {row["model"]: row for row in selection["comparison"]}
    history = []
    for key in (selection["selected"], selection["highest_test_candidate"]):
        row = table[key]
        history.append(dict(validation=row["validation_net_krw"], test=row["test_net_krw"],
                            wins=row["test_wins"], days=row["test_days"]))
    paper = study["paper"]
    candidate = paper["candidate_monitor"]
    return dict(archived=True, now=run["observed_kst"], phase="closed", task="unknown", next=None,
                action=None, heartbeat=last["time"], started=None,
                model=dict(entry=models["entry"]["name"], exit=models["exit"]["name"],
                           entryDays=models["entry"]["days"], exitDays=models["exit"]["days"],
                           frozen=paper["strategy_locked_kst"], history=history),
                connection=dict(at=last["time"], quote=query["quotes_ok"], position=query["account_ok"],
                                orders=query["account_ok"], stale=False),
                live=paper["account_reconciliation"], events=items[:20],
                shadow=dict(at=candidate["as_of_kst"], net=candidate["net_krw"], outcome=candidate["outcome"]))


@router.get("")
def status():
    folder = archive_folder()
    if folder:
        try:
            return archive_state(folder)
        except (OSError, ValueError, KeyError, StopIteration):
            raise HTTPException(409, "제출본의 모델이나 체결 기록을 확인해 주세요.") from None
    return desk_state(now_kst())[1]


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["probe", "run"]


@router.post("/start")
def start(ask: TaskRequest, request: Request):
    if archive_folder():
        raise HTTPException(409, "11월 20일 모의거래가 마감되었습니다.")
    if request.headers.get("x-nh-paper-desk") != "1" or request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "NH 앱의 전략 화면에서 실행해 주세요.")
    root, state = desk_state(now_kst())
    if ask.action == "run" and state["task"] in ("running", "queued") and state["phase"] in ("before", "overnight", "day"):
        return {"message": "같은 모의거래 작업이 이미 실행 중입니다."}
    manual_probe = ask.action == "probe" and state["phase"] == "before" and state["action"] in ("run", "probe")
    if (state["action"] != ask.action and not manual_probe) or phase(now_kst()) != state["phase"]:
        raise HTTPException(409, "현재 시각의 실행 상태가 바뀌었습니다. 새로고침해 주세요.")
    launched = task_info(ask.action, root)
    if not launched.get("verified") or not launched.get("enabled"):
        raise HTTPException(503, "Windows에서 해당 예약 작업을 시작하지 못했습니다.")
    message = "NH 연결 점검을 요청했습니다." if ask.action == "probe" else "예약된 모의거래 프로그램에 시작을 요청했습니다."
    return {"message": message if launched["started"] else "해당 작업이 이미 실행 중입니다."}
