"""Refit the selected 12-minute forecasters on the pinned NQ minute archive."""

import argparse
import hashlib
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import duckdb
import joblib
import numpy as np
from lightgbm import LGBMClassifier, LGBMRegressor
from scipy.ndimage import gaussian_filter1d
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from forecast import KST, ROOT, forecast, from_rows, mirror, targets


def read_window(database, parquet, first, last):
    first, last = date.fromisoformat(first), date.fromisoformat(last)
    dates = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    weekdays = [day for day in dates if day.weekday() < 5]
    start = datetime.combine(weekdays[0], time(), KST)
    end = datetime.combine(weekdays[-1] + timedelta(days=1), time(), KST)
    data = database.execute(
        "SELECT epoch(ts)::BIGINT stamp, open, high, low, close, volume FROM read_parquet(?) "
        "WHERE stamp >= ? AND stamp < ? ORDER BY stamp",
        [str(parquet), int(start.timestamp()), int(end.timestamp())],
    ).fetchnumpy()
    prices = np.column_stack([data[col] for col in ("open", "high", "low", "close", "volume")])
    return from_rows("MNQ", weekdays, data["stamp"], prices)


def clock_curve(clock, y):
    counts = np.bincount(clock, minlength=1440)
    sums = np.bincount(clock, weights=y, minlength=1440)
    return gaussian_filter1d(sums.astype(float), 9, mode="wrap") / (
        gaussian_filter1d(counts.astype(float), 9, mode="wrap") + 24
    )


def fit(parquet, destination):
    settings = json.loads((ROOT / "strategy.json").read_text(encoding="utf-8"))
    with parquet.open("rb") as f:
        if hashlib.file_digest(f, "sha256").hexdigest() != settings["training_source"]["sha256"]:
            raise ValueError("NQ source hash differs from the released archive")
    if destination.exists():
        raise FileExistsError(f"Choose a new output file: {destination}")
    hyper = settings["training"]["hyperparameters"]
    rng = np.random.default_rng(hyper["random_state"])
    samples = []
    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'")
        for year in range(2017, 2025):
            tape = read_window(db, parquet, f"{year}-01-01", f"{year}-12-31")
            # The 12- and 30-minute candidates used the same sample and lookahead eligibility.
            observed = tape.price[:, :, 0] > 0
            contiguous = np.lib.stride_tricks.sliding_window_view(observed, 32, axis=1).all(axis=-1)
            days, slots = np.nonzero(tape.valid[:, : contiguous.shape[1]] & contiguous)
            picked = np.sort(rng.choice(len(slots), min(len(slots), 52000), replace=False))
            d, m = days[picked], slots[picked]
            samples.append((tape.x[d, m], tape.ny[d, m], tape.london[d, m], *targets(tape, d, m, 12)))
        calibration = read_window(db, parquet, *settings["chronology"]["calibration_proxy"])
    x, ny, london, y, barrier = (np.concatenate(parts) for parts in zip(*samples))
    xx = np.concatenate([x, mirror(x)])
    yy = np.r_[y, -y]
    classifier_mask = np.r_[barrier != 0, barrier != 0]
    classes = np.r_[barrier, -barrier][classifier_mask] > 0
    huber = LGBMRegressor(objective="huber", alpha=0.9, **hyper).fit(xx, yy)
    classifier = LGBMClassifier(**hyper).fit(xx[classifier_mask], classes)
    with threadpool_limits(limits=2):
        ridge = make_pipeline(StandardScaler(), Ridge(alpha=6000)).fit(xx, yy)
    model = dict(
        ridge=ridge,
        huber=huber.booster_,
        barrier=classifier.booster_,
        ny=clock_curve(ny, np.clip(y, -2.5, 2.5)),
        london=clock_curve(london, np.clip(y, -2.5, 2.5)),
        cuts={},
    )
    d, m = np.nonzero(calibration.valid)
    for leg in settings["rule"]["components"]:
        name, q = leg["model"], leg["quantile"]
        values = forecast(model, calibration.x[d, m], calibration.ny[d, m], calibration.london[d, m], name)
        cut = float(np.quantile(np.abs(values), q))
        if not np.isclose(cut, leg["cut"], atol=1e-6, rtol=1e-5):
            raise ValueError(f"{name} calibration changed: {cut}")
        model["cuts"][name] = {str(q): cut}
        print(name, "calibrated threshold", cut)
    joblib.dump({12: model}, destination, compress=3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("parquet", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    fit(args.parquet, args.output)
