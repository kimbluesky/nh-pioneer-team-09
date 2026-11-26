"""Learn entry and exit continuation values from chronologically ordered sessions."""

from dataclasses import dataclass

import numpy as np

from market import CLOCK, MULT, SIDES, cost, due, prints, tick


def basis(x, drift):
    return np.concatenate((np.ones_like(x[..., :1]), x, drift), axis=-1)


def ridge(x, y, w):
    good = np.isfinite(y)
    if not good.any():
        return np.zeros(x.shape[-1])
    x, y, w = x[good], y[good], w[good]
    w = w / w.sum()
    p = 0.7 * np.eye(x.shape[-1])
    p[0, 0] = 0.0001
    return np.linalg.solve((x.T * w) @ x + p, (x.T * w) @ y)


class Policy:
    def __init__(self, half, residual, entry, wait):
        self.half, self.residual, self.entry, self.wait = half, residual, entry, wait

    def score(self, s, x, drifts, friction):
        drift = drifts[..., 0 if self.half == 8 else 1, :]
        f = basis(x, drift)
        edge = drift + f @ self.entry[s]
        entry = np.repeat(edge, 2, axis=-1) * SIDES - friction
        return np.concatenate(((f @ self.wait[s])[..., None], entry), axis=-1)

    def act(self, s, x, drifts, friction, legal, ordinal):
        q = self.score(s, x, drifts, friction)
        if not np.isfinite(q).all():
            raise ArithmeticError("Nonfinite option values")
        masked = np.where(legal, q, -np.inf)
        chosen = masked.argmax(axis=-1)
        # Exact side ties use a date parity independent of future prices.
        for a in (1, 3):
            chosen = np.where(
                (chosen == a)
                & (masked[..., a] == masked[..., a + 1])
                & ((np.asarray(ordinal) + s) % 2 == 0),
                a + 1,
                chosen,
            )
        return chosen

    def save(self, path):
        temp = path.with_suffix(".partial")
        with temp.open("wb") as f:
            np.savez(f, half=self.half, residual=self.residual, entry=self.entry, wait=self.wait)
        temp.replace(path)

    @classmethod
    def load(cls, path):
        with np.load(path, allow_pickle=False) as z:
            return cls(int(z["half"]), bool(z["residual"]), z["entry"], z["wait"])


def fit(t, ids, half, residual, external=None):
    dr = t.drift[ids, :, 0 if half == 8 else 1]
    design = basis(t.x[ids], dr)
    n, stages, p = design.shape
    model = Policy(half, residual, np.zeros((stages, p, 2)), np.zeros((stages, p)))
    weights = np.exp2((np.arange(n) - n + 1) / 252)
    value = np.zeros(n)
    for s in reversed(range(stages)):
        f = design[:, s]
        if residual:
            for h in (0, 1):
                y = (t.gross_move[ids, s, h] - dr[:, s, h]).clip(-3, 3)
                model.entry[s, :, h] = ridge(f, y, weights)
        if s < stages - 1:
            model.wait[s] = ridge(f, value if external is None else external[:, s], weights)
        a = model.act(
            s,
            t.x[ids, s],
            t.drift[ids, s],
            t.friction[ids, s],
            t.legal[ids, s],
            [t.days[i].toordinal() for i in ids],
        )
        realised = t.reward[ids, s, (a - 1).clip(0)]
        value = np.where((a != 0) & np.isfinite(realised), realised, value)
    return model


def train(t, ids, half, residual):
    labels = np.zeros((len(ids), len(CLOCK)))
    blocks = []
    for local in np.array_split(np.arange(len(ids)), 4):
        rows = ids[local]
        past = ids[: local[0]]
        teacher = fit(t, past, half, residual) if len(past) else None
        value = np.zeros(len(rows))
        for s in reversed(range(len(CLOCK))):
            labels[local, s] = value
            if teacher is None:
                if s == len(CLOCK) - 1:
                    value = np.nan_to_num(t.reward[rows, s, :2].mean(axis=1))
            else:
                a = teacher.act(
                    s,
                    t.x[rows, s],
                    t.drift[rows, s],
                    t.friction[rows, s],
                    t.legal[rows, s],
                    [t.days[i].toordinal() for i in rows],
                )
                net = t.reward[rows, s, (a - 1).clip(0)]
                value = np.where((a != 0) & np.isfinite(net), net, value)
        blocks.append(
            dict(
                teacher_last=str(t.days[past[-1]]) if len(past) else None,
                first_target=str(t.days[rows[0]]),
                last_target=str(t.days[rows[-1]]),
                days=len(rows),
            )
        )
    return fit(t, ids, half, residual, labels), blocks


def checkpoints(entry, planned):
    return tuple(range(entry + 30, planned - 14, 30))


