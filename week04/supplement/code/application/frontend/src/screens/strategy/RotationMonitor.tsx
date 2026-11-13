import { useEffect, useRef, useState } from 'react'
import { rotationRequest, type RotationArm, type RotationResponse, type RotationRun } from '../../data/rotation'
import { accountSummary, activeOrders, capitalSummary, codeState, comparisonScale, currentMid, displayState, entryDiagnostics, exitLabel, familyInfo, fillLabel, heldPositions, holdingRows, isPairArm, lastClosedTrade, markerLabels, marketPoints, pairTradeView, preferredCode, tradeCycleMarkers, tradeMarkers, tradePricePoints, tradeRisk, tradeStatistics, type ChartPoint, type StateTone, type TradeMarker } from '../../data/rotationView'
import { saveRotationFigure, saveRotationMonitorFigure } from './rotationFigure'
import { breakoutEntryRules, isBreakout } from '../../data/rotationView'
import './RotationDesk.css'

const COLORS = ['#20b58e', '#69a8f1', '#dcb774', '#b89ce4', '#a4adb8']
const MARKER_COLORS: Record<StateTone, string> = { neutral: '#aeb9c8', active: '#39cba2', long: '#f48d8d', short: '#81b3ff', profit: '#46d1a8', loss: '#f19b6b', warn: '#efc66f' }
const number = (v: number | null | undefined, d = 2) => v == null || !Number.isFinite(v) ? '—' : v.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d })
const money = (v: number | null | undefined) => v == null ? '—' : `${v < 0 ? '−' : ''}$${number(Math.abs(v))}`
const percent = (v: number) => `${v > 0 ? '+' : ''}${number(v, 3)}%`
const pnlClass = (v: number) => v > 0 ? 'rd-up' : v < 0 ? 'rd-down' : ''
const duration = (v: number) => { const s = Math.max(0, Math.floor(v)); return `${String(Math.floor(s / 3600)).padStart(2, '0')}:${String(Math.floor(s / 60) % 60).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}` }
const elapsedLabel = (v: number) => `${v < 0 ? '−' : '+'}${duration(Math.abs(v))}`

function Badge({ tone = 'neutral', children }: { tone?: StateTone; children: React.ReactNode }) { return <span className={`rd-state-badge rd-tone-${tone}`}>{children}</span> }

function LineChart({ points, color, price = false, height = 185, markers = [] }: {
  points: ChartPoint[]; color: string; price?: boolean; height?: number; markers?: TradeMarker[]
}) {
  const ref = useRef<SVGSVGElement>(null)
  const [width, setWidth] = useState(600)
  const [hover, setHover] = useState<ChartPoint | null>(null)
  useEffect(() => {
    const element = ref.current
    if (!element) return
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(260, Math.floor(entry.contentRect.width))))
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  const valid = points.filter(p => p.valid !== false && Number.isFinite(p.y))
  const left = price ? 77 : 61, right = 17, top = 25, bottom = 26
  const x0 = points.length ? Math.min(...points.map(p => p.x)) : 0
  const x1 = Math.max(x0 + 1, ...points.map(p => p.x))
  const visibleMarkers = markers.filter(p => p.x >= x0 && p.x <= x1)
  const values = [...valid.map(p => p.y), ...visibleMarkers.map(p => p.y), ...(price ? [] : [0])]
  const low = values.length ? Math.min(...values) : 0, high = values.length ? Math.max(...values) : 0
  const pad = Math.max((high - low) * .17, price ? .5 : .005), lo = low - pad, hi = high + pad
  const x = (v: number) => left + (v - x0) / (x1 - x0) * (width - left - right)
  const y = (v: number) => top + (hi - v) / (hi - lo) * (height - top - bottom)
  const labels = markerLabels(visibleMarkers.map(m => ({ ...m, x: x(m.x), y: y(m.y) })),
    { left, right: width - right, top: 6, bottom: height - bottom - 2 })
  let connected = false
  const path = points.map(p => {
    if (p.valid === false || !Number.isFinite(p.y)) { connected = false; return '' }
    const part = `${connected ? 'L' : 'M'}${x(p.x).toFixed(2)},${y(p.y).toFixed(2)}`; connected = true; return part
  }).join(' ')
  const last = valid.at(-1)
  return <svg ref={ref} className="rd-live-chart" style={{ height }} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={price ? '종목 가격 선 그래프' : '전략 수익률 선 그래프'}
    onMouseLeave={() => setHover(null)} onMouseMove={e => {
      if (!valid.length || !ref.current) return
      const mx = e.clientX - ref.current.getBoundingClientRect().left
      const target = x0 + (mx - left) / (width - left - right) * (x1 - x0)
      setHover(valid.reduce((a, b) => Math.abs(a.x - target) < Math.abs(b.x - target) ? a : b))
    }}>
    {[0, 1, 2, 3].map(i => { const value = lo + (hi - lo) * i / 3; return <g key={i}>
      <line className="rd-grid" x1={left} x2={width - right} y1={y(value)} y2={y(value)} />
      <text x={left - 10} y={y(value) + 4} textAnchor="end">{number(value, price ? 2 : 3)}{price ? '' : '%'}</text>
    </g> })}
    {!price && <line x1={left} x2={width - right} y1={y(0)} y2={y(0)} className="rd-zero" />}
    {x0 < 0 && x1 > 0 && <g><line x1={x(0)} x2={x(0)} y1={top} y2={height - bottom} className="rd-zero" /><text x={x(0) + 5} y={top - 9}>실행 시작</text></g>}
    <path data-price-series={price || undefined} data-return-series={!price || undefined} data-point-count={valid.length} d={path} fill="none" stroke={color} strokeWidth={price ? 2 : 1.8} />
    {last && <circle cx={x(last.x)} cy={y(last.y)} r="3" fill={color} />}
    {visibleMarkers.map((m, i) => { const label = labels[i], px = x(m.x), py = y(m.y), long = m.tone === 'long'; return <g key={i} data-fill-label={m.label} data-fill-entry={m.entry}><title>{m.detail}</title>
      {label && <line x1={px} x2={label.x} y1={py} y2={label.y} stroke={MARKER_COLORS[m.tone]} opacity=".45" />}
      {m.entry ? <path d={long ? `M${px},${py - 5}l-5,9h10Z` : `M${px},${py + 5}l-5,-9h10Z`} fill={MARKER_COLORS[m.tone]} stroke="#11171e" strokeWidth="1" /> : <circle cx={px} cy={py} r="4" fill="#11171e" stroke={MARKER_COLORS[m.tone]} strokeWidth="1.5" />}
      <circle cx={px} cy={py} r="9" fill="transparent" />
      {label && <g data-marker-text="true"><rect x={label.box.x} y={label.box.y} width={label.box.width} height={label.box.height} rx="2" fill="#151c23" /><text x={label.x} y={label.y + 4} textAnchor="middle" style={{ fill: MARKER_COLORS[m.tone] }}>{m.label}</text></g>}
    </g> })}
    {[0, 1, 2].map(i => { const value = x0 + (x1 - x0) * i / 2; return <text key={i} x={x(value)} y={height - 7} textAnchor={i === 0 ? 'start' : i === 2 ? 'end' : 'middle'}>{elapsedLabel(value)}</text> })}
    {!valid.length && <text className="rd-graph-empty" x={width / 2} y={height / 2} textAnchor="middle">가격 자료 수신 대기</text>}
    {hover && <g pointerEvents="none"><line x1={x(hover.x)} x2={x(hover.x)} y1={top} y2={height - bottom} stroke="#7e8da2" strokeDasharray="3 3" /><circle cx={x(hover.x)} cy={y(hover.y)} r="4" fill={color} /><rect x={left} y="2" width="210" height="20" fill="#151c25" /><text x={left + 6} y="16" style={{ fill: '#e7edf6' }}>{elapsedLabel(hover.x)}　{number(hover.y, price ? 2 : 3)}{price ? '' : '%'}</text></g>}
  </svg>
}

