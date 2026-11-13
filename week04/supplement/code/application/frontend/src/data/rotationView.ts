import type { RotationArm, RotationFill, RotationRun, RotationTrade } from './rotation.ts'

export type StateTone = 'neutral' | 'active' | 'long' | 'short' | 'profit' | 'loss' | 'warn'
export interface ChartPoint { x: number; y: number; valid?: boolean }
export interface TradeMarker { x: number; y: number; label: string; detail: string; tone: StateTone; entry?: boolean }

const EXIT_LABELS: Record<string, { label: string; short: string; tone: StateTone }> = {
  take_profit: { label: '익절 청산', short: '익절', tone: 'profit' },
  stop_loss: { label: '손절 청산', short: '손절', tone: 'loss' },
  structural_stop: { label: '기준 이탈 손절', short: '손절', tone: 'loss' },
  trailing_stop: { label: '수익 보호 청산', short: '수익 보호', tone: 'profit' },
  session_loss_limit: { label: '손실 한도 청산', short: '손실 한도', tone: 'loss' },
  leg_timeout: { label: '부분 체결 정리', short: '부분 정리', tone: 'warn' },
  feed_gap: { label: '시세 공백 정리', short: '공백 정리', tone: 'warn' },
  time_exit: { label: '보유 시간 종료 청산', short: '시간 종료', tone: 'neutral' },
  session_end: { label: '운용 종료 청산', short: '종료 청산', tone: 'neutral' },
  user_stop: { label: '중지 요청 청산', short: '중지 청산', tone: 'neutral' },
  restored: { label: '재접속 후 정리', short: '재접속 정리', tone: 'warn' },
}
export function exitLabel(reason: string | null | undefined) {
  return EXIT_LABELS[reason || ''] || { label: '포지션 청산', short: '청산', tone: 'neutral' as StateTone }
}
export function activeOrders(arm: RotationArm, code?: string) {
  return (arm.orders || []).filter(o => ['waiting', 'partial'].includes(o.status) && o.quantity > o.filled && (!code || o.code === code))
}
export function heldPositions(arm: RotationArm, family?: string) {
  return Object.values(arm.positions).filter(p => p.quantity !== 0 && (!family || p.family === family))
}
export function positionText(arm: RotationArm) {
  const held = heldPositions(arm)
  return held.length ? held.map(p => `${p.code} ${p.quantity > 0 ? '롱' : '숏'} ${Math.abs(p.quantity)}계약`).join(' · ') : '보유 없음'
}
export function lastClosedTrade(arm: RotationArm): RotationTrade | undefined {
  return [...(arm.trades || [])].reverse().find(t => t.filled && t.closed_at != null)
}

export function isPairArm(arm: RotationArm) {
  return !['nasdaq_only', 'sp500_only'].includes(arm.name)
}

export function holdingRows(run: RotationRun, arm: RotationArm) {
  const held = heldPositions(arm), pending = activeOrders(arm)
  const codes = new Set([...held.map(p => p.code), ...pending.map(o => o.code)])
  if (isPairArm(arm)) {
    // An unfilled leg must remain visible beside the filled leg.
    for (const family of ['a', 'b'] as const) {
      const expected = Object.keys(arm.trade?.expected || {}).filter(c =>
        arm.positions[c]?.family === family || familyInfo(run, family).codes.includes(c))
      if (expected.length) expected.forEach(c => codes.add(c))
      else if (!held.some(p => p.family === family)) codes.add(familyInfo(run, family).codes[0])
    }
  }
  return [...codes].map(code => ({ code, position: arm.positions[code]?.quantity ? arm.positions[code] : null,
    orders: pending.filter(o => o.code === code), expected: arm.trade?.expected[code] || 0 }))
}

