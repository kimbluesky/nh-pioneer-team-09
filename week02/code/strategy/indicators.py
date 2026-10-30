"""지표 계산 한 벌.

**여기가 유일한 계산 자리입니다.** 같은 지표를 화면과 엔진이 따로 세면, 차트에
그려진 선과 엔진이 본 값이 어긋나는 날 「화면에서는 교차했는데 왜 안 샀나」에
답할 수 없습니다.

## 값이 없는 자리는 `None` 입니다

봉이 모자라면 0 이 아니라 `None` 입니다. 0 으로 채우면 조건식이 「RSI 0」을 참인
값으로 읽어 **준비도 안 된 지표로 주문이 나갑니다.** 받는 쪽은 `None` 을 「아직
모름」으로 다루어야 합니다.

## 계산법을 여기 적어 둡니다

명세에도 원본 기획에도 계산법이 없어 **널리 쓰는 것을 골라 적어 둡니다.** 고를
때마다 값이 달라지는 자리라, 바꾸려면 이 문단을 함께 고쳐야 합니다.

    SMA    단순 이동평균.
    EMA    첫 값은 **SMA 로 시작**하고(period 번째 봉) 그 뒤로 2/(n+1) 로 이어
           갑니다. 첫 값을 종가 하나로 잡으면 앞쪽 값이 한참 흔들립니다.
    RSI    **Wilder 평활**(1/n). 첫 평균은 처음 n 개의 상승·하락 평균입니다.
    BB     이동평균 ± σ×배수, **모집단 표준편차**(n 으로 나눔). 표본(n−1)로 세면
           같은 이름의 밴드가 조금 좁아집니다.
    ATR    **Wilder 평활**한 TR. TR 은 max(고−저, |고−전일종가|, |저−전일종가|)
           이고 첫 봉은 전일 종가가 없어 고−저입니다.
    MACD   선(DIF) = EMA(단기) − EMA(장기), 신호(DEA) = DIF 의 EMA(신호 기간)
           (DIF 가 처음 선 자리부터 위 EMA 와 같은 SMA 시드), 막대 = DIF − DEA.
           막대에 2 를 곱하지 않습니다.
    스토캐스틱  원값 = (종가 − n봉 최저) ÷ (n봉 최고 − n봉 최저) × 100, %K = 원값의
           SMA(K), %D = %K 의 SMA(D). n봉 최고와 최저가 같으면(값이 하나도 안
           움직임) 원값은 가운데인 50 입니다. K·D 가 1 이면 평활이 없습니다.
    VWAP   **세션마다 처음부터 다시** 셉니다. 전형가 (고+저+종)/3 에 거래량을
           곱한 값의 누적 ÷ 누적 거래량입니다. 세션은 봉의 **거래일과 세션 창**
           (주간·야간)이 같은 봉들이고, 창이 바뀌거나 거래일이 바뀌는 봉에서
           새로 시작합니다. 야간이 자정을 넘겨도 같은 거래일이라 이어집니다.
           해외는 거래소 거래일(시간표의 거래 창)로 가르고, 시간표를 모르면 한국
           시각의 달력 날짜로 가릅니다(`rules._vwap_marks`).
           누적 거래량이 0 이면 나눌 수 없어 값이 없습니다(`None`).

## 준비 봉

`warmup_bars(기간)` = **기간 × 2** 입니다. 매개변수가 여럿인 지표는 값이 서기까지
거치는 봉을 더한 것을 기간으로 봅니다 — MACD 는 장기 + 신호, 스토캐스틱은
n + K + D 입니다. VWAP 은 첫 봉부터 값이 있어 준비 봉이 없습니다. Wilder 평활은 앞쪽 값이 초기값에
끌려다녀서, 기간만큼만 모으면 같은 자리에서 다른 값이 나옵니다. 배수를 바꾸면
신호가 달라지므로 한 자리에 둡니다.

위의 식들은 **기간만 차면 값을 냅니다.** 준비 봉이 찰 때까지 앞쪽을 지우는 것은
`warmed()` 한 자리에서 하고, 조건 판정이 쓰는 열은 `rules.series_of()` 가 그것을
거쳐 만듭니다. 식과 자르는 자리를 갈라 두면 시드 값이 맞는지 따로 잴 수 있습니다.
"""
from __future__ import annotations