function CurrentState({ run, arm, disconnected }: { run: RotationRun; arm: RotationArm; disconnected: boolean }) {
  const state = displayState(run, arm, disconnected), risk = tradeRisk(run, arm), last = lastClosedTrade(arm)
  const positions = heldPositions(arm), pending = activeOrders(arm)
  const account = accountSummary(run, arm), capital = capitalSummary(run, arm), statistics = tradeStatistics(run, arm)
  return <section className="rd-current rd-account" data-trade-state={arm.state} data-account-arm={arm.id}>
    <header className="rd-account-head"><span className="rd-current-label">{arm.name === 'spread' ? '주 전략' : '비교군'} · <b>{arm.label}</b></span>
      <h2 className={`rd-tone-text-${state.tone}`}>{state.label}</h2><span className="rd-account-reason">{state.detail}</span>
      <span className="rd-account-held">보유 <b>{account.contracts}계약</b>{pending.length > 0 && <> · 미체결 <b>{pending.reduce((s, p) => s + p.quantity - p.filled, 0)}계약</b></>}</span>
    </header>
    <div className="rd-account-metrics">
      <div><span>총 순손익</span><b data-account-total={account.total} className={pnlClass(account.total)}>{money(account.total)}</b><small>완료 거래 + 현재 거래</small></div>
      <div><span>수익률</span><b data-account-return={account.returnPct} className={pnlClass(account.total)}>{percent(account.returnPct)}</b><small>원금 {money(run.settings.cash)} 기준</small></div>
      <div><span>현재 거래 손익</span><b data-account-current={account.current} className={pnlClass(account.current)}>{money(account.current)}</b><small>{arm.trade ? arm.mark_stale ? '평가 시세 지연' : '진입·청산 예상 비용 포함' : '보유 없음'}</small></div>
      <div><span>청산 완료 순손익</span><b data-account-closed={account.closed} className={pnlClass(account.closed)}>{money(account.closed)}</b><small>완료 거래 {arm.completed_trades}회</small></div>
      <div><span>거래 횟수</span><b data-trade-entries={statistics.entries}>{statistics.entries}회</b><small>완료 {statistics.completed} · 진행 {statistics.active}</small></div>
      <div title="비용 포함 평가손익의 이전 최고점에서 가장 크게 감소한 금액 ÷ 최초 원금. 고점 평가자산을 분모로 쓰는 MDD와 구분합니다."><span>MDD · 원금 기준</span><b data-account-mdd={statistics.drawdownPct} className={statistics.drawdownPct > 0 ? 'rd-down' : ''}>{number(statistics.drawdownPct, 3)}%</b><small>최대 감소 {money(statistics.drawdownAmount)}</small></div>
      <div><span>평가자산</span><b>{money(account.equity)}</b><small>원금 + 총 순손익</small></div>
      <div><span>누적 수수료</span><b>{money(account.fees)}</b><small>체결 {arm.fill_count}건 · 손익에 반영됨</small></div>
    </div>
    {run.model === 'relative-depth-scalp-nq-es-v1' && arm.state === 'watching' && <EntryChecks run={run} arm={arm} />}
    {isBreakout(run) && arm.state === 'watching' && <BreakoutChecks run={run} arm={arm} />}
    <div className="rd-account-risk"><span>손절 <b className={risk ? 'rd-down' : ''}>{money(risk?.stop)}</b></span><span>익절 <b className={risk ? 'rd-up' : ''}>{money(risk?.target)}</b></span>
      <span>{run.model === 'partial-hedge-blend-v15' ? '손절·익절 고정 · 보유시간 청산 없음' : risk ? risk.trailActive ? `수익 보호 ${money(risk.trailStop)} 이하 청산` : `수익 보호 ${money(risk.trailStart)}부터` : '손절·익절은 진입 시 확정'}</span>
      {last ? <span className="rd-last-completed" data-last-exit={last.exit_reason}><Badge tone={exitLabel(last.exit_reason).tone}>{exitLabel(last.exit_reason).label} 완료</Badge><b className={pnlClass(last.net ?? 0)}>{money(last.net)}</b></span> : <span className="rd-last-completed">최근 청산 없음</span>}
    </div>
    <div className="rd-capital-grid">
      <div><span>사용 증거금</span><b data-margin-used={capital.used}>{money(capital.used)} <small>{number(capital.usedPct, 1)}%</small></b></div>
      <div><span>명목보유금액</span><b>{money(capital.gross)} <small>{number(capital.exposureMultiple, 1)}배</small></b></div>
      <div><span>수량 계산 한도</span><b>{arm.sizing_mode === 'max_affordable' ? money(capital.budget) : '거래당 계획 위험 $500'}</b></div>
      <div><span>{positions.length ? '진입 시 왕복 비용 예비금' : '검토 후보 왕복 비용'}</span><b>{money(capital.reserve)}</b></div>
    </div>
    {capital.plannedTickets.length > 0 && <div className="rd-capital-plan">{arm.trade ? '이번 진입 주문' : '검토 후보 · 아직 주문하지 않음'} <b>{capital.plannedTickets.map(p => `${p.code} ${p.quantity}계약`).join(' + ')}</b><span>증거금 {money(capital.plannedMargin)} + 비용 {money(capital.reserve)}{capital.budget != null && <> / 한도 {money(capital.budget)} · 계산 잔여 {money(capital.unallocated)}</>}</span></div>}
  </section>
}

