"""NH overseas demo REST adapter used for the Friday MNQ session.

The public NH FUTURES API application's backend provides the API and parsers.
The report and the offline replay do not connect to a broker.
"""

from datetime import datetime, timedelta, timezone
import time
from backend import orders, quotes
from backend.auth.auth_functions import TokenManager
from backend.config import DEFAULT_BASE_URLS, load_config
from backend.nh_api.client import TradeClient
from backend.nh_api.errors import BusinessError
from backend.nh_api.session import LIVE, Session, replay_enabled
from backend.overseas_futureoption import overseas_account as account, overseas_quotes as oq
from backend.overseas_futureoption import overseas_futureoption_functions as api
from risk import TICK, add_price_ok

KST = timezone(timedelta(hours=9))
CUTOFF = datetime(2026, 11, 27, 22, tzinfo=KST).timestamp()
DAY = "2026-11-27"


class OrderWithheld(Exception):
    pass


class DemoConnection:
    def __init__(self, authorization=None):
        import httpx
        import truststore

        config = load_config()
        if replay_enabled() or config.environment("paper").base_url != DEFAULT_BASE_URLS["paper"]:
            raise RuntimeError("Official NH paper server required")
        config.market("overseas", "paper")
        truststore.inject_into_ssl()
        http = httpx.Client(timeout=6)
        self.client = TradeClient(Session(LIVE, config, http, TokenManager(config, client=http)))
        self.authorization = authorization

    def call(self, fn, *args, **kwargs):
        return fn(*args, api=self.client, environment="paper", **kwargs)

    def close(self):
        self.client.session.close()

    def bars(self, code, start, end):
        start, end = start.astimezone(KST), end.astimezone(KST)
        cursor, seen, bars = api.FIRST_PAGE, set(), {}
        for _ in range(18):
            raw = self.call(
                api.chart_minute,
                code,
                exch_cd="FCME",
                inq_strt_dt=start.strftime("%Y%m%d"),
                strt_tm=start.strftime("%H%M%S"),
                inq_ed_dt=end.strftime("%Y%m%d"),
                ed_tm=end.strftime("%H%M%S"),
                req_qty="600",
                next_key=cursor,
            )
            page = quotes.bars(raw, minute=True, asked=code)
            if page["code"] != code or page["skipped"]:
                raise ValueError("NH chart mismatch")
            for r in page["bars"]:
                t = r["timestamp"] // 1000
                if start.timestamp() <= t <= end.timestamp():
                    b = [float(r[k]) for k in ("open", "high", "low", "close", "volume")]
                    if t % 60 or not 0 < b[2] <= min(b[0], b[3]) <= max(b[0], b[3]) <= b[1] or b[4] < 0:
                        raise ValueError("Invalid NH minute bar")
                    if t in bars and b != bars[t]:
                        raise ValueError("Conflicting NH minute bar")
                    bars[t] = b
            key = page["nextKey"]
            if (
                not page["bars"]
                or not key
                or set(key) == {"9"}
                or min(r["timestamp"] for r in page["bars"]) <= start.timestamp() * 1000
            ):
                return bars
            if key == cursor or key in seen:
                raise ValueError("Repeated NH chart cursor")
            seen.add(cursor)
            cursor = key
            time.sleep(0.22)
        raise ValueError("Incomplete NH chart")

    def book(self, code):
        b = oq.book(self.call(api.bid_price, code, exch_cd="FCME"), asked=code)
        if b.get("code") != code:
            raise ValueError("NH book symbol mismatch")
        bid = max(float(r["price"]) for r in b["bids"] if r["quantity"] > 0)
        ask = min(float(r["price"]) for r in b["asks"] if r["quantity"] > 0)
        if not 0 < bid <= ask:
            raise ValueError("Invalid NH book")
        now = datetime.now(KST)
        hms = datetime.strptime(b["time"], "%H:%M:%S").time()
        stamps = [datetime.combine(now.date() + timedelta(days=i), hms, KST) for i in (-1, 0, 1)]
        stamp = min(stamps, key=lambda ts: abs((now - ts).total_seconds()))
        return dict(
            bid=bid, ask=ask, timestamp=stamp.timestamp(), age=round((now - stamp).total_seconds(), 2)
        )

    def rows(self, kind):
        cursor, seen, records = "", set(), []
        while cursor not in seen and len(seen) < 30:
            seen.add(cursor)
            if kind == "positions":
                response = account.positions(self.call(api.open_interest, next_key=cursor))
            else:
                raw = self.call(
                    api.order_history,
                    inq_div=api.PENDING if kind == "pending" else api.ALL,
                    next_key=cursor,
                )
                response = account.order_history(raw)
            records.extend(response["positions" if kind == "positions" else "orders"])
            cursor = response["nextKey"]
            if not cursor or set(cursor) == {"9"}:
                return records
        raise ValueError("Incomplete NH account query")

    def capacity(self, code, side, price, order_type=api.MARKET_PRICE):
        raw = self.call(api.orderable_quantity, code, glb_ord_tp_cd=order_type, ord_pric=f"{price:.2f}")
        quantity = account.orderable_quantity(raw, code=code, side="buy" if side == 1 else "sell")
        return int(quantity["newQuantity"])


