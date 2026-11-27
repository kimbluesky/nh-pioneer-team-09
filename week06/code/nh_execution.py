"""Friday's staged order logic, extracted from the NH demo worker.

One owner calls on_bar(record) on a completed forecast, then poll() between bars.
The caller supplies the broker port and the durable journal. No orders at import.
"""

from datetime import datetime
import math
import time

from nh_demo import BusinessError, CUTOFF, KST, OrderWithheld, api, orders
from risk import (
    FEE,
    MAX_DAY,
    MULT,
    RISK_RESERVE,
    TICK,
    add_price_ok,
    add_stop,
    component_key,
    forecast_action,
    pullback_level,
    room,
    stop_distance,
    trail_after_partial,
)


def minute_at(stamp):
    local = datetime.fromtimestamp(stamp, KST)
    return local.hour * 60 + local.minute


class StagedOrders:
    def __init__(self, journal, port, rule, clock=time.time):
        self.journal, self.s, self.port, self.clock = journal, journal.s, port, clock
        self.rule = dict(rule, management="scale", touch_partial=True)
        self.code, self.tick, self.mult = "MNQZ26", TICK, MULT

    def entry_start(self, plan):
        return plan["bar"]

    def exit_deadline(self, plan):
        return None

    def add_block(self, pos, side, price):
        if not pos or side != pos["side"] or not add_price_ok(self.rule, pos, price, self.s["net"]):
            return "add_price_or_budget"
        return None

    def log(self, kind, **fields):
        self.journal.log(self.clock(), kind, **fields)

    def halt(self, reason):
        if not self.s["halt"]:
            self.s["halt"] = reason
            self.log("entry_halt", reason=reason)

    def for_symbol(self, kind):
        return [r for r in self.port.rows(kind) if r.get("code") == self.code]

    def inventory(self):
        out = []
        for r in self.for_symbol("positions"):
            if (
                r.get("side") not in ("long", "short")
                or not isinstance(r.get("quantity"), int)
                or r["quantity"] < 0
            ):
                raise ValueError("Cannot establish position ownership")
            out.extend(([1] if r["side"] == "long" else [-1]) * r["quantity"])
        return out

    def book(self):
        b = self.port.book(self.code)
        if not -1 <= self.clock() - b["timestamp"] <= 8:
            raise ValueError("Stale quote")
        return b

    def skip(self, slot, reason):
        if slot not in self.s["processed"]:
            self.s["processed"].append(slot)
            self.log("no_entry", slot=slot, reason=reason)

    def holdings(self):
        pos = self.s["position"]
        return [pos["side"]] * len(pos["lots"]) if pos else []

    def reconcile(self):
        p = self.s["pending"]
        p["plan"]["slot"] = minute_at(p["plan"]["bar"])
        rows = self.for_symbol("orders")
        expect = "buy" if p["side"] == 1 else "sell"
        eligible = [
            r
            for r in rows
            if r.get("orderKind") == "new" and r.get("quantity") == 1 and r.get("side") == expect
        ]
        if not p["id"]:
            matches = set()
            for r in eligible:
                ident = orders.order_no(r["id"])
                if ident in p["before"] or not r.get("receivedDate") or not r.get("receivedTime"):
                    continue
                ts = (
                    datetime.fromisoformat(r["receivedDate"] + "T" + r["receivedTime"])
                    .replace(tzinfo=KST)
                    .timestamp()
                )
                if abs(ts - p["sent"]) <= 12:
                    matches.add(ident)
            if len(matches) == 1:
                p["id"] = matches.pop()
                self.log("ack_recovered", ref=p["ref"])
        owned = [r for r in eligible if p["id"] and orders.order_no(r["id"]) == p["id"]]
        fill = next(
            (
                r
                for r in owned
                if r.get("filled") == 1
                and r.get("remaining") == 0
                and (r.get("avgFillPrice") or r.get("fillPrice"))
            ),
            None,
        )
        if fill:
            self.fill(p, fill)
            return
        terminal = any(
            r.get("remaining") == 0
            and (r.get("cancelled") == 1 or r.get("status") in ("cancelled", "auto-cancelled", "rejected"))
            for r in owned
        )
        cancelled = any(
            r.get("orderKind") == "cancel"
            and p["id"]
            and orders.order_no(r.get("originalOrderNo")) == p["id"]
            and r.get("status") in ("confirmed", "cancelled", "auto-cancelled")
            for r in rows
        )
        if terminal or cancelled:
            if any(r.get("status") == "rejected" for r in owned):
                self.halt("order_rejected")
            self.s["pending"] = None
            self.s["retry"] = self.clock() + 2
            self.log("unfilled_terminal", ref=p["ref"], slot=p["plan"]["slot"], role=p["role"])
            return
        age = self.clock() - p["sent"]
        expiry = p["sent"] + 25
        if p["id"] and self.clock() >= expiry and self.clock() - p["last_cancel"] >= 20:
            live = [
                r
                for r in self.for_symbol("pending")
                if orders.order_no(r["id"]) == p["id"] and r.get("remaining") == 1
            ]
            if live:
                p["last_cancel"] = self.clock()
                self.log("cancel_intent", ref=p["ref"], slot=p["plan"]["slot"], role=p["role"])
                self.port.order(self.code, p["side"], "cancel", original=p["id"], limit=p["limit"])
                self.log("cancel_ack", ref=p["ref"])
        if age > 90 and not p["alerted"]:
            p["alerted"] = True
            self.log("unresolved_order", ref=p["ref"])
            self.halt("unresolved_order")

    def submit(self, phase, side, p, reason, deadline=None, limit=None):
        s = self.s
        entry = phase != "reduce"
        m = minute_at(p["bar"])
        if s["pending"] or self.clock() < s["retry"]:
            return
        if self.inventory() != self.holdings() or self.for_symbol("pending"):
            self.halt("position_or_pending_conflict")
            s["retry"] = self.clock() + 2
            return
        before = [orders.order_no(r["id"]) for r in self.for_symbol("orders")]
        if entry:
            if s["halt"] or s["locked"]:
                return self.skip(m, s["halt"] or "daily_lock")
            book = self.book()
            px = book["ask" if side == 1 else "bid"]
            if book["ask"] - book["bid"] > 4 * self.tick + 1e-8:
                self.skip(m, "wide_spread")
                return "wide_spread"
            if phase == "add":
                denied = self.add_block(s["position"], side, px)
                if denied:
                    return self.skip(m, denied)
            if (
                self.port.capacity(
                    self.code,
                    side,
                    px if limit is None else limit,
                    api.MARKET_PRICE if limit is None else api.LIMIT_PRICE,
                )
                < 1
            ):
                return self.skip(m, "insufficient_capacity")
            start = self.entry_start(p)
            if not start <= self.clock() < start + 60:
                return self.skip(m, "execution_minute_elapsed")
            s["processed"].append(m)
        s["counter"] += 1
        req = dict(
            ref=f"order-{s['counter']}",
            phase=phase,
            role="entry" if entry else "exit",
            side=side,
            reason=reason,
            limit=limit,
            plan=dict(p, limit_minutes=5 if limit is not None else 2),
            id=None,
            before=before,
            sent=self.clock(),
            last_cancel=0.0,
            alerted=False,
        )
        s["pending"] = req
        self.log(
            "order_intent",
            ref=req["ref"],
            phase=phase,
            role=req["role"],
            side=side,
            qty=1,
            slot=m,
            reason=reason,
            limit=limit,
        )
        price_check = {"trigger_price": p["entry_trigger"]} if p.get("entry_trigger") is not None else {}
        if self.rule.get("management") == "scale" and phase == "add":
            price_check["add_state"] = (s["position"], s["net"])
        try:
            req["id"] = self.port.order(
                self.code,
                side,
                req["role"],
                signal_bar=p["bar"],
                deadline=deadline,
                limit=limit,
                **price_check,
            )
        except OrderWithheld as exc:
            s["pending"] = None
            s["retry"] = self.clock() + 2
            self.log("order_withheld", ref=req["ref"], reason=str(exc))
            return str(exc)
        except BusinessError as exc:
            s["pending"] = None
            s["retry"] = self.clock() + 10
            self.log("order_rejected", ref=req["ref"], code=str(exc.code))
            self.halt("order_rejected")
        except Exception as exc:
            self.log("ack_unknown", ref=req["ref"], error=type(exc).__name__)
        else:
            self.log("order_ack", ref=req["ref"], role=req["role"])

    def fill(self, p, row):
        px = float(row.get("avgFillPrice") or row.get("fillPrice") or 0)
        if not math.isfinite(px) or px <= 0 or (row.get("filled"), row.get("remaining")) != (1, 0):
            return
        s = self.s
        plan = p["plan"]
        side = p["side"]
        phase = p["phase"]
        net = None
        owner = (s["position"] or {}).get("component") or plan.get("component")
        was_locked = s["locked"]
        s["pending"] = None
        s["fills"] += 1
        s["fees"] += 2
        if phase == "first":
            r = plan["distance"]
            partial = math.ceil(self.rule["partial_r"] * r / self.tick - 1e-8) * self.tick
            s["position"] = dict(
                first=px,
                r=r,
                lots=[px],
                side=side,
                bar=plan["bar"],
                stop=px - side * r,
                partial_distance=partial,
                budget=plan["budget"],
                maxq=plan["maxq"],
                added=False,
                target=None,
                trigger=None,
                last_quote=self.clock(),
                deadline=self.exit_deadline(plan),
            )
        elif phase == "add":
            pos = s["position"]
            pos["lots"].append(px)
            pos["added"] = True
            pos["stop"] = pos["first"]
            if self.rule.get("management") == "scale":
                pos["stop"] = add_stop(pos, s["net"])
            pos["target"] = pos["first"] + side * pos["partial_distance"]
            pos["target_after"] = (
                self.clock() if self.rule.get("touch_partial") else (int(self.clock()) // 60 + 1) * 60
            )
        else:
            pos = s["position"]
            gross = (px - pos["lots"].pop(0)) * pos["side"] * self.mult
            net = round(gross - 4, 2)
            s["net"] = round(s["net"] + net, 2)
            s["gross"] += gross
            if p["reason"] in ("scale_price", "model_reduce"):
                pos["target"] = None
                if pos["trigger"] == p["reason"]:
                    pos["trigger"] = None
            if not pos["lots"]:
                s["position"] = None
                s["trades"] += 1
            if s["net"] <= -MAX_DAY:
                s["locked"] = True
        if self.rule.get("management") == "scale":
            if phase in ("first", "add"):
                s["position"]["fill_minute"] = int(self.clock()) // 60 * 60
            if phase == "first" and plan.get("component"):
                s["position"]["component"] = plan["component"]
            if not s["position"]:
                s["last_flat_minute"] = int(self.clock()) // 60 * 60
            s.pop("scale_arm", None)
        pos = s["position"]
        self.log(
            "confirmed_fill",
            ref=p["ref"],
            phase=phase,
            role=p["role"],
            reason=p["reason"],
            side=side,
            qty=1,
            price=px,
            slot=minute_at(plan["bar"]),
            broker_date=row.get("fillDate"),
            broker_time=row.get("fillTime"),
            trade_net=net,
            day_net=s["net"],
            remaining_qty=len(self.holdings()),
            component=owner,
        )
        if pos:
            levels = {}
            if self.rule.get("management") == "scale":
                levels = dict(
                    first_price=pos["first"],
                    risk_points=pos["r"],
                    risk_budget=pos["budget"],
                    next_add=pos["first"] + pos["side"] * self.rule["add_r"] * pos["r"]
                    if not pos["added"]
                    else None,
                    partial_level=pos["first"] + pos["side"] * pos["partial_distance"],
                    component=pos.get("component"),
                )
            self.log(
                "risk_set",
                stop=pos["stop"],
                target=pos["target"],
                qty=len(pos["lots"]),
                **levels,
                deadline_kst=datetime.fromtimestamp(pos["deadline"], KST).isoformat()
                if pos["deadline"] is not None
                else None,
            )
        if s["locked"] and not was_locked:
            self.log("daily_lock", realized=s["net"], limit=MAX_DAY)
        self.log("inventory_after_fill", qty=len(self.holdings()))

    def trigger(self, reason):
        pos = self.s["position"]
        old = pos["trigger"]
        if old is None or (
            old in ("model_reduce", "scale_price") and reason not in ("model_reduce", "scale_price")
        ):
            pos["trigger"] = reason
            self.log(
                "risk_trigger",
                reason=reason,
                qty=len(pos["lots"]),
                stop=pos["stop"],
                slot=minute_at(pos["bar"]),
            )

    def check_price_risk(self, b, *, quote_time=None, lock_on_mark=True):
        pos = self.s["position"]
        previous = pos["trigger"]
        side = pos["side"]
        px = b["bid" if side == 1 else "ask"]
        q = len(pos["lots"])
        pos["last_quote"] = self.clock() if quote_time is None else quote_time
        net = self.s["net"]
        mark = net + sum((px - lot) * side * self.mult - 4 for lot in pos["lots"])
        newly_locked = lock_on_mark and mark <= -MAX_DAY and not self.s["locked"]
        if newly_locked:
            self.s["locked"] = True
        daily = sum(pos["lots"]) / q - side * (MAX_DAY + net - 4 * q) / (q * self.mult)
        is_daily = (daily - pos["stop"]) * side > 0
        trigger = daily if is_daily else pos["stop"]
        stop = (
            math.ceil(trigger / self.tick - 1e-8) if side == 1 else math.floor(trigger / self.tick + 1e-8)
        ) * self.tick
        if mark <= -MAX_DAY or side * (px - stop) <= 1e-7:
            self.trigger("daily_stop" if is_daily or mark <= -MAX_DAY else "stop")
        elif (
            pos["target"] is not None
            and self.clock() >= pos["target_after"]
            and side * (px - pos["target"]) >= (0.0 if self.rule.get("touch_partial") else self.tick) - 1e-7
        ):
            self.trigger("scale_price")
        if newly_locked:
            self.log(
                "daily_lock",
                reason="marked_loss_limit",
                realized=net,
                marked_net=round(mark, 2),
                limit=MAX_DAY,
                quote_epoch=b["timestamp"],
            )
        if pos["trigger"] != previous:
            self.log(
                "risk_quote",
                reason=pos["trigger"],
                qty=q,
                component=pos["component"],
                bid=b["bid"],
                ask=b["ask"],
                stop=pos["stop"],
                target=pos["target"],
                quote_epoch=b["timestamp"],
            )

    def owned_forecast(self, record):
        pos = self.s["position"]
        if not pos or not self.rule.get("components"):
            return record
        key = pos["component"]
        leg = next(leg for leg in self.rule["components"] if component_key(leg) == key)
        value = record["component_scores"][key]
        return dict(
            record,
            component=key,
            long_score=value / leg["cut"],
            raw_atr_score=value,
            component_cut=leg["cut"],
            forecast_move_usd=value * record["atr"] * self.mult,
        )

    def reconcile_feed(self, stamp):
        s = self.s
        if (
            s["halt"] != "lost_quote"
            or s["locked"]
            or s["position"]
            or s["pending"]
            or stamp <= s.get("last_flat_minute", 0)
        ):
            return
        book = self.book()
        if book["timestamp"] < stamp:
            raise ValueError("waiting_for_post_close_quote")
        if self.inventory() or self.for_symbol("pending"):
            self.halt("position_or_pending_conflict")
            return
        s["halt"] = None
        self.log("feed_reconciled", signal_bar=stamp, quote_epoch=book["timestamp"], quantity=0, pending=0)

    def evaluate_position(self, record, book):
        pos = self.s["position"]
        partial = ("scale_price", "model_reduce")
        if not pos or (pos["trigger"] and pos["trigger"] not in partial) or self.s["pending"]:
            return
        side = pos["side"]
        executable = book["bid"] if side == 1 else book["ask"]
        exit_price = executable - side * self.tick
        net = sum((exit_price - entry) * side * self.mult - 2 * FEE for entry in pos["lots"])
        exit_cost = FEE + self.tick * self.mult
        record = self.owned_forecast(record)
        stop = trail_after_partial(self.rule, pos, record["last"])
        if stop != pos["stop"]:
            pos["stop"] = stop
            self.log("trailing_stop", stop=stop, qty=len(pos["lots"]), signal_bar=record["bar"])
            self.check_price_risk(book)
        reason = forecast_action(self.rule, pos, record["long_score"], net)
        edge = side * record.get("raw_atr_score", record["long_score"]) * record["atr"] * self.mult
        if reason:
            self.trigger(reason)
        self.log(
            "position_forecast",
            signal_bar=record["bar"],
            side=side,
            long_score=record["long_score"],
            threshold=record["threshold"],
            atr=record["atr"],
            bid=book["bid"],
            ask=book["ask"],
            estimated_net_if_closed=round(net, 2),
            forecast_remaining_usd=round(edge, 4),
            exit_cost_usd=exit_cost,
            reason=reason or "hold",
            component=record.get("component"),
        )

    def try_pullback(self):
        s = self.s
        arm = s.get("scale_arm")
        if not arm:
            return
        now = self.clock()
        if now >= arm["bar"] + 60 or s["position"] or s["pending"] or s["locked"] or s["halt"]:
            s.pop("scale_arm", None)
            self.log(
                "entry_watch_end",
                signal_bar=arm["bar"],
                reason="expired" if now >= arm["bar"] + 60 else "inventory_or_halt",
            )
            return
        if now < s["retry"] or not arm["bar"] <= now:
            return
        book = self.book()
        side = arm["side"]
        price = book["ask" if side == 1 else "bid"]
        if (
            book["timestamp"] < arm["bar"]
            or book["ask"] - book["bid"] > 4 * self.tick
            or side * (price - arm["entry_trigger"]) > 0
        ):
            return
        outcome = self.submit("first", side, arm, "forecast_pullback")
        if outcome not in (
            "wide_spread",
            "quote_unavailable",
            "entry_quote_unavailable",
            "pullback_recovered",
        ):
            s.pop("scale_arm", None)
            self.log(
                "entry_watch_end",
                signal_bar=arm["bar"],
                reason="order_check_completed",
                entry_trigger=arm["entry_trigger"],
                observed_price=price,
            )

    def scaled_close(self, stamp, record):
        s = self.s
        if not record or record["bar"] != stamp:
            return
        self.reconcile_feed(stamp)
        pos = s["position"]
        can_enter = not (s["halt"] or s["locked"] or s["pending"])
        first = not pos and can_enter and record["side"] and stamp > s.get("last_flat_minute", 0)
        held = pos and not s["pending"] and stamp > pos.get("fill_minute", 0)
        if first or held:
            if not stamp <= self.clock() < stamp + 60:
                return self.log("withhold", reason="expired_forecast", signal_bar=stamp)
            book = self.book()
            if book["timestamp"] < stamp:
                raise ValueError("waiting_for_post_close_quote")
            if held:
                self.check_price_risk(book)
                self.evaluate_position(record, book)
                record = self.owned_forecast(record)
            side = pos["side"] if held else record["side"]
            px = book["ask" if side == 1 else "bid"]
            add = (
                held
                and can_enter
                and not pos["trigger"]
                and record["long_score"] * side >= 0.2 * self.rule["cut"]
                and add_price_ok(self.rule, pos, px, s["net"])
            )
            if first or add:
                if self.clock() < s["retry"]:
                    raise ValueError("entry_retry_pending")
                if book["ask"] - book["bid"] > 4 * self.tick:
                    if s.get("spread_wait_bar") != stamp:
                        s["spread_wait_bar"] = stamp
                        self.log(
                            "entry_quote_wait",
                            reason="wide_spread",
                            signal_bar=stamp,
                            bid=book["bid"],
                            ask=book["ask"],
                        )
                    raise ValueError("wide_spread")
                distance = stop_distance(record["atr"], s["net"], self.rule)
                if distance:
                    plan = dict(
                        record,
                        maxq=2,
                        budget=room(s["net"]),
                        distance=distance,
                        unit_risk=distance * MULT + RISK_RESERVE,
                    )
                    self.log(
                        "scale_decision",
                        phase="add" if add else "first",
                        component=record.get("component"),
                        signal_bar=stamp,
                        side=side,
                        qty_before=len(self.holdings()),
                        order_qty=1,
                        bid=book["bid"],
                        ask=book["ask"],
                        quote_epoch=book["timestamp"],
                        long_score=record["long_score"],
                        raw_atr_score=record.get("raw_atr_score", record["long_score"]),
                        atr=record["atr"],
                        risk_points=pos["r"] if pos else distance,
                        risk_budget=plan["budget"],
                        add_trigger=pos["first"] + side * self.rule["add_r"] * pos["r"] if pos else None,
                    )
                    trigger = (
                        pullback_level(
                            record if "pullback_atr" in record else self.rule,
                            record["last"],
                            record["atr"],
                            side,
                        )
                        if first
                        else None
                    )
                    if trigger is not None:
                        if (s.get("scale_arm") or {}).get("bar") != stamp:
                            s["scale_arm"] = dict(plan, entry_trigger=trigger)
                            self.log(
                                "entry_watch",
                                signal_bar=stamp,
                                side=side,
                                entry_trigger=trigger,
                                expires=stamp + 60,
                            )
                        self.try_pullback()
                    else:
                        outcome = self.submit(
                            "add" if add else "first",
                            side,
                            plan,
                            "favorable_confirmation" if add else "minute_forecast",
                        )
                        if (
                            outcome
                            in (
                                "wide_spread",
                                "quote_unavailable",
                                "entry_quote_unavailable",
                                "add_price_moved",
                            )
                            and self.clock() < stamp + 60
                        ):
                            raise ValueError(outcome)
                else:
                    self.log("withhold", reason="risk_budget", available=room(s["net"]), signal_bar=stamp)
        s["last_execution_window"] = stamp
        self.log("minute_checked", slot=minute_at(stamp))

    def on_bar(self, record):
        if (
            self.s["done"]
            or self.clock() >= CUTOFF
            or record["bar"] <= self.s.get("last_execution_window", 0)
        ):
            return
        self.scaled_close(record["bar"], record)

    def poll(self):
        s = self.s
        if s["done"]:
            return
        if self.clock() >= CUTOFF:
            if not s.get("operator_close_requested"):
                s["operator_close_requested"] = True
                s.pop("scale_arm", None)
                self.log(
                    "operator_cutoff", scheduled_end_kst=datetime.fromtimestamp(CUTOFF, KST).isoformat()
                )
            if s["pending"]:
                self.reconcile()
                if s["pending"]:
                    return
            if s["position"]:
                pos = s["position"]
                self.trigger("operator_close")
                self.submit("reduce", -pos["side"], {"bar": pos["bar"]}, pos["trigger"])
            elif self.inventory() or self.for_symbol("pending"):
                self.halt("position_or_pending_conflict")
            else:
                s.update(done=True, flat=True, carry_open=False, window_closed_at=self.clock())
                self.log("session_end", reason="operator_close", flat=True, quantity=0, pending=0)
            return
        if s["pending"]:
            try:
                self.reconcile()
            except Exception:
                if s["pending"] and self.clock() - s["pending"]["sent"] > 90:
                    self.halt("unresolved_order")
                if not s["position"]:
                    raise
        pos = s["position"]
        if pos:
            try:
                book = self.book()
            except Exception:
                if self.clock() - pos["last_quote"] > 15:
                    self.halt("lost_quote")
                    self.trigger("feed_exit")
            else:
                self.check_price_risk(book, quote_time=min(self.clock(), book["timestamp"]))
            if pos["trigger"] and not s["pending"]:
                self.submit("reduce", -pos["side"], {"bar": pos["bar"]}, pos["trigger"])
        if s.get("scale_arm"):
            self.try_pullback()
