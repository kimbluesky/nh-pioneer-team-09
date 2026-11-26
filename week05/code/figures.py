"""Rebuild all five report figures and both statistical tables from the published study."""

import argparse
import csv
import json
from datetime import date, time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.lines import Line2D
from matplotlib.ticker import MaxNLocator, StrMethodFormatter
import numpy as np

from market import MULT
from performance import METHODS, SPLITS, compare, equity_curve, learning_curve, write_csv
from reproduce import (
    RESULTS,
    check,
    clock,
    load_market,
    load_models,
    read_json,
    verify_backtests,
    verify_paper,
)

from validation import training_examples


INK, SHORT, LONG = "#164966", "#ac501e", "#687882"
SELL, BUY, GRID = "#12638b", "#c35c18", "#dce3e6"
METHOD_STYLE = {
    "policy": ("학습 전략", INK, "-"),
    "short": ("단순 매도", SHORT, "--"),
    "long": ("단순 매수", LONG, ":"),
}


def style():
    available = {f.name for f in font_manager.fontManager.ttflist}
    family = next(
        (
            f
            for f in (
                "Malgun Gothic",
                "AppleGothic",
                "Noto Sans CJK KR",
                "Noto Sans KR",
                "NanumGothic",
            )
            if f in available
        ),
        None,
    )
    if family is None:
        raise RuntimeError("Install a Korean font, e.g. Noto Sans CJK KR, before rendering.")
    plt.rcParams.update(
        {
            "font.family": family,
            "font.size": 10,
            "axes.unicode_minus": False,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.edgecolor": "#a6b3b9",
            "axes.labelcolor": "#2f444e",
            "text.color": "#203640",
            "xtick.color": "#425862",
            "ytick.color": "#425862",
            "axes.titleweight": "bold",
            "axes.titlelocation": "left",
            "axes.axisbelow": True,
            "legend.frameon": False,
            "figure.facecolor": "white",
        }
    )


