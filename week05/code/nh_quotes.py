"""Download the 70 overlapping NH chart windows, or rebuild them from the original page cache."""

import argparse
from contextlib import closing
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import gzip
from hashlib import sha256
from io import TextIOWrapper
import json
from pathlib import Path
import sqlite3
import sys
import time

KST = timezone(timedelta(hours=9))
ASSETS = {"hynix": ("A506C000", "15000000")}
RAW_FIELDS = tuple(
    "occurs1_" + name
    for name in ("ref_dt", "exch_tm", "open_pric", "high_pric", "low_pric", "close_pric", "cum_trd_qty")
)
FIRST, END = date(2022, 11, 19), date(2026, 11, 19)
CAP = 2000


class Unavailable(RuntimeError):
    pass


def nh_client(environment, timeout=5.0):
    import httpx
    import truststore
    from backend.auth.auth_functions import TokenManager
    from backend.config import DEFAULT_BASE_URLS, load_config
    from backend.nh_api.client import TradeClient
    from backend.nh_api.session import LIVE, Session, replay_enabled

    config = load_config()
    if replay_enabled() or config.environment(environment).base_url != DEFAULT_BASE_URLS[environment]:
        raise Unavailable("official NH host required")
    config.market("domestic", environment)
    truststore.inject_into_ssl()
    http = httpx.Client(timeout=timeout)
    return TradeClient(Session(LIVE, config, http, TokenManager(config, client=http)))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha(path):
    return sha256(path.read_bytes()).hexdigest()


def windows(start, stop, core, overlap):
    d = start
    while d < stop:
        yield (
            max(start, d - timedelta(days=overlap)),
            min(stop, d + timedelta(days=core)) - timedelta(days=1),
        )
        d += timedelta(days=core)


def database(folder):
    folder.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(folder / "nh_chart_pages.sqlite3", timeout=30)
    db.execute("""CREATE TABLE IF NOT EXISTS pages (
        asset TEXT, interval TEXT, lo TEXT, hi TEXT, acquired_kst TEXT, digest TEXT, packed BLOB,
        PRIMARY KEY(asset,interval,lo,hi))""")
    return db


def page(db, client, asset, interval, lo, hi, *, optional=False, outright=None):
    key = (asset, interval if not outright else f"{interval}/{outright}", lo.isoformat(), hi.isoformat())
    record = db.execute(
        "SELECT digest,packed FROM pages WHERE asset=? AND interval=? AND lo=? AND hi=?", key
    ).fetchone()
    if record is not None:
        raw = gzip.decompress(record[1])
        if sha256(raw).hexdigest() != record[0]:
            raise ValueError(f"Damaged raw chart receipt {key}")
        return json.loads(raw)
    if client is None:
        raise FileNotFoundError(f"Missing cached NH request {key}")
    from backend.nh_api.errors import BusinessError, GatewayError, HttpError, TransportError

    symbol, linked = (outright, outright) if outright else ASSETS[asset]
    request = dict(
        sym=symbol,
        inq_strt_dt=lo.strftime("%Y%m%d"),
        inq_ed_dt=hi.strftime("%Y%m%d"),
        nght_tp="1",
        cidx_yn="0" if outright else "1",
        fut_cidx_exp_mod="0",
        nxt_key="99999999",
        req_qty="9999",
    )
    endpoint = "domestic_chart-daily" if interval == "daily" else "domestic_chart-minute"
    if interval != "daily":
        request.update(dcnt=interval, strt_tm="000000", ed_tm="235959")
    for attempt in range(4):
        try:
            body = client.call(endpoint, request, environment="real").body
            rows = body.get("Occurs1") or []
            reported_symbol = str(body.get("sym") or "").strip()
            raw_count = str(body.get("nrec") or "").strip()
            empty_header = not raw_count and not rows and optional
            if (
                not empty_header
                and (not raw_count.isdigit() or int(raw_count) != len(rows))
                or rows
                and reported_symbol != linked
            ):
                raise ValueError(f"NH chart count/symbol mismatch {key}")
            response = dict(
                request=request,
                endpoint=endpoint,
                symbol=reported_symbol,
                expected_symbol=linked,
                next_key=body.get("nxt_key"),
                status="empty_header" if empty_header else "ok",
                rows=[[r.get(k, "") for k in RAW_FIELDS] for r in rows],
            )
            break
        except (BusinessError, GatewayError, HttpError, TransportError) as exc:
            retry = (
                isinstance(exc, TransportError)
                or getattr(exc, "kind", None) == "retry"
                or getattr(exc, "status", None) in (408, 429, 500, 502, 503, 504)
            )
            if retry and attempt < 3:
                time.sleep(2**attempt)
                continue
            if (
                optional
                and not retry
                and (isinstance(exc, BusinessError) or getattr(exc, "status", None) in (400, 404))
            ):
                response = dict(
                    request=request,
                    endpoint=endpoint,
                    expected_symbol=linked,
                    status="refused",
                    provider_code=str(getattr(exc, "code", getattr(exc, "status", ""))),
                    rows=[],
                )
                break
            raise
    raw = canonical(response).encode("utf-8")
    with db:
        db.execute(
            "INSERT OR IGNORE INTO pages VALUES (?,?,?,?,?,?,?)",
            (*key, datetime.now(KST).isoformat(), sha256(raw).hexdigest(), gzip.compress(raw, mtime=0)),
        )
    time.sleep(0.42)
    return response