function EntryChecks({ run, arm }: { run: RotationRun; arm: RotationArm }) {
  const d = entryDiagnostics(run, arm)
  const outcome = (pass: boolean | null) => pass == null ? '확인 중' : pass ? '충족' : '미충족'
  return <div className="rd-entry-checks" aria-label="최근 진입 판단">
    <span>최근 진입 판단</span>
    <div data-entry-check="price" data-passed={String(d.pricePassed)}><b>가격 신호 {outcome(d.pricePassed)}</b><small>강도 {number(d.strength)} / {number(d.threshold)} · 방향 일치 {number((d.breadth ?? 0) * 100, 0)}%</small></div>
    <div data-entry-check="depth" data-passed={String(d.depthPassed)}><b>호가 {d.depthExcluded ? '확인 제외' : outcome(d.depthPassed)}</b><small>{d.depthExcluded ? '비교군 설정' : `신호 방향 ${number(d.pressure, 3)} / 최소 0.050`}</small></div>
    <div data-entry-check="cost" data-passed={String(d.costPassed)}><b>비용 {outcome(d.costPassed)}</b><small>{money(d.cost)} / 허용 {money(d.costLimit)}</small></div>
  </div>
}

function BreakoutChecks({ run, arm }: { run: RotationRun; arm: RotationArm }) {
  const s = arm.signal, p = arm.plan.chosen, excluded = arm.name === 'spread_without_regime_gate'
  const rules = breakoutEntryRules(run)
  const price = s.channel_level == null ? null : Math.abs(s.strength ?? 0) > 1 && (rules.maxChaseFraction == null || (s.chase_fraction ?? 1) <= rules.maxChaseFraction)
  const state = (v: boolean | null | undefined) => v == null ? '확인 중' : v ? '충족' : '대기'
  return <div className="rd-entry-checks" aria-label="돌파 진입 판단">
    <span>진입 판단</span>
    <div data-entry-check="range"><b>범위 돌파 {state(price)}</b><small>최근 {rules.channelCloses}분 범위{rules.maxChaseFraction != null ? ` · 추격 ${rules.maxChaseFraction * 100}% 이내` : ' · 추가 추격 제한 없음'}</small></div>
    <div data-entry-check="confirmation"><b>돌파 유지 {excluded ? '확인 제외' : state(s.breakout_confirmed)}</b><small>새 호가 {rules.confirmObservations}회 · 최소 {rules.confirmSeconds}초</small></div>
    <div data-entry-check="net-target"><b>비용 후 손익비 {number(p?.net_reward_risk)} / 1.00</b><small>목표 {money(p?.target_net)} · 왕복 비용 {money(p?.estimated_round_trip_cost)}</small></div>
  </div>
}

function HoldingsOverview({ run, arms, selected, onSelect, disconnected }: { run: RotationRun; arms: RotationArm[]; selected: string; onSelect: (name: string) => void; disconnected: boolean }) {
  return <section className="rd-live-holdings"><header><h2>보유·매매 현황</h2><span>각 전략 독립 자금 $100,000 · 전략 이름 선택 시 상단 상세</span></header>
    <div className="rd-table-wrap"><table><thead><tr><th>전략 / 상태</th><th>종목 · 방향</th><th>보유 / 미체결</th><th>평균 진입가</th><th>평가손익</th><th>증거금 사용</th><th>총 순손익</th><th>수익률</th></tr></thead><tbody>
      {arms.flatMap(a => {
        const positions = heldPositions(a), account = accountSummary(run, a), capital = capitalSummary(run, a), state = displayState(run, a, disconnected)
        const entries = holdingRows(run, a), rows = entries.length ? entries : [null]
        return rows.map((row, i) => { const p = row?.position, code = row?.code, order = row?.orders.find(o => o.purpose === 'entry'); return <tr key={`${a.id}:${code || 'flat'}`} className={a.name === selected ? 'selected' : ''} data-holding-arm={a.id} data-holding-code={code || ''}>
          {i === 0 && <th rowSpan={rows.length}><button onClick={() => onSelect(a.name)}>{a.name === 'spread' && <em>주 전략</em>}{a.label}</button><small>{state.label}{!positions.length && <> · {a.reason_label}</>}</small></th>}
          <td className="rd-holding-instrument">{code && <b>{code} </b>}{p ? <Badge tone={p.quantity > 0 ? 'long' : 'short'}>{p.quantity > 0 ? '롱 · 매수' : '숏 · 매도'}</Badge> : order ? <Badge tone="warn">{order.side === 'buy' ? '롱' : '숏'} 주문 대기</Badge> : <span className="rd-no-position">미보유</span>}</td>
          <td><b>{Math.abs(p?.quantity || 0)}계약</b><small>{row?.orders.length ? `미체결 ${row.orders.reduce((s, o) => s + o.quantity - o.filled, 0)}계약` : '미체결 없음'}</small></td>
          <td>{number(p?.average)}</td><td className={pnlClass(p?.unrealized_net || 0)}>{money(p?.unrealized_net)}</td>
          {i === 0 && <><td rowSpan={rows.length}>{money(capital.used)}<small>{number(capital.usedPct, 1)}%</small></td><td rowSpan={rows.length} className={pnlClass(account.total)}><b>{money(account.total)}</b></td><td rowSpan={rows.length} className={pnlClass(account.total)}><b>{percent(account.returnPct)}</b></td></>}
        </tr> })
      })}
    </tbody></table></div>
  </section>
}

function MarketPanel({ run, arm, family, color }: { run: RotationRun; arm: RotationArm; family: 'a' | 'b'; color: string }) {
  const [picked, setPicked] = useState<string | null>(null)
  const code = picked || preferredCode(arm, family, run)
  const mid = currentMid(run, code), state = codeState(arm, code), pos = arm.positions[code]
  const points = marketPoints(run, code)
  const lastFill = arm.fills.filter(f => f.code === code).at(-1)
  const { codes, label } = familyInfo(run, family)
  return <section className="rd-market rd-market-permanent" data-market-family={family} data-market-code={code}>
    <header><h2>{label}</h2><Badge tone={state.tone}>{state.label}</Badge></header>
    <div className="rd-market-price"><div className="rd-contract-tabs">{codes.map(c => <button key={c} aria-pressed={c === code} onClick={() => setPicked(c)}>{c}</button>)}</div><b>{number(mid?.price)}</b><span>{mid?.fresh ? '중간호가' : mid ? '시세 지연' : '시세 대기'}</span></div>
    <LineChart points={points} color={color} price height={195} markers={tradeMarkers(run, arm, code).filter(m => !isPairArm(arm) || !run.execution_policy?.effective_at || run.started_at + m.x * 1000 >= run.execution_policy.effective_at)} />
    <div className="rd-market-position"><span>보유 <b>{pos?.quantity ? `${pos.quantity > 0 ? '롱' : '숏'} ${Math.abs(pos.quantity)}계약` : '없음'}</b></span><span>평균 진입가 <b>{number(pos?.quantity ? pos.average : null)}</b></span><span>평가손익 <b className={pnlClass(pos?.quantity ? pos.unrealized_net || 0 : 0)}>{money(pos?.quantity ? pos.unrealized_net : null)}</b></span></div>
    <div className="rd-market-caption"><span>{lastFill ? `${fillLabel(arm, lastFill).label} · ${lastFill.quantity}계약 · ${number(lastFill.price)}` : '최근 체결 없음'}</span><span>▲ 롱 · ▼ 숏 · ○ 청산</span></div>
  </section>
}