export interface PairLeg {
  code: string; family: 'a' | 'b'; direction: number; planned: number; entryQuantity: number; exitQuantity: number
  entryPrice: number | null; exitPrice: number | null; held: number; net: number | null
}
export function pairTradeView(run: RotationRun, arm: RotationArm) {
  if (!isPairArm(arm)) return null
  const trade = arm.trade || lastClosedTrade(arm)
  if (!trade) return null
  const closed = trade.closed_at != null
  const fills = (arm.chart_fills || arm.fills).filter(f => f.time >= trade.entry_at && f.time <= (trade.closed_at ?? run.updated_at))
  const legs: PairLeg[] = Object.entries(trade.expected).map(([code, expected]) => {
    const own = fills.filter(f => f.code === code), entries = own.filter(f => f.purpose === 'entry'), exits = own.filter(f => f.purpose === 'exit')
    const quantity = (rows: RotationFill[]) => rows.reduce((s, f) => s + f.quantity, 0)
    const average = (rows: RotationFill[]) => quantity(rows) ? rows.reduce((s, f) => s + f.price * f.quantity, 0) / quantity(rows) : null
    const entryQuantity = quantity(entries), exitQuantity = quantity(exits), entryPrice = average(entries), exitPrice = average(exits)
    const position = arm.positions[code], market = run.markets.find(p => p.code === code)
    const family = (position?.family || market?.family || own[0]?.family ||
      (trade.plan.leg_a?.tickets.some(t => t.code === code) ? 'a' : 'b')) as 'a' | 'b'
    const multiplier = position?.multiplier ?? market?.multiplier ?? own[0]?.multiplier
    const realized = entryPrice != null && exitPrice != null && multiplier != null ?
      (exitPrice - entryPrice) * Math.sign(expected) * exitQuantity * multiplier : 0
    const held = closed ? 0 : Math.abs(position?.quantity || 0)
    const net = !entryQuantity || multiplier == null || (!closed && held > 0 && arm.mark_stale) ? null :
      realized - own.reduce((s, f) => s + f.fee, 0) + (held ? position?.unrealized_net || 0 : 0)
    return { code, family, direction: Math.sign(expected), planned: Math.abs(expected), entryQuantity, exitQuantity,
      entryPrice, exitPrice, held, net }
  })
  return { trade, closed, legs, legacy: legs.some(l => !familyInfo(run, l.family).codes.includes(l.code)),
    label: closed ? '최근 스프레드 거래 · 청산 완료' : '현재 스프레드 거래',
    net: closed ? trade.net ?? null : arm.net - trade.starting_net }
}

export function tradePricePoints(run: RotationRun, code: string, trade: RotationTrade): ChartPoint[] {
  const start = Math.max(0, (trade.entry_at - run.started_at) / 1000 - 60)
  const end = Math.min((run.updated_at - run.started_at) / 1000,
    ((trade.closed_at ?? run.updated_at) - run.started_at) / 1000 + 30)
  const points = run.market_curve.filter(p => p.elapsed >= start && p.elapsed <= end).map(p =>
    ({ x:p.elapsed, y:typeof p[code] === 'number' ? p[code] as number : 0, valid:p.fresh && typeof p[code] === 'number' }))
  // Shared limits preserve the timing relationship of both legs. Missing
  // observations stay gaps; an old ES fill is never mapped onto MES prices.
  return [{ x:start, y:0, valid:false }, ...points, { x:Math.max(start+1,end), y:0, valid:false }]
}

