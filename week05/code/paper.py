"""Order and reconciliation logic used for the dated SK hynix NH paper run.

With the official app on PYTHONPATH, these classes expose its actual chart and account adapters.
The submission's offline entry point is reproduce.py.
"""

from datetime import date, datetime, timedelta
import json
import sqlite3

from backend.config import DEFAULT_BASE_URLS
from backend.domestic_futureoption import (
    domestic_account as account,
    domestic_futureoption_functions as api,
)
from backend.master.master_files import MASTER_FILES_BY_NAME
from backend.master.master_store import load_master
from backend.orders import order_no
from nh_quotes import KST, RAW_FIELDS, Unavailable, canonical, nh_client, values
from market import CLOCK, FEE, MULT, SIDES, allowed, due, exchange_cash, state, tick
from policy import checkpoints, exit_state


DAY = date(2026, 11, 20)
CODE = "A506C000"
FRONT = "15000000"


def contract():
    m = load_master(MASTER_FILES_BY_NAME["stk_fut_opt"])
    rows = (
        []
        if m.table is None
        else [dict(zip(m.table.fields, r, strict=True)) for r in m.table.rows if CODE in r]
    )
    good = [
        r
        for r in rows
        if r["series"] == CODE
        and r["fo_tp"] == "F"
        and r["under_name"] == "S50"
        and r["exp_ym"] == "20261210"
        and r["ctrt_size"] == "10"
        and r["cme_tp"] == "day"
        and "SKH" in r["name_eng"]
    ]
    if len(good) != 1:
        raise Unavailable("December SK hynix contract unavailable in NH master")
    return dict(symbol=CODE, expiry="2026-12-10", multiplier=10, master_date=m.stamp)


class Charts:
    def __init__(self, environment):
        self.environment = environment
        self.conn = nh_client(environment)

    def bars(self, day, continuous=False):
        stamp = day.strftime("%Y%m%d")
        req = dict(
            sym=CODE,
            inq_strt_dt=stamp,
            inq_ed_dt=stamp,
            strt_tm="000000",
            ed_tm="235959",
            dcnt="5",
            nght_tp="1",
            cidx_yn=str(int(continuous)),
            fut_cidx_exp_mod="0",
            nxt_key="99999999",
            req_qty="9999",
        )
        raw = self.conn.call("domestic_chart-minute", req, environment=self.environment).body
        rows = raw.get("Occurs1") or []
        if (
            len(rows) >= 2000
            or not str(raw.get("nrec", "")).strip().isdigit()
            or int(raw["nrec"]) != len(rows)
        ):
            raise Unavailable("incomplete chart")
        if rows and str(raw.get("sym", "")).strip() != (FRONT if continuous else CODE):
            raise Unavailable("unexpected quote symbol")
        bars = {}
        for row in rows:
            fields = [row[k] for k in RAW_FIELDS]
            dd, tm = fields[:2]
            if (
                dd != stamp
                or len(tm) != 6
                or not tm.isdigit()
                or tm[4:] != "00"
                or int(tm[2:4]) >= 60
                or int(tm[2:4]) % 5
            ):
                raise Unavailable("invalid chart timestamp")
            v = values(fields)
            m = int(tm[:2]) * 60 + int(tm[2:4])
            bar = (*(float(vv) for vv in v[:4]), int(v[4]))
            if m in bars and bars[m] != bar:
                raise Unavailable("conflicting chart timestamps")
            bars[m] = bar
        return bars

    def close(self):
        self.conn.session.close()