class DemoTransport(DemoConnection):
    code = "MNQZ26"

    def check_time(self, role, signal_bar):
        now = datetime.now(KST)
        if role not in ("entry", "exit", "cancel") or str(now.date()) != DAY or now.hour < 8:
            raise OrderWithheld("outside_demo_session")
        if role == "entry" and (
            now.timestamp() >= CUTOFF
            or signal_bar is None
            or signal_bar % 60
            or not signal_bar <= now.timestamp() < signal_bar + 60
        ):
            raise OrderWithheld("execution_window_elapsed")

    def order(self, code, side, role, original=None, limit=None, signal_bar=None):
        self.check_time(role, signal_bar)
        if (
            code != self.code
            or side not in (-1, 1)
            or (role == "cancel") != bool(original)
            or (
                limit is not None
                and (role == "exit" or limit <= 0 or abs(limit / TICK - round(limit / TICK)) > 1e-7)
            )
        ):
            raise BusinessError("paper_order", "OUT_OF_POLICY", "Invalid paper order", {})
        raw = self.call(
            api.order,
            code,
            qt_div_cd=api.CANCEL if original else api.NEW,
            trd_div_cd=account.SIDE_CODE["buy" if side == 1 else "sell"],
            glb_ord_tp_cd=api.LIMIT_PRICE if limit is not None else api.MARKET_PRICE,
            ord_qty="1",
            ord_pric=f"{limit:.2f}" if limit is not None else "0",
            stop_pric="",
            org_ord_no=original or api.NO_ORIGINAL,
        )
        if orders.failed(str(raw.get("err_cd") or "").strip()):
            raise BusinessError("paper_order", str(raw["err_cd"]), "NH paper order rejected", {})
        ident = orders.order_no(raw.get("ord_no"))
        if not ident or not ident.strip("0"):
            raise ValueError("Missing NH acknowledgement")
        return ident


class MinutePaper(DemoTransport):
    """Send one owned contract; the strategy has reconciled inventory before calling."""

    def order(
        self,
        code,
        side,
        role,
        original=None,
        limit=None,
        signal_bar=None,
        deadline=None,
        trigger_price=None,
        add_state=None,
    ):
        self.check_time(role, signal_bar)
        if code != self.code or limit is not None:
            raise OrderWithheld("outside_minute_release")
        if role == "entry":
            b = self.book(code)
            if not -1 <= time.time() - b["timestamp"] <= 8 or b["timestamp"] < signal_bar:
                raise OrderWithheld("entry_quote_unavailable")
            px = b["ask" if side == 1 else "bid"]
            if b["ask"] - b["bid"] > 4 * TICK:
                raise OrderWithheld("wide_spread")
            if trigger_price is not None and side * (px - trigger_price) > 0:
                raise OrderWithheld("pullback_recovered")
            if add_state is not None and not add_price_ok(self.rule, add_state[0], px, add_state[1]):
                raise OrderWithheld("add_price_moved")
        return super().order(code, side, role, original, limit, signal_bar)

    def __init__(self, rule):
        super().__init__()
        self.rule = rule