export function tradeCycleMarkers(run: RotationRun, arm: RotationArm, code: string, trade: RotationTrade) {
  const selected = (arm.chart_fills || arm.fills).filter(f => f.time >= trade.entry_at && f.time <= (trade.closed_at ?? run.updated_at))
  return tradeMarkers(run, { ...arm, chart_fills:selected }, code)
}
export function displayState(run: RotationRun, arm: RotationArm, disconnected = false) {
  const held = heldPositions(arm)
  if (disconnected) return { label: '상태 확인 중', detail: '서버 연결 확인 · 마지막 수신값 표시', tone: 'warn' as StateTone }
  if (run.session_phase === 'ready') return { label: '시작 대기', detail: '거래 내역 초기화 · 보유 없음', tone: 'neutral' as StateTone }
  if (arm.state === 'exiting') {
    const reason = exitLabel(arm.trade?.exit_reason || arm.reason)
    return { label: reason.label + ' 중', detail: held.length ? positionText(arm) : '청산 체결 확인 중', tone: reason.tone }
  }
  if (arm.state === 'entering') return { label: '진입 체결 중', detail: held.length ? '일부 체결 · 남은 주문 대기' : '주문 접수 · 아직 체결되지 않음', tone: 'active' as StateTone }
  if (held.length) return { label: '포지션 보유 중', detail: arm.mark_stale ? '평가 시세 지연 · 마지막 평가값' : '손절·익절 조건 감시 중', tone: 'active' as StateTone }
  if (!run.enabled || arm.state === 'stopped') return { label: '운용 종료', detail: '잔여 보유 없음', tone: 'neutral' as StateTone }
  if (run.warmup_valid_ms < run.execution.warmup_seconds * 1000) return { label: '시세 준비 중', detail: '유효 시세 점검 · 신규 주문 전', tone: 'warn' as StateTone }
  if (run.entry_lock || arm.reason === 'session_loss_limit' || arm.reason === 'entry_cutoff') return { label: '신규 진입 중지', detail: arm.reason_label, tone: 'warn' as StateTone }
  if (!run.feed_ok) return { label: '시세 수신 대기', detail: '최신 시세를 확인한 뒤 진입 판단', tone: 'warn' as StateTone }
  return { label: '진입 대기', detail: arm.reason_label, tone: 'neutral' as StateTone }
}
export function codeState(arm: RotationArm, code: string) {
  const pos = arm.positions[code]
  if (pos?.quantity) return { label: `${pos.quantity > 0 ? '롱' : '숏'} ${Math.abs(pos.quantity)}계약`, tone: (pos.quantity > 0 ? 'long' : 'short') as StateTone, quantity: pos.quantity }
  const order = activeOrders(arm, code).find(o => o.purpose === 'entry')
  if (order) return { label: `${order.side === 'buy' ? '롱' : '숏'} 주문 대기`, tone: 'warn' as StateTone, quantity: 0 }
  return { label: '미보유', tone: 'neutral' as StateTone, quantity: 0 }
}
export function familyInfo(run: RotationRun | undefined, family: 'a' | 'b') {
  return run?.market_families?.[family] || (family === 'a' ? { label: '나스닥 100', codes: ['NQZ26', 'MNQZ26'] } : { label: 'S&P 500', codes: ['ESZ26', 'MESZ26'] })
}
export function preferredCode(arm: RotationArm, family: 'a' | 'b', run?: RotationRun) {
  const held = heldPositions(arm, family).sort((a, b) => Math.abs(b.quantity * b.multiplier) - Math.abs(a.quantity * a.multiplier))
  if (held.length) return held[0].code
  const codes = familyInfo(run, family).codes
  const order = activeOrders(arm).find(o => codes.includes(o.code))
  return order?.code || codes[0]
}
export function currentMid(run: RotationRun, code: string) {
  const market = run.markets.find(p => p.code === code)
  const bids = market?.bids.filter(p => Number.isFinite(p.price) && p.price > 0) || []
  const asks = market?.asks.filter(p => Number.isFinite(p.price) && p.price > 0) || []
  if (!bids.length || !asks.length || !market) return null
  const bid = Math.max(...bids.map(p => p.price)), ask = Math.min(...asks.map(p => p.price))
  if (bid > ask) return null
  return { price: (bid + ask) / 2, spread: ask - bid, at: market.quote_at, fresh: run.updated_at - market.quote_at <= 5000 && run.updated_at >= market.quote_at }
}
export function marketPoints(run: RotationRun, code: string, minutes = 30): ChartPoint[] {
  const family = (['a', 'b'] as const).find(f => familyInfo(run, f).codes[0] === code)
  const live = run.market_curve.map(p => ({ x: p.elapsed, y: typeof p[code] === 'number' ? p[code] as number : 0, valid: p.fresh && typeof p[code] === 'number' }))
  const firstLive = live.length ? live[0].x : Infinity
  const history = family ? run.bars.filter(b => b.time + 60000 <= run.updated_at)
    .map(b => ({ x: (b.time + 60000 - run.started_at) / 1000, y: b[family], valid: true })).filter(p => p.x < firstLive) : []
  const points = [...history, ...live]
  const last = currentMid(run, code)
  if (last) {
    const x = (last.at - run.started_at) / 1000
    if (!points.length || x > points[points.length - 1].x) points.push({ x, y: last.price, valid: last.fresh })
  }
  const end = Math.max(0, (run.updated_at - run.started_at) / 1000)
  return points.filter(p => p.x >= end - minutes * 60 && p.x <= end)
}
export function exitReasonForFill(arm: RotationArm, fill: RotationFill) {
  const closed = [...(arm.trades || [])].reverse().find(t => fill.time >= t.entry_at && fill.time <= (t.closed_at ?? -1))
  return closed?.exit_reason || (arm.trade && fill.time >= arm.trade.entry_at ? arm.trade.exit_reason : null)
}
export function fillLabel(arm: RotationArm, fill: RotationFill) {
  if (fill.purpose === 'entry') return { label: fill.side === 'buy' ? '롱 진입' : '숏 진입', tone: (fill.side === 'buy' ? 'long' : 'short') as StateTone }
  const reason = exitLabel(exitReasonForFill(arm, fill))
  return { label: reason.label, tone: reason.tone }
}
export function tradeMarkers(run: RotationRun, arm: RotationArm, code: string): TradeMarker[] {
  const orders = new Map<string, RotationFill>()
  const trades = [...(arm.trades || []), ...(arm.trade ? [arm.trade] : [])]
  for (const f of arm.chart_fills || arm.fills || []) {
    if (f.code !== code || f.quantity <= 0) continue
    const trade = trades.find(t => f.time >= t.entry_at && f.time <= (t.closed_at ?? Infinity))
    const key = `${f.orderId || (trade ? `trade-${trade.entry_at}` : `fill-${f.time}`)}:${f.purpose}:${f.side}`
    const prior = orders.get(key)
    if (!prior) orders.set(key, { ...f })
    else {
      const quantity = prior.quantity + f.quantity
      prior.price = (prior.price * prior.quantity + f.price * f.quantity) / quantity
      prior.quantity = quantity
      prior.time = Math.min(prior.time, f.time)
    }
  }
  return [...orders.values()].sort((a, b) => a.time - b.time).map(f => {
    const action = fillLabel(arm, f)
    const entry = f.purpose === 'entry'
    return { x: (f.time - run.started_at) / 1000, y: f.price, label: entry ? (f.side === 'buy' ? '롱' : '숏') : '청산', entry,
      detail: `${action.label} · ${code} ${f.quantity}계약 · 평균 ${f.price.toFixed(2)}`, tone: entry ? action.tone : 'neutral' }
  })
}