class PaperBroker(Charts):
    def __init__(self):
        self.instrument = contract()
        super().__init__("paper")

    def _paper(self, method, *args, **kwargs):
        if (
            self.environment != "paper"
            or self.conn.session.is_replay
            or self.conn.session.base_url("paper") != DEFAULT_BASE_URLS["paper"]
        ):
            raise Unavailable("invalid demo connection")
        return method(*args, api=self.conn, environment="paper", **kwargs)

    def pages(self, kind, day, pending=False):
        key, seen, found = "", set(), []
        for _ in range(100):
            if key in seen:
                raise Unavailable("repeated account cursor")
            seen.add(key)
            if kind == "positions":
                raw = self._paper(api.open_interest, next_key=key)
                parsed = account.positions(raw)
            else:
                raw = self._paper(
                    api.order_history,
                    exec_div=api.PENDING if pending else api.ALL,
                    trd_tm_div_cd=api.DAY_SESSION,
                    next_key=key,
                )
                parsed = account.order_history(raw)
            rows = raw.get("Occurs1")
            count = str(raw.get("nrec", "")).strip()
            if not count.isascii() or not count.isdigit():
                raise Unavailable("unreadable account count")
            expected = int(count)
            if expected == 0 and rows is None:
                rows = []
            if (
                not isinstance(rows, list)
                or len(rows) != expected
                or len(parsed[kind]) != expected
                or any(not isinstance(r, dict) for r in rows)
            ):
                raise Unavailable("unreadable account page")
            for r in parsed[kind]:
                if r["code"] == CODE and (
                    kind == "positions" or r.get("date") == str(day) or (pending and not r.get("date"))
                ):
                    found.append(r)
                elif r["code"] == CODE and kind == "orders" and not r.get("date"):
                    raise Unavailable("undated order history")
            key = parsed["nextKey"]
            if not key:
                return found
        raise Unavailable("account pagination incomplete")

    def position(self):
        pos = []
        for r in self.pages("positions", DAY):
            if (
                r.get("quantity") is None
                or r.get("quantity") < 0
                or r.get("side") not in ("long", "short")
            ):
                raise Unavailable("unreadable stock futures position")
            pos.extend(([1] if r["side"] == "long" else [-1]) * r["quantity"])
        return pos

    def orders(self, pending=False):
        return self.pages("orders", DAY, pending)

    def capacity(self, side, price):
        raw = self._paper(
            api.orderable_quantity, CODE, trd_div="1" if side == 1 else "2", ord_pric=str(int(price))
        )
        q = account.orderable_quantity(raw, code=CODE, side="buy" if side == 1 else "sell").get(
            "newQuantity"
        )
        return q is not None and q >= 1

    @staticmethod
    def window(role):
        now = datetime.now(KST)
        minute = now.hour * 60 + now.minute
        if now.date() != DAY or not 9 * 60 + 30 <= minute < (
            14 * 60 + 31 if role == "entry" else 15 * 60 + 45
        ):
            raise Unavailable("outside authorized demo order window")

    def submit(self, side, role):
        if side not in (-1, 1) or role not in ("entry", "exit"):
            raise Unavailable("invalid demo order")
        self.window(role)
        raw = self._paper(
            api.order,
            CODE,
            trd_div="1" if side == 1 else "2",
            ord_tp_cd=api.MARKET_PRICE,
            ord_qty="1",
            ord_pric="0",
            exec_cnd_cd=api.FAS,
        )
        ident = str(raw.get("ord_no") or "").strip()
        if not ident.isdigit() or not int(ident):
            raise Unavailable("unknown demo order acknowledgement")
        return order_no(ident)

    def cancel_entry(self, ident, row):
        self.window("exit")
        if (
            row["code"] != CODE
            or order_no(row["id"]) != ident
            or row.get("orderKind") != "new"
            or row.get("quantity") != 1
            or row.get("remaining") != 1
            or (row.get("filled") or 0) != 0
            or row.get("side") not in ("buy", "sell")
        ):
            raise Unavailable("cancel target does not match owned one-lot entry")
        return self._paper(
            api.cancel, CODE, org_ord_no=ident, trd_div="1" if row["side"] == "buy" else "2", ord_qty="1"
        )


def stamp(minute):
    return datetime.combine(DAY, datetime.min.time(), KST) + timedelta(minutes=minute)


def failure(exc):
    return dict(
        error_type=type(exc).__name__,
        code=str(getattr(exc, "code", "") or "")[:32],
        status=getattr(exc, "status", None),
        reason=str(exc)[:110] if isinstance(exc, Unavailable) else "",
    )