def exit_state(visible, minute, entry, fill, side, planned, scale):
    bars = [(m, b) for m, b in prints(visible) if m + 5 <= minute - 5]
    if not bars:
        raise ValueError("No observable exit input")
    first, last = bars[0][1][0], bars[-1][1][3]
    future = [b for m, b in bars if m >= entry]
    recent = [b for m, b in bars if m >= minute - 40]
    hi = max([fill] + [r[1] for r in future])
    lo = min([fill] + [r[2] for r in future])
    z = side * MULT / scale
    fav = z * ((hi if side > 0 else lo) - fill)
    pnl = z * (last - fill)
    loc = (2 * last - hi - lo) / max(hi - lo, tick(last))
    rec = (last - recent[0][0]) * z if recent else 0.0
    time_left = (planned - minute) / 330.0
    return np.array(
        [
            1.0,
            side,
            time_left,
            (minute - entry) / 330.0,
            pnl,
            fav - pnl,
            side * loc,
            (last - first) * z,
            rec,
            pnl * time_left,
            rec * time_left,
            (hi - lo) * MULT / scale,
        ]
    ).clip(-4, 4)


@dataclass
class Samples:
    x: np.ndarray
    inc: np.ndarray
    following: np.ndarray
    depth: np.ndarray
    day: np.ndarray
    weight: np.ndarray
    days: list


def paths(t, stock):
    xs, delta, nxt, depth, ids = [], [], [], [], []
    for d, day in enumerate(t.days):
        bars = stock[day]
        for s, start in enumerate(CLOCK):
            for a, side in enumerate(SIDES, 1):
                if not t.legal[d, s, a]:
                    continue
                en, ex, op, cl = t.quotes[d, s, (a - 1) // 2]
                if not np.isfinite(op):
                    continue
                en, ex = int(en), int(ex)
                planned = due(day, en, a)
                points = [v for v in checkpoints(en, planned) if v in bars and bars[v][4] > 0]
                fill = op + side * tick(op if side > 0 else op - 1)
                for j, m in enumerate(points):
                    next_m = points[j + 1] if j + 1 < len(points) else ex
                    px = bars[m][0]
                    next_px = bars[next_m][0]
                    unit = t.scale[d, s]
                    inc = (side * (next_px - px) * MULT + cost(px, -side) - cost(next_px, -side)) / unit
                    xs.append(exit_state(bars, m, en, fill, side, planned, unit))
                    delta.append(inc)
                    nxt.append(len(xs) if j + 1 < len(points) else -1)
                    depth.append(len(points) - j)
                    ids.append(d)
    ids = np.asarray(ids, dtype=int)
    weights = np.exp2((ids - len(t.days) + 1) / 252) / np.bincount(ids)[ids]
    return Samples(np.stack(xs), np.array(delta), np.array(nxt), np.array(depth), ids, weights, t.days)


class ExitPolicy:
    def __init__(self, weights, margin):
        self.weights, self.margin = weights, margin

    def value(self, x):
        heads = x @ self.weights.T
        avg = heads.mean(axis=-1)
        se = np.sqrt(0.75 * np.square(heads - avg[..., None]).sum(axis=-1))
        return avg + self.margin * se

    def save(self, path):
        part = path.with_suffix(".partial")
        with part.open("wb") as f:
            np.savez(f, weights=self.weights, margin=self.margin)
        part.replace(path)

    @classmethod
    def load(cls, path):
        with np.load(path, allow_pickle=False) as z:
            return cls(z["weights"], float(z["margin"]))


def returns(p, model):
    y = p.inc.copy()
    hold = np.ones(len(y), bool) if model is None else model.value(p.x) >= 0.0
    for depth in range(2, int(p.depth.max()) + 1):
        ids = np.flatnonzero(p.depth == depth)
        nxt = p.following[ids]
        if np.any(nxt < 0) or np.any(p.day[nxt] != p.day[ids]):
            raise ValueError("Exit target crossed session boundary")
        y[ids] += hold[nxt] * y[nxt]
    return y


def estimate(p, dates, y, margin):
    chosen = np.isin(p.day, dates)
    heads = []
    for group in range(4):
        rows = chosen & ((p.day // 5) % 4 != group)
        heads.append(ridge(p.x[rows], y[rows].clip(-4, 4), p.weight[rows]))
    return ExitPolicy(np.stack(heads), margin)


def learn(p, dates, margin):
    labels = np.zeros(len(p.day))
    for block in np.array_split(dates, 4):
        past = dates[dates < block[0]]
        model = None
        if len(past):
            for _ in range(3):
                model = estimate(p, past, returns(p, model), margin)
        mask = np.isin(p.day, block)
        labels[mask] = returns(p, model)[mask]
    return estimate(p, dates, labels, margin)