def load_csv(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def save(fig, dest):
    try:
        fig.savefig(dest, dpi=155, facecolor="white")
    finally:
        plt.close(fig)


def grid(ax):
    ax.grid(axis="y", color=GRID, linewidth=0.7)
    ax.tick_params(length=3)


def training_plot(rows, dest):
    fig, ax = plt.subplots(figsize=(8.2, 3.7), layout="constrained")
    counts = [r["entry_days"] for r in rows]
    values = np.array([float(r["validation_return_pct"]) for r in rows])
    baseline = float(rows[-1]["fixed_short_return_pct"])
    ax.axhline(
        baseline, color=SHORT, linestyle="--", linewidth=1.5, label=f"단순 매도  {baseline:+.2f}%"
    )
    ax.plot(counts, values, marker="o", color=INK, linewidth=2.3, label="학습 전략")
    ax.axhline(0, color="#8b999e", linewidth=0.8)
    for x, y in zip(counts, values, strict=True):
        ax.annotate(
            f"{y:+.2f}%",
            (x, y),
            xytext=(0, -16 if y < 0 else 9),
            textcoords="offset points",
            ha="center",
            fontsize=10,
            weight="bold",
            color=INK,
        )
    ax.set_xticks(counts, [f"{r['entry_days']} / {r['exit_days']}일" for r in rows])
    ax.set(
        xlabel="진입 학습일 / 청산 학습일",
        ylabel="검증 순수익률 (%)",
        xlim=(counts[0] - 40, counts[-1] + 40),
        ylim=(min(values.min(), 0) - 4, max(values.max(), baseline) + 7),
    )
    ax.legend(loc="lower right")
    grid(ax)
    save(fig, dest)


def equity_plot(ledger, dest):
    fig, axes = plt.subplots(
        2, 2, figsize=(10, 5.8), height_ratios=[2.0, 1.0], sharex="col", layout="constrained"
    )
    for col, split in enumerate(SPLITS):
        rows = [r for r in ledger if r["split"] == split]
        notional = int(rows[0]["entry_reference"]) * MULT
        top, lower = axes[:, col]
        for method, field in METHODS.items():
            profits = np.array([float(r[field]) for r in rows])
            cum, dd, _ = equity_curve(profits, notional)
            label, color, fmt = METHOD_STYLE[method]
            top.plot(
                cum, fmt, color=color, label=label, linewidth=2.1 if method == "policy" else 1.5
            )
            if method != "long":
                lower.plot(dd, fmt, color=color, linewidth=1.6)
        n = len(rows)
        ticks = [1, 1 + n // 3, 1 + 2 * n // 3, n]
        lower.set_xticks(ticks, [rows[t - 1]["date"][5:].replace("-", "/") for t in ticks])
        top.set_title(
            ("검증" if split == "validation" else "테스트") + f"   {n}거래일", fontsize=13
        )
        top.set_xlim(0, n + 0.3)
        top.axhline(0, color="#8b999e", linewidth=0.8)
        lower.axhline(0, color="#8b999e", linewidth=0.8)
        if col == 0:
            top.set_ylabel("누적 순수익률 (%)")
            lower.set_ylabel("직전 고점 대비 (%)")
        top.legend(loc="best", fontsize=9)
        grid(top)
        grid(lower)
    save(fig, dest)


def minutes(stamp):
    t = time.fromisoformat(stamp)
    return t.hour * 60 + t.minute + t.second / 60


def draw_candles(ax, start_minutes, ohlc, linewidth=0.85):
    centers = np.asarray(start_minutes, dtype=float) + 2.5
    opening, high, low, closing = np.asarray(ohlc, dtype=float).T / 1000
    colors = np.where(closing >= opening, "#b07463", "#5e8297")
    ax.vlines(centers, low, high, colors=colors, linewidth=linewidth)
    ax.bar(centers, closing - opening, bottom=opening, width=3.1, color=colors, linewidth=0)
    flat = closing == opening
    ax.hlines(
        opening[flat],
        centers[flat] - 1.55,
        centers[flat] + 1.55,
        colors=colors[flat],
        linewidth=1.3 * linewidth,
    )
    return low.min(), high.max()


def historical_session(ax, stock, row, *, volume_ax=None):
    day = date.fromisoformat(row["date"])
    bars = [(m, b) for m, b in sorted(stock[day].items()) if 540 <= m <= 930 and b[4] > 0]
    times = [m for m, _ in bars]
    en, ex = minutes(row["entry"]), minutes(row["exit"])
    check(en in times and ex in times, f"Missing fill reference on {day}")
    buy_to_enter = row["direction"] == "long"
    small = volume_ax is None
    ax.axvspan(en, ex, color="#e8eff1", zorder=0)
    low, high = draw_candles(ax, times, [b[:4] for _, b in bars], 0.55 if small else 0.95)
    for t, fill, buy in (
        (en, row["entry_fill"], buy_to_enter),
        (ex, row["exit_fill"], not buy_to_enter),
    ):
        y = float(fill) / 1000
        ax.scatter(
            t,
            y,
            marker="^" if buy else "v",
            s=52 if small else 90,
            color=BUY if buy else SELL,
            edgecolor="white",
            linewidth=0.65,
            zorder=5,
        )
        low, high = min(low, y), max(high, y)
    pad = max(1.0, 0.09 * (high - low))
    ax.set_ylim(low - pad, high + pad)
    ax.set_xlim(538, 936)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=3, integer=True))
    ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
    grid(ax)
    if volume_ax is not None:
        volume_ax.bar(
            np.asarray(times) + 2.5, [b[4] / 1000 for _, b in bars], width=3.1, color="#8da2aa"
        )
        volume_ax.set_ylim(bottom=0)
        volume_ax.yaxis.set_major_locator(MaxNLocator(nbins=2, integer=True, prune="lower"))
        volume_ax.set_ylabel("거래량\n(천 계약)", fontsize=8, rotation=0, ha="right", va="center")
        volume_ax.tick_params(labelsize=8, length=2)
        volume_ax.set_xticks(
            [540, 630, 720, 810, 900], ["09:00", "10:30", "12:00", "13:30", "15:00"]
        )
        ax.tick_params(labelbottom=False)
        ax.set_ylabel("가격 (천원)", fontsize=9)