class Journal:
    def __init__(self, path, release):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.executescript("""
          PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY,v TEXT);
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,kst TEXT,kind TEXT,data TEXT);
          CREATE TABLE IF NOT EXISTS bars(m INTEGER PRIMARY KEY,seen TEXT,source TEXT,data TEXT);
          CREATE TABLE IF NOT EXISTS revisions(m INTEGER,seen TEXT,source TEXT,data TEXT,UNIQUE(m,source,data));
        """)
        row = self.db.execute("SELECT v FROM state WHERE id=1").fetchone()
        self.s = (
            json.loads(row[0])
            if row
            else dict(
                release=release,
                seen=[],
                plan=None,
                pending=None,
                trade=None,
                used=False,
                fills=0,
                round_trips=0,
                gross=0.0,
                fee_estimate=0.0,
                next_exit_retry=0.0,
                last_beat=-1,
                shadow_plan=None,
                shadow=None,
                shadow_used=False,
                shadow_round_trips=0,
                shadow_net=0.0,
                complete=False,
            )
        )
        if self.s["release"] != release:
            raise ValueError("Journal belongs to another release")
        self.bars = {m: tuple(json.loads(b)) for m, b in self.db.execute("SELECT m,data FROM bars")}

    def event(self, now, kind, **payload):
        with self.db:
            self.db.execute(
                "INSERT INTO events(kst,kind,data) VALUES(?,?,?)",
                (now.isoformat(timespec="milliseconds"), kind, canonical(payload)),
            )
            self.db.execute("INSERT OR REPLACE INTO state VALUES(1,?)", (canonical(self.s),))

    def merge(self, now, source, prices):
        minute = now.hour * 60 + now.minute
        rows = [(m, tuple(b)) for m, b in prices.items() if m + 5 <= minute and m not in self.bars]
        revisions = [
            (m, now.isoformat(), source, canonical(b))
            for m, b in prices.items()
            if m + 5 <= minute and m in self.bars and self.bars[m] != tuple(b)
        ]
        with self.db:
            self.db.executemany(
                "INSERT INTO bars VALUES(?,?,?,?)",
                [(m, now.isoformat(), source, canonical(b)) for m, b in rows],
            )
            self.db.executemany("INSERT OR IGNORE INTO revisions VALUES(?,?,?,?)", revisions)
        self.bars.update(rows)

    def close(self):
        self.db.close()