// Keep every point, but only draw a label when it fits without overlapping
// another label. Close orders remain inspectable through the point tooltip.
export function markerLabels(points: { x: number; y: number; label: string; entry?: boolean; tone: StateTone }[],
  bounds: { left: number; right: number; top: number; bottom: number }) {
  const boxes: { x: number; y: number; width: number; height: number }[] = []
  return points.map(p => {
    const width = p.label.length * 12 + 10
    const cx = Math.max(bounds.left + width / 2, Math.min(bounds.right - width / 2, p.x))
    const direction = p.entry === false ? 1 : p.tone === 'short' ? -1 : 1
    for (const offset of [direction * 20, direction * 38, -direction * 20]) {
      const cy = p.y + offset
      const box = { x: cx - width / 2, y: cy - 8, width, height: 16 }
      if (box.y < bounds.top || box.y + box.height > bounds.bottom) continue
      if (boxes.some(b => box.x < b.x + b.width + 5 && box.x + box.width + 5 > b.x && box.y < b.y + b.height + 4 && box.y + box.height + 4 > b.y)) continue
      boxes.push(box)
      return { x: cx, y: cy, box }
    }
    return null
  })
}

export function comparisonScale(arms: RotationArm[]) {
  const points = arms.flatMap(a => a.curve)
  const values = points.filter(p => p.fresh && Number.isFinite(p.return_pct)).map(p => p.return_pct)
  const low = Math.min(0, ...values), high = Math.max(0, ...values)
  const pad = Math.max((high - low) * .12, .005)
  return { x0: 0, x1: Math.max(1, ...points.map(p => p.elapsed)), low: low - pad, high: high + pad }
}
export function isBreakout(run: RotationRun | null | undefined) {
  return run?.model === 'residual-range-breakout-v9' || run?.model === 'residual-range-breakout-v10'
}

export function breakoutEntryRules(run: RotationRun) {
  const fast = run.model === 'residual-range-breakout-v10', rules = run.strategy_rules
  return { fast, channelCloses: rules?.channel_closes ?? (fast ? 5 : 20),
    targetCloses: rules?.target_closes ?? 20, confirmSeconds: rules?.confirm_seconds ?? (fast ? 2 : 10),
    confirmObservations: rules?.confirm_observations ?? 3,
    maxChaseFraction: rules?.max_chase_fraction === null ? null : rules?.max_chase_fraction ?? (fast ? null : .25),
    cooldownSeconds: rules?.cooldown_seconds ?? (fast ? 5 : 10) }
}

export function tradeRisk(run: RotationRun, arm: RotationArm) {
  const trade = arm.trade
  if (!trade) return null
  // Includes exit fills already received during a partial close.
  const net = arm.net - trade.starting_net
  const breakout = isBreakout(run)
  const trailStart = breakout ? .6 * trade.target_net : trade.risk * run.execution.trail_start_r
  return { net, stop: -trade.risk, target: trade.target_net,
    trailActive: trade.peak_net >= trailStart, trailStart,
    trailStop: trade.peak_net >= trailStart ? breakout ? .5 * trade.peak_net : trade.peak_net - trade.risk * run.execution.trail_distance_r : null }
}