function InstrumentCharts({ run, arm }: { run: RotationRun; arm: RotationArm }) {
  const [view, setView] = useState<'trade' | 'live'>('trade')
  const pair = pairTradeView(run, arm)
  const showTrade = Boolean(pair && view === 'trade')
  return <>
    <div className="rd-section-title rd-instrument-title"><h2>{showTrade ? pair!.label : `종목 시세 · ${arm.label}`}
      {showTrade && <small>{pair!.legacy ? '종목 제한 적용 전 기록 · ' : ''}두 레그 합산 <b className={pnlClass(pair!.net || 0)}>{money(pair!.net)}</b></small>}</h2>
      {pair && <nav><button aria-pressed={showTrade} onClick={() => setView('trade')}>두 레그 체결</button><button aria-pressed={!showTrade} onClick={() => setView('live')}>현재 시세</button></nav>}
    </div>
    {showTrade && pair ? <div className="rd-price-grid" data-paired-trade={pair.trade.entry_at} data-pair-closed={pair.closed}>
      {(['a', 'b'] as const).map((family, i) => <PairMarketPanel key={`${pair.trade.entry_at}:${family}`} run={run} arm={arm} pair={pair} family={family} color={COLORS[i+1]} />)}
    </div> : <div className="rd-price-grid"><MarketPanel run={run} arm={arm} family="a" color={COLORS[1]} /><MarketPanel run={run} arm={arm} family="b" color={COLORS[2]} /></div>}
  </>
}

function PairMarketPanel({ run, arm, pair, family, color }: { run: RotationRun; arm: RotationArm; pair: NonNullable<ReturnType<typeof pairTradeView>>; family: 'a' | 'b'; color: string }) {
  const legs = pair.legs.filter(l => l.family === family)
  const [picked, setPicked] = useState<string | null>(null)
  const leg = legs.find(l => l.code === picked) || legs[0]
  if (!leg) return <section className="rd-market rd-market-permanent"><h2>{familyInfo(run, family).label}</h2><p>레그 기록 확인 중</p></section>
  const mid = currentMid(run, leg.code)
  const price = pair.closed ? leg.exitPrice : mid?.price
  const points = tradePricePoints(run, leg.code, pair.trade)
  return <section className="rd-market rd-market-permanent rd-pair-market" data-market-family={family} data-market-code={leg.code} data-pair-direction={leg.direction > 0 ? 'long' : 'short'}>
    <header><h2>{familyInfo(run, family).label} · {leg.code}</h2><Badge tone={leg.entryQuantity ? leg.direction > 0 ? 'long' : 'short' : 'warn'}>{leg.direction > 0 ? '롱' : '숏'} {leg.entryQuantity || leg.planned}계약{leg.entryQuantity ? pair.closed ? ' · 청산 완료' : '' : ' · 미체결'}</Badge></header>
    <div className="rd-market-price"><div className="rd-contract-tabs">{legs.length > 1 && legs.map(l => <button key={l.code} aria-pressed={l.code === leg.code} onClick={() => setPicked(l.code)}>{l.code}</button>)}</div><b>{number(price)}</b><span>{pair.closed ? '평균 청산가' : mid?.fresh ? '중간호가' : '시세 대기'}</span></div>
    <div className="rd-pair-leg-facts"><span>진입 <b data-leg-entry={leg.entryPrice}>{number(leg.entryPrice)}</b></span><span>{pair.closed ? '체결 수량' : '보유 / 주문'} <b>{pair.closed ? `${leg.entryQuantity}계약` : `${leg.held} / ${leg.planned}계약`}</b></span><span>{pair.closed ? '레그 순손익' : '레그 평가손익'} <b data-leg-net={leg.net} className={pnlClass(leg.net || 0)}>{money(leg.net)}</b></span></div>
    <LineChart points={points} color={color} price height={195} markers={tradeCycleMarkers(run, arm, leg.code, pair.trade)} />
    <div className="rd-market-caption"><span>{pair.closed ? exitLabel(pair.trade.exit_reason).label : leg.held ? '보유 중' : '후속 호가 체결 대기'}</span><span>{pair.closed ? '완료 거래 기록 · 현재 보유 아님' : '현재 거래'} · ▲ 롱 · ▼ 숏 · ○ 청산</span></div>
  </section>
}