def split_fills_plot(examples, ledger, stock, dest):
    rows = []
    for i, trade in enumerate(examples, 1):
        rows.append(
            (
                f"학습 기간 · 내부 평가 {i}",
                dict(
                    date=trade["day"],
                    entry=clock(trade["entry"]),
                    exit=clock(trade["exit"]),
                    scheduled_exit=clock(trade["original_exit"]),
                    direction="long" if trade["side"] > 0 else "short",
                    entry_fill=trade["entry_fill"],
                    exit_fill=trade["exit_fill"],
                    net_krw=trade["net_krw"],
                ),
            )
        )
    for split, title in (("validation", "검증"), ("test", "테스트")):
        rows.append((title, next(r for r in ledger if r["split"] == split)))
    fig = plt.figure(figsize=(10.4, 10.2), layout="constrained")
    outer = fig.add_gridspec(len(rows), 2, width_ratios=(4.4, 1.35))
    fig.suptitle(
        "SK하이닉스 주식선물 5분봉   |   구간별 첫 거래",
        x=0.055,
        ha="left",
        fontsize=16,
        weight="bold",
        color=INK,
    )
    fig.supxlabel(
        "한국시간   ·   NH 주간 연결선물   ·   추정 체결가에 편도 1틱 슬리피지 적용", fontsize=9
    )
    for i, (title, row) in enumerate(rows):
        sub = outer[i, 0].subgridspec(2, 1, height_ratios=(3.6, 0.92), hspace=0.035)
        ax = fig.add_subplot(sub[0])
        vol = fig.add_subplot(sub[1], sharex=ax)
        historical_session(ax, stock, row, volume_ax=vol)
        ax.set_title(f"{title}    {row['date'].replace('-', '.')}", fontsize=11, pad=5)
        legend = fig.add_subplot(outer[i, 1])
        legend.set_axis_off()
        long = row["direction"] == "long"
        for y, buy, action, at, fill in (
            (0.80, long, "진입", row["entry"], row["entry_fill"]),
            (0.51, not long, "청산", row["exit"], row["exit_fill"]),
        ):
            color = BUY if buy else SELL
            legend.scatter(
                [0.04],
                [y],
                transform=legend.transAxes,
                marker="^" if buy else "v",
                s=58,
                color=color,
            )
            legend.text(
                0.14,
                y,
                f"{'매수' if buy else '매도'} {action}  {at}",
                transform=legend.transAxes,
                va="center",
                fontsize=10,
                weight="bold",
                color=color,
            )
            legend.text(
                0.14, y - 0.115, f"{int(fill):,}원", transform=legend.transAxes, fontsize=10
            )
        reason = "조기 청산" if row["exit"] < row["scheduled_exit"] else "예정 청산"
        legend.text(0.03, 0.18, reason, transform=legend.transAxes, fontsize=9)
        legend.text(
            0.03,
            0.06,
            f"순손익 {float(row['net_krw']):+,.2f}원",
            transform=legend.transAxes,
            fontsize=10,
            weight="bold",
        )
    save(fig, dest)