class Engine:
    def __init__(self, journal, entry, overlay):
        self.j, self.entry, self.overlay = journal, entry, overlay

    def plans(self, now, prior):
        j, s = self.j, self.j.s
        for i, m in enumerate(CLOCK):
            lead = (stamp(m) - now).total_seconds()
            if not 0 < lead <= 70 or m in s["seen"] or (s["used"] and s["shadow_used"]):
                continue
            latest = max((v for v, b in j.bars.items() if v + 5 <= m - 5 and b[4] > 0), default=-1)
            if latest < m - 15 or prior is None:
                continue
            x, dr, fr, scale = state(DAY, i, j.bars, prior)
            a = int(self.entry.act(i, x, dr, fr, allowed(DAY, i), DAY.toordinal()))
            q = self.entry.score(i, x, dr, fr)
            s["seen"].append(m)
            if a:
                p = dict(
                    at=m,
                    a=a,
                    side=int(SIDES[a - 1]),
                    scale=float(scale),
                    ref=j.bars[latest][3],
                    created=now.isoformat(),
                )
                if not s["used"] and s["pending"] is None:
                    s["plan"] = p
                if not s["shadow_used"] and s["shadow_plan"] is None:
                    s["shadow_plan"] = p
            j.event(
                now,
                "signal",
                minute=m,
                action=a,
                scores=q.tolist(),
                last_input_end=latest + 5,
                scale=float(scale),
                paper_eligible=not s["used"],
                history_mode=s.get("history_mode"),
                weights=s["release"],
            )

    def exit_plan(self, trade, now, tag):
        if self.overlay is None or trade.get("early_exit") is not None:
            return
        for m in checkpoints(trade["entry"], trade["planned"]):
            before = (stamp(m) - now).total_seconds()
            if not 0 < before <= 70 or m in trade["checked"]:
                continue
            latest = max((v for v, b in self.j.bars.items() if v + 5 <= m - 5 and b[4] > 0), default=-1)
            if latest < m - 15:
                continue
            x = exit_state(
                self.j.bars,
                m,
                trade["entry"],
                trade["price"],
                trade["side"],
                trade["planned"],
                trade["scale"],
            )
            value = float(self.overlay.value(x))
            trade["checked"].append(m)
            if value < 0:
                trade["early_exit"] = m
            self.j.event(
                now,
                tag + "_exit_signal",
                minute=m,
                incremental_hold_value=value,
                exit=value < 0,
                last_input_end=latest + 5,
            )

    def shadow(self, now):
        j, s = self.j, self.j.s
        p = s["shadow_plan"]
        if p:
            m = p["at"]
            row = j.bars.get(m)
            if row and row[4] > 0 and datetime.fromisoformat(p["created"]) < stamp(m):
                side = p["side"]
                px = row[0]
                fill = px + side * tick(px if side > 0 else px - 1)
                s["shadow"] = dict(
                    entry=m,
                    reference=px,
                    price=fill,
                    side=side,
                    scale=p["scale"],
                    planned=due(DAY, m, p["a"]),
                    checked=[],
                    early_exit=None,
                )
                s["shadow_used"], s["shadow_plan"] = True, None
                j.event(now, "reference_entry", minute=m, reference=px, mode="chart_simulation")
            elif now >= stamp(m + 9):
                s["shadow_plan"] = None
                j.event(now, "reference_entry_unobserved", minute=m)
        tr = s["shadow"]
        if tr:
            self.exit_plan(tr, now, "reference")
            at = tr["early_exit"] or tr["planned"]
            m = next((k for k, v in sorted(j.bars.items()) if k >= at and v[4] > 0), None)
            if m is not None and now >= stamp(m + 5):
                _, _, _, net = exchange_cash(tr["side"], tr["reference"], j.bars[m][0])
                s["shadow_round_trips"] += 1
                s["shadow_net"] += net
                s["shadow"] = None
                j.event(now, "reference_exit", minute=m, net_krw=net, mode="chart_simulation")

    def begin(self, port, now, role, side, p):
        j, s = self.j, self.j.s
        if s["pending"] is not None:
            return
        expected = [] if role == "entry" else [s["trade"]["side"]]
        if port.position() != expected or port.orders(True):
            raise Unavailable("paper position or open orders conflict with proposed order")
        before = sorted({order_no(r["id"]) for r in port.orders()})
        if role == "entry" and not port.capacity(side, p["ref"]):
            raise Unavailable("insufficient paper buying power")
        actual = port.clock() if hasattr(port, "clock") else datetime.now(KST)
        if actual.date() != DAY or (
            role == "entry" and not 0 <= (actual - stamp(p["at"])).total_seconds() <= 45
        ):
            return
        pending = dict(
            role=role,
            side=side,
            id=None,
            before=before,
            sent=actual.isoformat(),
            plan=p,
            cancel_attempt=False,
        )
        s["pending"] = pending
        if role == "entry":
            s["plan"] = None
        j.event(
            actual, "order_intent", role=role, quantity=1, side=side, symbol=CODE, environment="paper"
        )
        try:
            pending["id"] = port.submit(side, role)
        except Exception as exc:
            # A broker business rejection is final. Transport errors and missing IDs stay pending.
            from backend.nh_api.errors import BusinessError

            if isinstance(exc, BusinessError):
                s["pending"] = None
                s["next_exit_retry"] = actual.timestamp() + 20
                j.event(actual, "order_refused", role=role, **failure(exc))
            else:
                j.event(actual, "ack_unknown", role=role, **failure(exc))
            return
        j.event(actual, "ack", role=role, order_id=pending["id"])

    def reconcile(self, port, now):
        j, s = self.j, self.j.s
        p = s["pending"]
        if not p:
            return
        history = port.orders()
        want = "buy" if p["side"] > 0 else "sell"
        rows = [
            r
            for r in history
            if r.get("orderKind") == "new" and r.get("quantity") == 1 and r.get("side") == want
        ]
        if p["id"] is None:
            sent = datetime.fromisoformat(p["sent"])
            matching = set()
            for r in rows:
                clock = r.get("receivedTime") or r.get("fillTime")
                if not clock or order_no(r["id"]) in p["before"]:
                    continue
                try:
                    time_of_order = datetime.fromisoformat(str(DAY) + "T" + clock).replace(tzinfo=KST)
                except ValueError:
                    continue
                if -2 <= (time_of_order - sent).total_seconds() <= 30:
                    matching.add(order_no(r["id"]))
            if len(matching) != 1:
                return
            p["id"] = matching.pop()
            j.event(now, "ack_recovered_from_history", role=p["role"], order_id=p["id"])
        found = [r for r in rows if order_no(r["id"]) == p["id"]]
        fill = next(
            (r for r in found if r.get("filled") == 1 and (r.get("avgFillPrice") or r.get("fillPrice"))),
            None,
        )
        if fill is not None:
            price = float(fill.get("avgFillPrice") or fill["fillPrice"])
            clock = fill.get("fillTime")
            filled_at = (
                datetime.fromisoformat(str(DAY) + "T" + clock).replace(tzinfo=KST) if clock else now
            )
            m = filled_at.hour * 60 + filled_at.minute
            s["fills"] += 1
            if p["role"] == "entry":
                plan = p["plan"]
                s["used"] = True
                s["trade"] = dict(
                    entry=(m // 5) * 5,
                    price=price,
                    side=p["side"],
                    planned=due(DAY, (m // 5) * 5, plan["a"]),
                    scale=plan["scale"],
                    entry_id=p["id"],
                    checked=[],
                    early_exit=None,
                )
            else:
                tr = s["trade"]
                gross = tr["side"] * MULT * (price - tr["price"])
                fee = FEE * MULT * (price + tr["price"])
                s["round_trips"] += 1
                s["gross"] += gross
                s["fee_estimate"] += fee
                s["trade"] = None
            s["pending"] = None
            j.event(
                now,
                "confirmed_fill",
                role=p["role"],
                order_id=p["id"],
                broker_fill_time=clock,
                quantity=1,
                price=price,
                gross_krw=s["gross"],
                estimated_fees_krw=s["fee_estimate"],
            )
            return
        cancel = any(
            r.get("orderKind") == "cancel"
            and order_no(r.get("originalOrderNo")) == p["id"]
            and r.get("status") in ("confirmed", "filled")
            for r in history
        )
        terminal = any(
            r.get("status") in ("auto-cancelled", "rejected") or r.get("cancelled") == 1 for r in found
        )
        if cancel or terminal:
            s["pending"] = None
            s["next_exit_retry"] = now.timestamp() + 10
            j.event(now, "unfilled_terminal", role=p["role"], order_id=p["id"])
            return
        if (
            p["role"] == "entry"
            and not p["cancel_attempt"]
            and (now - datetime.fromisoformat(p["sent"])).total_seconds() > 65
        ):
            open_rows = [
                r for r in port.orders(True) if order_no(r["id"]) == p["id"] and r.get("remaining") == 1
            ]
            if len(open_rows) == 1:
                p["cancel_attempt"] = True
                j.event(now, "cancel_intent", order_id=p["id"])
                port.cancel_entry(p["id"], open_rows[0])
                j.event(now, "cancel_ack", order_id=p["id"])

    def tick(self, now, prior, port):
        j, s = self.j, self.j.s
        self.plans(now, prior)
        self.shadow(now)
        if s["pending"]:
            self.reconcile(port, now)
        if s["pending"]:
            return
        tr = s["trade"]
        if tr:
            self.exit_plan(tr, now, "paper")
            at = tr["early_exit"] or tr["planned"]
            if stamp(at) <= now < stamp(945) and now.timestamp() >= s["next_exit_retry"]:
                self.begin(port, now, "exit", -tr["side"], dict(at=at))
        elif s["plan"] and not s["used"]:
            p = s["plan"]
            age = (now - stamp(p["at"])).total_seconds()
            if 0 <= age <= 45:
                self.begin(port, now, "entry", p["side"], p)
            elif age > 45:
                s["plan"] = None
                j.event(now, "entry_window_missed", minute=p["at"])


OPEN, CLOSE = 525, 945


def session_chart(port, reader, requested_at, report_problem):
    """Prefer the freshest, most complete observed history. Paper wins ties."""
    end = min(CLOSE, (requested_at.hour * 60 + requested_at.minute) // 5 * 5)
    candidates, last_error = [], None
    for feed in (port, reader):
        try:
            raw = feed.bars(DAY)
            closed = {m: bar for m, bar in raw.items() if OPEN <= m < end}
            if not closed:
                raise Unavailable("no completed current-session candles")
            last_trade = max((m for m, bar in closed.items() if bar[4] > 0), default=-1)
            candidates.append(((last_trade, len(closed)), feed.environment, closed))
            if len(closed) == (end - OPEN) // 5 and last_trade == end - 5:
                break
        except Exception as exc:
            report_problem(datetime.now(KST), feed.environment + "_chart", exc)
            last_error = failure(exc)
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [(source, bars) for _, source, bars in candidates], None if candidates else last_error