from dataclasses import dataclass

#: 준비 봉 배수. 지표 기간의 몇 배를 모아야 값을 믿을지입니다.
WARMUP_FACTOR = 2


def warmup_bars(period: int) -> int:
    """그 기간의 지표를 믿으려면 있어야 하는 봉 수."""
    return max(int(period), 1) * WARMUP_FACTOR


def warmed(column: list[float | None], period: int) -> list[float | None]:
    """준비 봉이 차기 전의 앞쪽을 `None` 으로 지운 열.

    믿을 수 있는 값만 남깁니다. 지우지 않으면 시드에 끌려다니는 앞쪽 값으로
    조건이 걸려, 같은 종목·같은 규칙인데 언제 시작했느냐로 신호가 갈립니다.
    """
    ready = warmup_bars(period)
    made = list(column)
    for at in range(min(ready - 1, len(made))):
        made[at] = None
    return made


@dataclass(frozen=True)
class Bar:
    """봉 하나. 시각은 밀리초이고 순서는 **오래된 것부터**입니다."""

    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def sma(values: list[float], period: int) -> list[float | None]:
    """단순 이동평균. 봉이 모자라는 앞쪽은 `None` 입니다."""
    period = max(int(period), 1)
    made: list[float | None] = [None] * len(values)
    total = 0.0
    for at, value in enumerate(values):
        total += value
        if at >= period:
            total -= values[at - period]
        if at >= period - 1:
            made[at] = total / period
    return made


def ema(values: list[float], period: int) -> list[float | None]:
    """지수 이동평균. **첫 값은 SMA 로 시작합니다** — 까닭은 이 파일 첫머리에."""
    period = max(int(period), 1)
    made: list[float | None] = [None] * len(values)
    if len(values) < period:
        return made
    weight = 2.0 / (period + 1)
    previous = sum(values[:period]) / period
    made[period - 1] = previous
    for at in range(period, len(values)):
        previous = (values[at] - previous) * weight + previous
        made[at] = previous
    return made


def rsi(closes: list[float], period: int) -> list[float | None]:
    """상대강도지수. **Wilder 평활**입니다.

    오르지도 내리지도 않은 구간에서는 하락 평균이 0 이라 나눌 수 없습니다. 그때는
    100 으로 봅니다 — 「내림이 하나도 없었다」는 뜻이라 그 값이 맞습니다.
    """
    period = max(int(period), 1)
    made: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return made

    gains = 0.0
    losses = 0.0
    for at in range(1, period + 1):
        change = closes[at] - closes[at - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    gains /= period
    losses /= period
    made[period] = _rsi_of(gains, losses)

    for at in range(period + 1, len(closes)):
        change = closes[at] - closes[at - 1]
        gains = (gains * (period - 1) + max(change, 0.0)) / period
        losses = (losses * (period - 1) + max(-change, 0.0)) / period
        made[at] = _rsi_of(gains, losses)
    return made


def _rsi_of(gains: float, losses: float) -> float:
    if losses == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + gains / losses)