function ReturnComparison({ run, arms, selected, onSelect }: { run: RotationRun; arms: RotationArm[]; selected: string; onSelect: (name: string) => void }) {
  const ref = useRef<SVGSVGElement>(null)
  const [width, setWidth] = useState(1000), [hover, setHover] = useState<number | null>(null)
  useEffect(() => {
    if (!ref.current) return
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(300, Math.floor(entry.contentRect.width))))
    observer.observe(ref.current)
    return () => observer.disconnect()
  }, [])
  const height = 220, left = 72, right = 22, top = 20, bottom = 30
  const scale = comparisonScale(arms)
  const x = (v: number) => left + v / scale.x1 * (width - left - right)
  const y = (v: number) => top + (scale.high - v) / (scale.high - scale.low) * (height - top - bottom)
  const policyAt = run.execution_policy?.effective_at == null ? null : (run.execution_policy.effective_at - run.started_at) / 1000
  const entryPolicyAt = run.entry_policy == null ? null : (run.entry_policy.effective_at - run.started_at) / 1000
  const nearest = arms.map(a => hover == null || !a.curve.length ? null : a.curve.reduce((p, q) => Math.abs(p.elapsed - hover) < Math.abs(q.elapsed - hover) ? p : q))
  return <section className="rd-return-comparison" data-comparison-series={arms.length}>
    <div className="rd-return-legend">{arms.map((a, i) => { const stats = tradeStatistics(run, a); return <button key={a.id} data-strategy-name={a.name} onClick={() => onSelect(a.name)} aria-pressed={a.name === selected}>
      <span className="rd-return-name"><i style={{ background: COLORS[i] }} />{a.label}</span><b className={pnlClass(a.net)}>{percent(a.return_pct)}</b><span>{money(a.net)}</span><small>거래 {stats.entries}회 · MDD(원금) {number(stats.drawdownPct, 3)}%</small>
    </button> })}</div>
    <svg ref={ref} className="rd-live-chart rd-combined-chart" style={{ height }} viewBox={`0 0 ${width} ${height}`} role="img" aria-label="전략 수익률 통합 비교 그래프"
      onMouseLeave={() => setHover(null)} onMouseMove={e => { if (ref.current) setHover(Math.max(0, Math.min(scale.x1, (e.clientX - ref.current.getBoundingClientRect().left - left) / (width - left - right) * scale.x1))) }}>
      {[0, 1, 2, 3, 4].map(i => { const value = scale.low + (scale.high - scale.low) * i / 4; return <g key={i}><line className="rd-grid" x1={left} x2={width - right} y1={y(value)} y2={y(value)} /><text x={left - 10} y={y(value) + 4} textAnchor="end">{number(value, 3)}%</text></g> })}
      <line className="rd-zero" x1={left} x2={width - right} y1={y(0)} y2={y(0)} />
      {policyAt != null && policyAt <= scale.x1 && <g><line x1={x(policyAt)} x2={x(policyAt)} y1={top} y2={height - bottom} stroke="#526174" strokeDasharray="2 5" /><text x={Math.min(width - 150, x(policyAt) + 6)} y="13">MNQ·MES 전용 적용</text></g>}
      {entryPolicyAt != null && entryPolicyAt <= scale.x1 && <g data-entry-policy-marker="true"><line x1={x(entryPolicyAt)} x2={x(entryPolicyAt)} y1={top} y2={height - bottom} stroke="#a09a7c" strokeDasharray="3 5" /><text x={Math.min(width - 160, x(entryPolicyAt) + 6)} y="13">진입 조건 변경 · 손익 유지</text></g>}
      {arms.map((a, i) => { let connected = false; const path = a.curve.map(p => {
        if (!p.fresh || !Number.isFinite(p.return_pct)) { connected = false; return '' }
        const command = `${connected ? 'L' : 'M'}${x(p.elapsed).toFixed(2)},${y(p.return_pct).toFixed(2)}`; connected = true; return command
      }).join(' '); const last = a.curve.at(-1); return <g key={a.id}><path data-return-series={a.name} data-point-count={a.curve.filter(p => p.fresh).length} d={path} stroke={COLORS[i]} strokeWidth={a.name === selected ? 2.3 : 1.7} fill="none" />{last?.fresh && <circle cx={x(last.elapsed)} cy={y(last.return_pct)} r="3" fill={COLORS[i]} />}</g> })}
      {[0, 1, 2, 3, 4].map(i => <text key={i} x={x(scale.x1 * i / 4)} y={height - 8} textAnchor={i === 0 ? 'start' : i === 4 ? 'end' : 'middle'}>{elapsedLabel(scale.x1 * i / 4)}</text>)}
      {hover != null && <g pointerEvents="none"><line x1={x(hover)} x2={x(hover)} y1={top} y2={height - bottom} stroke="#7e8da2" strokeDasharray="3 3" />{nearest.map((p, i) => p?.fresh && <circle key={i} cx={x(p.elapsed)} cy={y(p.return_pct)} r="4" fill={COLORS[i]} />)}<g transform={`translate(${Math.max(left, Math.min(width - 310, x(hover) + 12))},${top})`}><rect width="280" height={30 + arms.length * 21} rx="3" fill="#0e141c" stroke="#384454" /><text x="10" y="18">{elapsedLabel(hover)}</text>{arms.map((a, i) => <text key={a.id} x="10" y={39 + i * 21} style={{ fill: COLORS[i] }}>{a.label}　{nearest[i]?.fresh ? percent(nearest[i]!.return_pct) : '—'}</text>)}</g></g>}
    </svg>
  </section>
}