export function accountSummary(run: RotationRun, arm: RotationArm) {
  const current = arm.trade ? arm.net - arm.trade.starting_net : 0
  const closed = arm.trade ? arm.trade.starting_net : arm.net
  return { total: arm.net, current, closed, equity: run.settings.cash + arm.net,
    returnPct: 100 * arm.net / run.settings.cash, fees: arm.fees,
    contracts: heldPositions(arm).reduce((s, p) => s + Math.abs(p.quantity), 0) }
}

export function tradeStatistics(run: RotationRun, arm: RotationArm) {
  // One paired entry is one trade, even with multiple contracts/partial fills.
  const completed = arm.completed_trades || 0
  const active = Number(Boolean(arm.trade && (heldPositions(arm).length ||
    arm.fills.some(f => f.purpose === 'entry' && f.time >= arm.trade!.entry_at))))
  return { entries: completed + active, completed, active,
    drawdownPct: arm.drawdown_pct || 0,
    drawdownAmount: (arm.drawdown_pct || 0) * run.settings.cash / 100 }
}

export function entryDiagnostics(run: RotationRun, arm: RotationArm) {
  const signal = arm.signal, plan = arm.plan.chosen
  const strength = signal.strength == null ? null : Math.abs(signal.strength)
  const threshold = run.settings.min_strength ?? 1
  const breadthThreshold = run.settings.min_positive_fraction ?? .5
  const direction = Math.sign(signal.strength ?? signal.relative_move_bps ?? 0)
  const pressure = signal.book_pressure == null || !direction ? null : direction * signal.book_pressure
  const depthExcluded = arm.name === 'spread_without_regime_gate'
  return {
    strength, threshold, breadth: signal.breadth,
    pricePassed: strength == null || signal.breadth == null ? null : strength >= threshold &&
      signal.breadth >= breadthThreshold && signal.reason !== 'weak_or_expired_strength',
    pressure, depthExcluded, depthPassed: depthExcluded ? true : pressure == null ? null : pressure >= .05,
    cost: plan?.estimated_round_trip_cost ?? null,
    costLimit: plan ? plan.risk * (run.settings.max_cost_fraction_of_risk ?? .4) : null,
    costPassed: plan?.cost_acceptable ?? null,
  }
}

export function positionMargin(run: RotationRun, code: string, quantity: number) {
  if (!quantity) return 0
  const margin = run.markets.find(p => p.code === code)?.margin
  return margin == null || !Number.isFinite(margin) || margin <= 0 ? null : margin * Math.abs(quantity)
}

export function capitalSummary(run: RotationRun, arm: RotationArm) {
  const held = heldPositions(arm), plan = arm.trade?.plan || arm.plan.chosen
  const margins = held.map(p => positionMargin(run, p.code, p.quantity))
  const used = margins.some(v => v == null) ? null : margins.reduce<number>((s, v) => s + (v || 0), 0)
  const gross = held.reduce((s, p) => s + Math.abs(p.quantity) * p.multiplier * (currentMid(run, p.code)?.price ?? p.mark_price ?? p.average), 0)
  const relevant = run.markets.filter(p => arm.name === 'nasdaq_only' ? p.family === 'a' : arm.name === 'sp500_only' ? p.family === 'b' : arm.name === 'spread_micro_only' ? p.code.startsWith('M') : true)
  const available = relevant.map(p => p.available_amount)
  const beforeTrade = arm.trade?.starting_net ?? arm.realized - arm.fees
  const budget = arm.sizing_mode === 'max_affordable' && available.length && available.every(v => v != null && Number.isFinite(v) && v > 0)
    ? Math.min(run.settings.cash + Math.min(0, beforeTrade), ...available as number[]) : null
  const routes = plan ? [plan.leg_a, plan.leg_b, plan.leg].filter(Boolean) as NonNullable<typeof plan.leg>[] : []
  const tickets = routes.flatMap(p => p.tickets)
  const reserve = plan?.estimated_round_trip_cost ?? null
  return { used, usedPct: used == null ? null : 100 * used / run.settings.cash, gross,
    exposureMultiple: gross / run.settings.cash, budget, reserve,
    plannedMargin: plan?.margin ?? null, plannedTickets: tickets,
    committed: plan && budget != null ? plan.margin + plan.estimated_round_trip_cost : null,
    unallocated: plan && budget != null ? budget - plan.margin - plan.estimated_round_trip_cost : null }
}