def bollinger(
    closes: list[float], period: int, sigma: float
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """볼린저 밴드 `(상단, 중심, 하단)`. **모집단 표준편차**입니다."""
    period = max(int(period), 1)
    middle = sma(closes, period)
    upper: list[float | None] = [None] * len(closes)
    lower: list[float | None] = [None] * len(closes)
    for at, mid in enumerate(middle):
        if mid is None:
            continue
        window = closes[at - period + 1 : at + 1]
        spread = (sum((value - mid) ** 2 for value in window) / period) ** 0.5
        upper[at] = mid + spread * sigma
        lower[at] = mid - spread * sigma
    return upper, middle, lower


def true_range(bars: list[Bar]) -> list[float]:
    """봉마다의 참 범위. **첫 봉은 전 봉 종가가 없어 고−저입니다.**"""
    made: list[float] = []
    for at, bar in enumerate(bars):
        if at == 0:
            made.append(bar.high - bar.low)
            continue
        close = bars[at - 1].close
        made.append(max(bar.high - bar.low, abs(bar.high - close), abs(bar.low - close)))
    return made


def atr(bars: list[Bar], period: int) -> list[float | None]:
    """평균 참 범위. **Wilder 평활**이고 첫 값은 TR 의 단순 평균입니다."""
    period = max(int(period), 1)
    made: list[float | None] = [None] * len(bars)
    ranges = true_range(bars)
    if len(ranges) < period:
        return made
    previous = sum(ranges[:period]) / period
    made[period - 1] = previous
    for at in range(period, len(ranges)):
        previous = (previous * (period - 1) + ranges[at]) / period
        made[at] = previous
    return made


def _ema_from(values: list[float | None], period: int) -> list[float | None]:
    """앞쪽이 `None` 인 열의 EMA. 값이 처음 선 자리부터 `ema` 와 같은 식으로 셉니다."""
    start = next((at for at, value in enumerate(values) if value is not None), None)
    made: list[float | None] = [None] * len(values)
    if start is None:
        return made
    tail = ema([float(value) for value in values[start:]], period)
    made[start:] = tail
    return made


def _sma_from(values: list[float | None], period: int) -> list[float | None]:
    """앞쪽이 `None` 인 열의 SMA. 값이 처음 선 자리부터 셉니다."""
    start = next((at for at, value in enumerate(values) if value is not None), None)
    made: list[float | None] = [None] * len(values)
    if start is None:
        return made
    made[start:] = sma([float(value) for value in values[start:]], period)
    return made


def macd(
    closes: list[float], fast: int, slow: int, signal: int
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """MACD `(선 DIF, 신호 DEA, 막대 DIF − DEA)`. 식은 이 파일 첫머리에."""
    short = ema(closes, fast)
    long = ema(closes, slow)
    line: list[float | None] = [
        None if a is None or b is None else a - b for a, b in zip(short, long)
    ]
    sig = _ema_from(line, signal)
    hist: list[float | None] = [
        None if a is None or b is None else a - b for a, b in zip(line, sig)
    ]
    return line, sig, hist


def stochastic(
    bars: list[Bar], period: int, k: int, d: int
) -> tuple[list[float | None], list[float | None]]:
    """슬로 스토캐스틱 `(%K, %D)`. 식은 이 파일 첫머리에."""
    period = max(int(period), 1)
    raw: list[float | None] = [None] * len(bars)
    for at in range(period - 1, len(bars)):
        window = bars[at - period + 1 : at + 1]
        high = max(bar.high for bar in window)
        low = min(bar.low for bar in window)
        raw[at] = 50.0 if high == low else (bars[at].close - low) / (high - low) * 100.0
    k_line = _sma_from(raw, max(int(k), 1))
    d_line = _sma_from(k_line, max(int(d), 1))
    return k_line, d_line


def vwap(bars: list[Bar], sessions: list[object]) -> list[float | None]:
    """세션 VWAP. `sessions` 는 봉마다의 세션 표지이고 바뀌는 봉에서 새로 셉니다."""
    made: list[float | None] = [None] * len(bars)
    total = 0.0
    volume = 0.0
    previous: object = object()
    for at, bar in enumerate(bars):
        here = sessions[at] if at < len(sessions) else None
        if at == 0 or here != previous:
            total = 0.0
            volume = 0.0
        previous = here
        total += (bar.high + bar.low + bar.close) / 3.0 * bar.volume
        volume += bar.volume
        made[at] = total / volume if volume > 0 else None
    return made