def trade_grid(ledger, stock, dest):
    rows = [r for r in ledger if r["split"] == "test"]
    check(all(r["direction"] == "short" for r in rows), "Test marker labels require short trades")
    cols = 3
    fig, axes = plt.subplots((len(rows) + cols - 1) // cols, cols, figsize=(11.3, 15))
    fig.subplots_adjust(top=0.950, bottom=0.04, left=0.062, right=0.984, hspace=1.04, wspace=0.25)
    handles = [
        Line2D([], [], linestyle="none", marker=symbol, color=color, markersize=8, label=txt)
        for symbol, color, txt in (("v", SELL, "매도 진입"), ("^", BUY, "매수 청산"))
    ]
    fig.legend(handles=handles, loc="upper center", ncol=2, bbox_to_anchor=(0.5, 1))
    for ax, r in zip(axes.flat, rows):
        historical_session(ax, stock, r)
        ax.set_title(
            f"{r['date'][5:].replace('-', '.')}   {r['entry']}–{r['exit']}", fontsize=9, pad=17
        )
        ax.set_title(
            f"{float(r['trade_return_pct']):+.2f}%",
            loc="right",
            fontsize=9,
            pad=17,
            color=INK if float(r["net_krw"]) >= 0 else SHORT,
        )
        ax.text(
            0,
            1.035,
            f"진입 {int(r['entry_fill']):,}원   ·   청산 {int(r['exit_fill']):,}원",
            transform=ax.transAxes,
            fontsize=7.5,
            color="#40555e",
        )
        ax.set_xticks([540, 720, 900], ["09시", "12시", "15시"])
        ax.tick_params(labelsize=7.5, pad=1, length=2)
    for ax in list(axes.flat)[len(rows) :]:
        ax.set_visible(False)
    fig.text(
        0.5,
        0.012,
        "NH 연결선물 5분봉   ·   한국시간   ·   가격 단위 천원   ·   추정 체결가에 편도 1틱 적용",
        ha="center",
    )
    save(fig, dest)


def paper_plot(dest):
    log = read_json(RESULTS / "strategy-runs.json")
    (run,) = log["runs"]
    bars = [
        r
        for r in load_csv(RESULTS / "paper_bars.csv")
        if r["date"] == run["date"] and int(r["minute"]) >= 750 and int(r["volume"]) > 0
    ]
    events = [
        json.loads(line)
        for line in (RESULTS / "trades.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    fills = [e["event"] for e in events if e["kind"] == "confirmed_fill"]
    holds = [e["event"] for e in log["events"] if e["kind"] == "paper_exit_signal"]
    fig, (ax, lower) = plt.subplots(
        2, 1, figsize=(9.4, 5.2), sharex=True, height_ratios=[2.4, 1], layout="constrained"
    )
    low, high = draw_candles(
        ax,
        [int(r["minute"]) for r in bars],
        [[float(r[k]) for k in ("open", "high", "low", "close")] for r in bars],
        1.0,
    )
    for f in fills:
        opening = f["role"] == "entry"
        x, y = minutes(f["broker_fill_time"]), float(f["price"]) / 1000
        color = SELL if opening else BUY
        ax.scatter(
            x, y, marker="v" if opening else "^", s=85, color=color, edgecolor="white", zorder=5
        )
        label = "매도" if opening else "매수"
        ax.annotate(
            f"{label} {f['broker_fill_time']}\n{int(f['price']):,}원",
            (x, y),
            xytext=(6 if opening else -6, 16),
            textcoords="offset points",
            ha="left" if opening else "right",
            fontsize=9,
            weight="bold",
            color=color,
        )
    span = high - low
    ax.set_ylim(low - 0.10 * span, high + 0.15 * span)
    ax.set_ylabel("12월물 가격 (천원)")
    ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
    values = [v["incremental_hold_value"] for v in holds]
    lower.axhline(0, color="#788b94", linewidth=1.1)
    lower.vlines([e["minute"] for e in holds], 0, values, color=INK, linewidth=1.3)
    for e, y in zip(holds, values, strict=True):
        x = e["minute"]
        lower.scatter(x, y, s=35, color=INK)
        lower.annotate(
            f"{clock(x)}  {y:+.4f}",
            (x, y),
            xytext=(0, 8),
            textcoords="offset points",
            ha="center",
            fontsize=9,
            color=INK,
        )
    lower.set_ylim(-0.0012, max(values) * 1.57)
    lower.set_yticks([0, 0.004, 0.008, 0.012])
    lower.set_ylabel("추가 보유 가치")
    lower.set_xlim(748, 936)
    ticks = list(range(750, 931, 30))
    lower.set_xticks(ticks, [clock(v) for v in ticks])
    lower.set_xlabel("11월 20일 한국시간")
    grid(ax)
    grid(lower)
    save(fig, dest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=RESULTS, help="Output directory")
    args = parser.parse_args()
    args.out.mkdir(exist_ok=True, parents=True)
    style()
    study = read_json(RESULTS / "study.json")
    stock, calendar = load_market(study)
    models = load_models(study)
    ledger = verify_backtests(stock, calendar, models, study)
    check(
        load_csv(RESULTS / "backtest.csv") == [{k: str(v) for k, v in r.items()} for r in ledger],
        "Published ledger differs",
    )
    verify_paper(stock, *models, study)
    table = compare(ledger, study)
    curve = learning_curve(stock, calendar, models, ledger)
    write_csv(args.out / "performance.csv", table)
    write_csv(args.out / "training_curve.csv", curve)
    training_plot(curve, args.out / "training-curve.png")
    equity_plot(ledger, args.out / "test-equity.png")
    examples = training_examples(stock, calendar, read_json(RESULTS / "tuning.json"))
    split_fills_plot(examples, ledger, stock, args.out / "split-fills.png")
    trade_grid(ledger, stock, args.out / "test-fills.png")
    paper_plot(args.out / "paper-fills.png")


if __name__ == "__main__":
    main()