function Conditions({ arm, run }: { arm: RotationArm; run: RotationRun }) {
  const single = arm.name === 'nasdaq_only' || arm.name === 'sp500_only', plan = arm.trade?.plan || arm.plan.chosen
  const scalp = run.model === 'relative-depth-scalp-nq-es-v1'
  if (isBreakout(run)) return <div className="rd-conditions"><strong>{arm.reason_label}</strong><dl>
    {[
      ['범위 하단 / 현재 / 상단', `${number((arm.signal.channel_low ?? NaN) * 10000)} / ${number((arm.signal.channel_level ?? NaN) * 10000)} / ${number((arm.signal.channel_high ?? NaN) * 10000)} bp`],
      ['돌파 유지', arm.signal.breakout_confirmed ? '확인됨' : '대기'],
      ...(!single ? [['헤지 β / 상관', `${number(arm.signal.beta)} / ${number(arm.signal.correlation)}`]] : []),
      ['비용 후 목표 / 손절', `${money(plan?.target_net)} / ${money(plan?.risk)}`],
      ['비용 후 손익비 / 최소', `${number(plan?.net_reward_risk)} / 1.00`],
      ['왕복 예상 비용', money(plan?.estimated_round_trip_cost)],
    ].map(([label,value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
  </dl></div>
  const rows = scalp ? [['3분 상대 움직임', `${number(arm.signal.relative_move_bps, 2)} bp`], ['호가 잔량 방향', number(arm.signal.book_pressure, 3)], ['가격 방향 일치', `${number((arm.signal.breadth ?? 0) * 100, 0)}%`], ...(!single ? [['헤지 β / 상관', `${number(arm.signal.beta)} / ${number(arm.signal.correlation)}`]] : [])] : single ? [['1분 변동성', `${number((arm.signal.sigma ?? 0) * 100, 3)}%`], ['추세 방향 일치 / 기준', `${number((arm.signal.breadth ?? 0) * 100, 0)}% / 60%`]] : [
    ['상승 구간 초과수익', `${number(arm.signal.up_excess_bps, 3)} bp`], ['하락 구간 초과수익', `${number(arm.signal.down_excess_bps, 3)} bp`], ['헤지 β / 상관', `${number(arm.signal.beta)} / ${number(arm.signal.correlation)}`],
  ]
  rows.push(['추세 강도 / 기준', `${number(arm.signal.strength)} / ±${number(run.settings.min_strength ?? 1.5)}`], ['왕복 비용 / 허용', plan ? `${money(plan.estimated_round_trip_cost)} / ${money(plan.risk * .4)}` : '—'])
  return <div className="rd-conditions"><strong>{arm.reason_label}</strong><dl>{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl></div>
}

function ScalpRules() {
  return <div className="rd-rules-content">
    <p><b>진입</b> 나스닥·S&P의 앞선 30분으로 헤지 비율을 추정합니다. 최근 3분 상대 움직임이 강하고, 최근 3개 분봉 중 2개 이상과 마지막 분봉의 방향이 일치해야 합니다. 최소 10초에 걸친 호가 잔량도 같은 방향인지 확인합니다.</p>
    <p><b>청산</b> 진입 시 손절 −1R, 익절 +1.5R을 고정합니다. 순익 +0.8R 이후 최고 순익에서 0.4R 되밀리면 정리합니다. 최대 5분 보유는 보조 종료 조건입니다. R은 최근 변동성·되밀림 폭·체결 비용으로 계산하며 화면에 달러로 표시합니다.</p>
    <p><b>준비</b> 과거 완료 1분봉 34개를 불러와 기준을 계산합니다. 추가 5분 대기는 없습니다. 최소 10초에 걸친 새 호가 3회와 최신 가격·주문 가능 수량을 확인하면 진입 조건을 판단합니다. 자료가 부족하거나 오래됐으면 해당 이유를 표시합니다.</p>
    <p><b>운용</b> 시작 후 3시간 운용합니다. 종료 6분 전 신규 진입을 멈추고 3분 전 정리를 시작합니다. 누적 순손실 $2,000에서 신규 진입을 잠급니다. 단절 중에는 체결을 만들지 않고 연결을 복구해 잔여 보유를 정리합니다.</p>
    <p><b>수량</b> MNQ·MES만 사용합니다. 스프레드는 두 레그의 증거금과 비용을 합해 $100,000 안에서 헤지 비율을 맞춘 최대 수량을 고릅니다. 나스닥 단독과 S&P 단독도 각각 독립된 $100,000 한도입니다. 손실·주문 가능 수량·호가 잔량에 따라 수량은 달라집니다.</p>
    <p><b>비교·비용</b> 단독 전략과 호가 확인 제외 전략을 같은 시세·체결 가정으로 비교합니다. 마이크로 편도 $2, 추가 미끄러짐 편도 1틱 가정입니다. 왕복 비용이 계획 위험의 40%를 넘으면 진입하지 않습니다. 실제 계좌 수수료와 수익성은 검증 중입니다.</p>
  </div>
}

function BreakoutRules({ run }: { run: RotationRun }) {
  const rules = breakoutEntryRules(run)
  return <div className="rd-rules-content">
    <p><b>진입</b> 앞선 30분의 수익률로 β를 추정합니다. 최근 {rules.channelCloses}분 가격차 범위를 위로 돌파하면 MNQ 매수·MES 매도, 아래로 돌파하면 MNQ 매도·MES 매수입니다. 새 호가 {rules.confirmObservations}회에서 최소 {rules.confirmSeconds}초간 범위 밖을 유지해야 합니다. {rules.maxChaseFraction == null ? '고정 추격 제한은 없으며, 남은 목표 이익이 비용 차감 후 계획 손실 이상인지 확인합니다.' : `범위 폭의 ${rules.maxChaseFraction * 100}%를 넘는 추격은 하지 않습니다.`}</p>
    <p><b>익절·손절</b> 돌파 경계에서 최근 {rules.targetCloses}분 범위 폭만큼 더 진행하는 가격이 목표입니다. 손절은 경계 안쪽으로 같은 범위 폭의 10% 또는 최소 틱 여유를 둡니다. β·목표·손절을 진입 시 고정합니다. 비용 차감 목표가 계획 손실 이상이고, 비용 차감 전 목표가 왕복 비용의 2배 이상이어야 합니다.</p>
    <p><b>수익 보호</b> 순익이 목표의 60%에 도달하면 최고 순익의 절반이 남는 지점에서 정리합니다. 몇 분 보유했다는 이유로 청산하지 않습니다. {rules.fast ? `청산 후 ${rules.cooldownSeconds}초가 지나고 새 완료 분봉이 생기면 다시 판단합니다. 같은 기준 분봉에서는 한 번만 진입합니다.` : '새 거래는 가격이 범위 안으로 돌아온 뒤 다시 돌파할 때 판단합니다.'}</p>
    <p><b>운용</b> 과거 완료 1분봉 51개로 기준을 바로 계산합니다. 추가 5분 대기는 없습니다. 3시간 운용하며 종료 3분 전부터 신규 진입을 중단하고 보유를 정리합니다. 순손실 $2,000에 신규 진입을 잠그되 감시·복구는 계속합니다. 체결 지연·가격 급변으로 실제 손실은 계획값을 넘을 수 있습니다.</p>
    <p><b>수량·비교</b> MNQ·MES만 사용합니다. 스프레드 두 레그를 합한 증거금과 비용 예비금이 $100,000 이내인 최대 수량입니다. 각 단독 비교군은 자기 종목만으로 같은 돌파 규칙을 계산하고 독립 자금 $100,000을 사용합니다.</p>
    <p><b>검증</b> 실전 시세·앱 가상 체결입니다. 수수료 편도 $2, 추가 미끄러짐 편도 1틱과 후속 호가 잔량을 반영합니다. {rules.fast ? '완화 후 개발 재생에서 진입은 증가했지만 순손실이 발생했습니다. 수익성 개선은 입증되지 않았습니다.' : '단기 개발 재생에서 우월성과 거래 빈도는 아직 입증되지 않았습니다.'}</p>
    {run.entry_policy && <p><b>조건 변경</b> 그래프의 점선 이후 새 진입에 적용합니다. 이전 손익·손실 제한·종료 시간은 유지하며, 기존 보유는 진입 당시 손절·익절을 사용합니다.</p>}
  </div>
}

export function RotationDesk({ standalone = false }: { standalone?: boolean }) {
  const [data, setData] = useState<RotationResponse | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false)
  const [mode, setMode] = useState('max_affordable'), [selected, setSelected] = useState('spread'), [ablation, setAblation] = useState(false)
  const [detail, setDetail] = useState<'fills' | 'comparison' | 'conditions' | 'rules'>('fills')
  useEffect(() => {
    let disposed = false, timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try { const next = await rotationRequest(); if (!disposed) { setData(next); setError('') } }
      catch (e) { if (!disposed) setError(e instanceof Error ? e.message : '서버 연결 실패') }
      if (!disposed) timer = setTimeout(poll, 2000)
    }
    void poll(); return () => { disposed = true; clearTimeout(timer) }
  }, [])
  const run = data?.run
  const ready = run?.session_phase === 'ready'
  const blend = run?.model === 'partial-hedge-blend-v15' || Boolean(data?.mock)
  const arms = run?.arms.filter(a => a.sizing_mode === mode) || [], shown = ablation ? arms : arms.slice(0, 3)
  const main = arms.find(a => a.name === selected) || arms[0]
  const elapsed = run && !ready ? ((run.ended_at || Math.min(data?.server_now || run.updated_at, run.end_at)) - run.started_at) / 1000 : 0
  const remaining = ready ? run.execution.duration_seconds : run ? Math.max(0, (run.end_at - (data?.server_now || run.updated_at)) / 1000) : 10800
  const warmup = run ? run.warmup_valid_ms / (run.execution.warmup_seconds * 1000 || 1) : 0
  async function action(path: string, body?: object) {
    setBusy(true)
    try { setData(await rotationRequest(path, body ?? (path === '/start' ? { mode: 'paper' } : {}))); setError('') }
    catch (e) { setError(e instanceof Error ? e.message : '요청 실패') }
    finally { setBusy(false) }
  }
  async function save(comparison = false) {
    if (!run) return
    try { if (comparison) await saveRotationFigure(run, mode); else await saveRotationMonitorFigure(run, mode, selected) }
    catch { setError('그림 저장에 실패했습니다. 결과 ZIP에서 원본 기록을 받을 수 있습니다.') }
  }
  return <div className={`rotation-desk rd-monitor ${standalone ? 'rd-standalone' : ''}`}>
    <header className="rd-header"><div><small>NH FUTURES · STRATEGY MONITOR</small><h1>{blend ? '부분 헤지 평균회귀' : isBreakout(run) ? '가격차 돌파 · 검증' : run?.model === 'relative-depth-scalp-nq-es-v1' ? '상대 모멘텀 · 호가 확인' : '상대강도 운용'} <span>{run?.market_families ? `${familyInfo(run, 'a').label} / ${familyInfo(run, 'b').label}` : 'NASDAQ / S&P 500'}</span></h1></div>
      <div className="rd-actions">{!standalone && <a href="/?view=rotation&present=1" target="_blank" rel="noreferrer">발표 보기</a>}<button disabled={!run} onClick={() => void save()}>그림 저장</button><button disabled={!run} onClick={() => void save(true)}>비교 그림</button>{!blend && <a href="/api/strategy/rotation/export" className={!run ? 'rd-disabled' : ''}>결과 ZIP</a>}{blend && !run?.enabled && <button disabled={busy || ready} onClick={() => void action('/reset')}>{ready ? '초기화 완료' : '대회 초기화'}</button>}{run?.enabled ? <button disabled={busy || run.stop_requested} onClick={() => void action('/stop')}>{run.stop_requested ? '잔여 보유 정리 중' : '정리 후 중지'}</button> : <button className="rd-primary" disabled={busy} onClick={() => void action('/start')}>{blend ? '3시간 모의운용 시작' : '3시간 가상매매 시작'}</button>}</div>
    </header>
    <div className="rd-status"><span className={`rd-dot ${run?.feed_ok && !error ? 'connected' : ''}`} /><strong>{error ? '연결 확인 중' : ready ? '시작 대기' : run ? run.enabled ? '자동매매 운용 중' : '운용 종료' : '시작 전'}</strong><span>{blend ? '발표 전략 고정 · 해외 모의 주문' : '실전 시세 · 가상 체결'}</span><span className="rd-status-right">경과 <b>{duration(elapsed)}</b> / 잔여 <b>{duration(remaining)}</b><span>기준 자금 <b>$100,000</b></span></span></div>
    {blend && data?.mock && <section className="rd-status rd-mock-status" aria-label="NH 모의계좌 주문 상태">
      <strong>NH 모의계좌</strong><span>{data.mock.note}</span>
      {data.mock.state === 'recovering' && <span>재시도 간격 {data.mock.retry_in_seconds || 2}초 · 복구 {data.mock.recoveries || 0}회</span>}
      <span>주문 {data.mock.orders}건 · 체결 {data.mock.fills}계약 · 완료 {data.mock.completed_trades}회</span>
      <b className={pnlClass(data.mock.net)}>{money(data.mock.net)} / {percent(data.mock.return_pct)}</b>
      <span>{data.mock.positions.filter(p => p.quantity).map(p => `${p.code} ${p.quantity > 0 ? '롱' : '숏'} ${Math.abs(p.quantity)}계약`).join(' · ') || '보유 없음'}</span>
      <span>최대 낙폭 {number(data.mock.drawdown_pct ?? 0,3)}%</span>
      {data.mock.positions.some(p => p.quantity) && <span>익절 {money(data.mock.target ?? 0)} · 손절 −{money(data.mock.risk ?? 0)}</span>}
      {data.mock.last_exit_reason === '수동 청산' && <span>최근 수동 청산 <b className={pnlClass(data.mock.last_trade_net ?? 0)}>{money(data.mock.last_trade_net ?? 0)}</b></span>}
      <button className="rd-manual-close" disabled={busy || Boolean(error) || !data.mock.can_manual_close || !data.mock.trade_at}
        title="NH 모의계좌의 MNQ·MES 보유를 모두 청산합니다. 청산 후 자동매매는 계속됩니다. 최종 손익은 체결가에 따라 달라집니다."
        onClick={() => void action('/manual-close', {run_id: run?.id, trade_at: data.mock?.trade_at})}>
        {data.mock.close_requested ? '청산 중…' : '수동 익절'}
      </button>
    </section>}
    {error && <div className="rd-error" role="alert">{error} · 서버 상태 확인 중이며 마지막 수신값을 표시합니다.</div>}
    {!run || !main ? <div className="rd-empty"><h2>상대강도 스프레드 · 단일 지수 비교</h2><p>3시간 가상매매를 시작하면 가격 그래프와 각 전략의 매매 상태가 표시됩니다.</p><p>실전 시세를 사용하며 체결은 앱 안에서 계산합니다.</p></div> : <>
      <nav className="rd-mode"><div><button aria-pressed={mode === 'max_affordable'} onClick={() => setMode('max_affordable')}>최대 수량</button>{!blend && <button aria-pressed={mode === 'risk_budget'} onClick={() => setMode('risk_budget')}>위험 제한 비교</button>}</div><span>{mode === 'max_affordable' ? '증거금·호가 내 최대 수량' : '거래당 계획 위험 $500 이하'}</span><span className="rd-mode-selection">선택 전략: <b>{main.label}</b></span></nav>
      {run.execution.warmup_seconds > 0 && warmup < 1 && run.enabled && <div className="rd-warmup"><span>시세 점검</span><progress max="1" value={warmup} /><b>{duration(run.warmup_valid_ms / 1000)} / {duration(run.execution.warmup_seconds)}</b></div>}
      {run.model === 'relative-depth-scalp-nq-es-v1' && run.execution.warmup_seconds === 0 && <div className="rd-warmup"><span>기준 준비</span><span>과거 분봉 {Math.min(main.signal.available_closes ?? 0, main.signal.required_closes ?? 34)} / {main.signal.required_closes ?? 34}</span><span>{main.signal.book_pressure == null ? '새 호가 확인 중 · 최소 10초' : '호가 표본 준비됨'}</span><span>{run.feed_ok ? '최신 시세 수신' : '시세 동기화 대기'}</span></div>}
      {isBreakout(run) && <div className="rd-warmup"><span>기준 준비</span><span>과거 분봉 {Math.min(main.signal.available_closes ?? 0,51)} / 51</span><span>진입 {breakoutEntryRules(run).channelCloses}분 범위 · 확인 {breakoutEntryRules(run).confirmSeconds}초 · 보유시간 청산 없음</span><span>{run.feed_ok ? '최신 시세 수신' : '시세 동기화 대기'}</span></div>}
      {blend && <div className="rd-warmup"><span>과거 봉 {main.signal.available_closes ?? 0} / {main.signal.required_closes ?? (run.strategy_rules?.lookback ?? 120) + 2}</span><span>Z {number(main.signal.strength)} / 기준 ±{run.strategy_rules?.entry_z ?? 1.5} · {main.reason_label}</span><span>두 레그 합산 $100,000 · 부분 헤지 25%</span>{run.strategy_rules?.session_loss === null && <span>연속 운용 · 손실로 운용 중단 없음</span>}</div>}
      <CurrentState run={run} arm={main} disconnected={Boolean(error)} />
      <HoldingsOverview run={run} arms={shown} selected={selected} onSelect={setSelected} disconnected={Boolean(error)} />
      <div className="rd-section-title"><h2>전략 수익률 비교 <small>각 원금 $100,000 · 가상 체결 · 비용 포함</small></h2>{!blend && <label><input type="checkbox" checked={ablation} onChange={e => { setAblation(e.target.checked); if (!e.target.checked && !arms.slice(0, 3).some(a => a.name === selected)) setSelected('spread') }} /> 조건 제거 비교</label>}</div>
      <ReturnComparison run={run} arms={shown} selected={selected} onSelect={setSelected} />
      <InstrumentCharts key={`${mode}:${selected}`} run={run} arm={main} />
      <section className="rd-detail-panel"><div className="rd-panel-head"><nav>{([['fills', '체결 기록'], ['comparison', '상세 비교'], ['conditions', '진입 조건'], ['rules', '운용 규칙']] as const).map(([id, label]) => <button key={id} aria-pressed={detail === id} onClick={() => setDetail(id)}>{label}</button>)}</nav><span>{main.label} · 경과시간 기준</span></div>
        {detail === 'fills' ? <div className="rd-table-wrap"><table><thead><tr><th>경과</th><th>종목</th><th>매매 상태</th><th>주문 방향</th><th>체결 수량</th><th>체결가</th><th>수수료</th></tr></thead><tbody>{main.fills.slice().reverse().map((f, i) => { const state = fillLabel(main, f); return <tr key={i}><td>{elapsedLabel((f.time - run.started_at) / 1000)}</td><td>{f.code}</td><td><Badge tone={state.tone}>{state.label}</Badge></td><td>{f.side === 'buy' ? '매수' : '매도'}</td><td>{f.quantity}</td><td>{number(f.price)}</td><td>{money(f.fee)}</td></tr> })}{!main.fills.length && <tr><td colSpan={7} className="rd-empty-row">아직 체결되지 않았습니다 · {main.reason_label}</td></tr>}</tbody></table></div>
          : detail === 'comparison' ? <div className="rd-comparison rd-table-wrap"><table><thead><tr><th>전략</th><th>수익률</th><th>순손익</th><th>최대 낙폭</th><th>완료 거래</th><th>수수료</th><th>평균 진입 위험</th><th>현재 상태</th></tr></thead><tbody>{shown.map(a => <tr key={a.id}><th>{a.label}</th><td className={pnlClass(a.net)}>{percent(a.return_pct)}</td><td>{money(a.net)}</td><td>{number(a.drawdown_pct, 3)}%</td><td>{a.completed_trades}</td><td>{money(a.fees)}</td><td>{money(a.average_entry_risk)}</td><td>{displayState(run, a, Boolean(error)).label}</td></tr>)}</tbody></table></div>
          : blend ? <div className="rd-rules-content"><p><b>진입</b> S = ln(MNQ) − 0.25 × ln(MES). 신호 직전 {run.strategy_rules?.lookback ?? 120}개 확정 분봉의 평균·표준편차로 Z를 계산합니다. Z ≥ +1.5에서 S가 하락하면 MNQ 숏·MES 롱, Z ≤ −1.5에서 S가 상승하면 MNQ 롱·MES 숏입니다. 두 지수의 최근 5분 방향이 반대이면 제외합니다.</p><p><b>수량</b> 두 레그 합산 $100,000 안에서 증거금·주문 가능 수량·호가 잔량·거래량을 반영해 정합니다. MES 명목금액은 MNQ의 약 25%이며 시장 방향 위험이 남습니다. 단일 비교군은 각각 독립 자금으로 같은 신호를 계산합니다.</p><p><b>청산</b> 순익 목표 = min(1.5σD − C, $600), 손절 폭 = min(0.75σD + C, $600). D는 MNQ 명목금액, C는 예상 왕복 비용입니다. 진입 시 값을 고정하고 C가 손절 폭의 40%를 넘으면 진입하지 않습니다. 실제 체결 손익은 기준을 넘을 수 있습니다.</p><p><b>운용</b> 시작부터 3시간, 마지막 60초는 신규 진입 없이 보유를 정리합니다. 연속 손실 중단·보유시간 청산은 없습니다. 연결·주문 오류는 계좌 대조 후 자동 복구하며, 접수가 불명확한 신규 주문은 중복 전송하지 않습니다.</p><p><b>집계</b> 비교 그래프는 앱 가상 체결, NH 모의계좌는 상단 별도 집계입니다. 수수료는 편도 계약당 $2 가정입니다.</p></div>
          : detail === 'conditions' ? <Conditions arm={main} run={run} />
          : isBreakout(run) ? <BreakoutRules run={run} />
          : run.model === 'relative-depth-scalp-nq-es-v1' ? <ScalpRules />
          : <div className="rd-rules-content"><p><b>진입</b> 앞선 60분으로 β를 추정하고 최근 10분의 상대강도를 평가합니다. 상승·하락 구간에서 방향이 일치하고 강도·비용 조건을 충족하면 진입합니다.</p><p><b>청산</b> 진입 시 고정한 −1R 또는 추세 이탈에서 손절, +2R에서 익절합니다. +1R 이후 최고 순익에서 0.5R 되밀리면 수익을 보호합니다. 최대 10분 보유는 보조 종료 조건입니다.</p><p><b>운용</b> 종료 10분 전 신규 진입 중단, 3분 전 정리 시작. 각 전략의 누적 순손실 $2,000에 신규 진입을 잠급니다. 시세 공백에서는 가상 체결을 만들지 않습니다.</p><p><b>비교</b> 각 전략의 독립 자금은 $100,000입니다. 단일 지수 전략은 해당 지수만으로 신호를 계산합니다. 수수료 가정은 마이크로 편도 $2, 미니 $8이며 계좌별 검증 전입니다.</p></div>}
      </section>
      <footer className="rd-footer"><span>{blend ? '비교 그래프: 앱 가상 체결 · NH 모의계좌: 상단 별도 집계' : '실전 시세 · 앱 가상 체결 · 실제 계좌 주문 전송 없음'}</span><span>{run.entry_policy ? '진입 조건 변경 전 손익 포함 · 기존 보유의 손절·익절 유지' : run.execution_policy?.effective_at ? '종목 제한 적용 전 거래·손익 포함 · 이후 MNQ/MES 전용' : run.capacity_error ? `주문 가능 수량 갱신 대기 · ${run.capacity_error}` : run.feed_error || '각 전략 독립 자금 $100,000 · 수수료·체결 가정 동일'}</span></footer>
    </>}
  </div>
}