def complete_pages(db, client, asset, interval, lo, hi, *, optional=False):
    p = page(db, client, asset, interval, lo, hi, optional=optional)
    if len(p["rows"]) < CAP:
        yield p
        return
    if lo == hi:
        raise ValueError(f"NH truncated a single-day {asset} {interval} request")
    mid = lo + (hi - lo) // 2
    yield from complete_pages(db, client, asset, interval, lo, mid, optional=optional)
    yield from complete_pages(
        db, client, asset, interval, mid + timedelta(days=1), hi, optional=optional
    )


def values(row):
    o, h, l, c, v = map(Decimal, row[2:])
    if (
        not all(x.is_finite() for x in (o, h, l, c, v))
        or not 0 < l <= min(o, c) <= max(o, c) <= h
        or v < 0
        or int(v) != v
    ):
        raise ValueError(f"Invalid NH OHLCV at {row[:2]}")
    return o, h, l, c, int(v)


def merge(table, key, value):
    old = table.get(key)
    if old is None:
        table[key] = value
        return 0
    if old != value:
        raise ValueError(f"Conflicting market records at {key}: {old} vs {value}")
    return 1


def checked_rows(page, asset, interval, linked=True, outright=None):
    if page.get("status") == "refused":
        return
    request = page["request"]
    front, continuous = (outright, outright) if outright else ASSETS[asset]
    if (
        request["sym"] != front
        or request["cidx_yn"] != str(int(linked))
        or request["nght_tp"] != "1"
        or request["fut_cidx_exp_mod"] != "0"
        or interval != "daily"
        and request["dcnt"] != interval
    ):
        raise ValueError("Wrong frequency, session or contract")
    if page["rows"] and page["symbol"].strip() != (continuous if linked else front):
        raise ValueError("Unexpected returned instrument")
    for row in page["rows"]:
        day, tm = row[:2]
        if (
            interval == "daily"
            and date.fromisoformat(day) == END
            and request["inq_ed_dt"] == END.strftime("%Y%m%d")
        ):
            continue
        if (
            not request["inq_strt_dt"] <= day <= request["inq_ed_dt"]
            or not FIRST <= date.fromisoformat(day) < END
        ):
            raise ValueError(f"NH price outside its date range: {day}")
        if interval != "daily" and (
            len(tm) != 6
            or tm[4:] != "00"
            or int(tm[2:4]) % int(interval)
            or int(tm[:2]) > 23
            or int(tm[2:4]) > 59
        ):
            raise ValueError(f"Off-grid {interval}-minute candle {day}/{tm}")
        yield (day, tm) if interval != "daily" else day, values(row)


def save_bars(folder, asset, table, origins):
    file = folder / f"{asset}_5m.csv.gz"
    partial = file.with_suffix(".partial")
    with (
        partial.open("wb") as output,
        gzip.GzipFile(fileobj=output, filename="", mode="wb", mtime=0) as zipped,
    ):
        with TextIOWrapper(zipped, encoding="utf-8", newline="") as text_stream:
            writer = csv.writer(text_stream, lineterminator="\n")
            writer.writerow(("time_kst", "open", "high", "low", "close", "volume", "source"))
            for (day, tm), val in sorted(table.items()):
                ts = datetime.strptime(day + tm, "%Y%m%d%H%M%S").replace(tzinfo=KST)
                writer.writerow(
                    (ts.isoformat(timespec="minutes"), *val, origins.get((day, tm), "native_5m"))
                )
    partial.replace(file)
    return sha(file)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--app", type=Path, help="NH app root with official domestic real quotation credentials"
    )
    source.add_argument("--cache", type=Path, help="Existing nh_chart_pages.sqlite3, read-only")
    parser.add_argument("--out", type=Path, required=True, help="New output directory")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if (args.out / "hynix_5m.csv.gz").exists():
        parser.error("An output archive already exists in this directory")
    client = None
    if args.app:
        sys.path.insert(0, str(args.app.resolve()))
        client = nh_client("real", timeout=20)
        db = database(args.out)
    else:
        db = sqlite3.connect(args.cache.resolve().as_uri() + "?mode=ro", uri=True)
    bars = {}
    pages = total = duplicates = 0
    try:
        with closing(db):
            for low, high in windows(FIRST, END, 21, 4):
                for receipt in complete_pages(db, client, "hynix", "5", low, high):
                    pages += 1
                    for key, value in checked_rows(receipt, "hynix", "5"):
                        total += 1
                        duplicates += merge(bars, key, value)
        if not bars or total - duplicates != len(bars):
            raise ValueError("The page and timestamp counts do not reconcile")
        checksum = save_bars(args.out, "hynix", bars, {})
        print(
            f"{pages} pages, {total} rows, {duplicates} duplicates, {len(bars)} unique. SHA-256 {checksum}"
        )
    finally:
        if client is not None:
            client.session.close()


if __name__ == "__main__":
    main()
