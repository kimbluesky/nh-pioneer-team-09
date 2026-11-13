"""Forward-only, cost-screened relative breakout of completed minute bars.

The boundary and ATR are frozen at the first excursion. Confirmation requires
another synchronized observation and further price progress, never a moving
reference catching up. This is a research hypothesis, not evidence of an edge.
"""
from __future__ import annotations

from typing import Any


def relative_breakout_entry(pair: dict[str, Any], config: Any, at: int) -> tuple[dict[str, float] | None, str]:
    bars = pair.get('trend_history', [])
    atr = pair.get('trend_atr')
    if len(bars) < config.trend_bars or not atr or atr <= 0:
        pair.pop('setup', None)
        return None, '완료된 1분봉으로 돌파 범위와 변동성을 계산합니다'
    window = bars[-config.trend_bars:]
    high, low = max(row['high'] for row in window), min(row['low'] for row in window)
    pair.update(breakout_high=high, breakout_low=low)
    spread = pair['spread']
    setup = pair.get('setup')
    if setup and at - setup['at'] > 60_000:
        pair.pop('setup', None)
        return None, '돌파 확인 시간이 지나 새 범위를 기다립니다'
    if setup is None:
        direction = (1 if spread >= high + config.breakout_buffer_atr * atr
                     else -1 if spread <= low - config.breakout_buffer_atr * atr else 0)
        if not direction:
            return None, '최근 5분 가격차 범위 돌파를 기다립니다'
        boundary = high if direction > 0 else low
        if direction * (spread - boundary) > config.breakout_max_extension_atr * atr:
            return None, '가격차가 돌파선에서 너무 멀어 추격 진입을 보류합니다'
        pair['setup'] = {'at': at, 'last_at': at, 'sign': direction, 'boundary': boundary,
                         'trigger': spread, 'atr': atr, 'range': high - low,
                         'confirmations': 1, 'phase': 'breakout',
                         'mean': pair['mean'], 'deviation': pair['deviation']}
        return None, '가격차 돌파 · 다음 시세에서 지속 여부를 확인합니다'
    direction, atr = setup['sign'], setup['atr']
    extension = direction * (spread - setup['boundary'])
    if extension < config.breakout_buffer_atr * atr or extension > config.breakout_max_extension_atr * atr:
        pair.pop('setup', None)
        return None, '돌파가 되돌아오거나 추격 범위를 넘어 후보를 취소합니다'
    if at <= setup['last_at']:
        return None, '새 동기화 시세를 기다립니다'
    setup['last_at'] = at
    setup['confirmations'] += 1
    follow = direction * (spread - setup['trigger']) / atr
    pair['breakout_follow_atr'] = follow
    if setup['confirmations'] < config.breakout_confirm_samples or follow < config.breakout_follow_atr:
        return None, '돌파 방향의 추가 움직임을 확인합니다'
    return {'direction': direction, 'atr': atr, 'spread': spread,
            'stop_spread': spread - direction * atr,
            'failure_spread': setup['boundary'] - direction * .25 * atr,
            'range': setup['range'], 'boundary': setup['boundary'],
            'z': (spread - setup['mean']) / setup['deviation'],
            'mean': setup['mean'], 'deviation': setup['deviation']}, ''
