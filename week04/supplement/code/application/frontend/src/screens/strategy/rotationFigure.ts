import type { RotationRun } from '../../data/rotation'
import { isBreakout } from '../../data/rotationView'
import { accountSummary, capitalSummary, codeState, comparisonScale, currentMid, displayState, exitLabel, familyInfo, heldPositions, lastClosedTrade, markerLabels, marketPoints, pairTradeView, positionMargin, preferredCode, tradeCycleMarkers, tradeMarkers, tradePricePoints, tradeRisk, tradeStatistics, type ChartPoint } from '../../data/rotationView'

// Fixed-size presentation figure; no account identifier or wall clock.
export async function saveRotationFigure(run: RotationRun, mode: string) {
  await document.fonts.ready
  const canvas = document.createElement('canvas')
  canvas.width = 1600; canvas.height = 1000
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('Canvas unavailable')
  const colors = ['#007f68', '#3878c3', '#b07b26']
  const arms = run.arms.filter(a => a.sizing_mode === mode).slice(0, 3)
  const money = (v: number) => `${v < 0 ? '−' : ''}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
  const pct = (v: number) => `${v > 0 ? '+' : ''}${v.toFixed(3)}%`
  const elapsed = Math.max(0, ((run.ended_at || run.updated_at) - run.started_at) / 1000)
  const clock = (v: number) => `${String(Math.floor(v / 3600)).padStart(2, '0')}:${String(Math.floor(v / 60) % 60).padStart(2, '0')}:${String(Math.floor(v) % 60).padStart(2, '0')}`
  const text = (value: string, x: number, y: number, size = 20, color = '#192a3e', bold = false) => {
    ctx.font = `${bold ? '600' : '400'} ${size}px "Malgun Gothic",sans-serif`; ctx.fillStyle = color; ctx.fillText(value, x, y)
  }
  const line = (x: number, y: number, x2: number, y2: number, color = '#dce2e9') => {
    ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x2, y2); ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.stroke()
  }
  ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, 1600, 1000)
  ctx.fillStyle = '#007f68'; ctx.fillRect(0, 0, 1600, 7)
  text('NH FUTURES  /  STRATEGY REVIEW', 60, 54, 15, '#63758b')
  text(isBreakout(run) ? '가격차 돌파 / 단일 지수 비교' : run.model === 'relative-depth-scalp-nq-es-v1' ? '상대 모멘텀 · 호가 확인 / 단일 지수 비교' : '상대강도 스프레드 · 단일 지수 비교', 60, 107, 36, '#172b43', true)
  text('실전 시세 · 가상 체결', 1260, 62, 19, '#007f68', true)
  text(`${run.enabled ? '진행 중' : '종료'}  +${clock(elapsed)}`, 1260, 100, 19, '#63758b')
  text(`${mode === 'max_affordable' ? '최대 수량 운용' : '거래당 계획 위험 $500 제한'}   |   독립 자금 $100,000 / 전략   |   비용 차감 수익률`, 60, 148, 18, '#63758b')
  line(60, 168, 1540, 168)
  arms.forEach((a, i) => {
    const x = 60 + i * 505
    ctx.fillStyle = colors[i]; ctx.fillRect(x, 197, 7, 18)
    text(a.label, x + 18, 213, 22, '#172b43', true)
    text(pct(a.return_pct), x, 265, 38, colors[i], true)
    text(`${money(a.net)}   /   MDD(원금) ${a.drawdown_pct.toFixed(3)}%`, x, 299, 18, '#63758b')
  })
  const plot = { left: 137, right: 1530, top: 343, bottom: 606 }
  const points = arms.flatMap(a => a.curve).filter(p => p.fresh)
  const low = Math.min(0, ...points.map(p => p.return_pct)), high = Math.max(0, ...points.map(p => p.return_pct))
  const pad = Math.max((high - low) * .15, .005), lo = low - pad, hi = high + pad
  const px = (v: number) => plot.left + v / Math.max(elapsed, 1) * (plot.right - plot.left)
  const py = (v: number) => plot.top + (hi - v) / (hi - lo) * (plot.bottom - plot.top)
  for (let i = 0; i <= 4; i++) {
    const v = lo + (hi - lo) * i / 4
    line(plot.left, py(v), plot.right, py(v)); text(`${v.toFixed(3)}%`, 60, py(v) + 6, 15, '#738196')
    const t = elapsed * i / 4
    text(`+${clock(t)}`, px(t) - 40, plot.bottom + 29, 15, '#738196')
  }
  line(plot.left, py(0), plot.right, py(0), '#a4b2c2')
  if (run.execution_policy?.effective_at) {
    const changed = (run.execution_policy.effective_at - run.started_at) / 1000
    ctx.setLineDash([2, 5]); line(px(changed), plot.top, px(changed), plot.bottom, '#a4b2c2'); ctx.setLineDash([])
    text('MNQ·MES 전용 적용', Math.min(plot.right - 165, px(changed) + 6), plot.top - 10, 13, '#63758b')
  }
  if (run.entry_policy) {
    const changed = (run.entry_policy.effective_at - run.started_at) / 1000
    ctx.setLineDash([3, 5]); line(px(changed), plot.top, px(changed), plot.bottom, '#a09a7c'); ctx.setLineDash([])
    text('진입 조건 변경 · 이전 손익 포함', Math.min(plot.right - 215, px(changed) + 6), plot.top - 10, 13, '#63758b')
  }
  arms.forEach((a, i) => {
    ctx.beginPath(); let connected = false
    for (const p of a.curve) {
      if (!p.fresh) { connected = false; continue }
      if (connected) ctx.lineTo(px(p.elapsed), py(p.return_pct)); else ctx.moveTo(px(p.elapsed), py(p.return_pct))
      connected = true
    }
    ctx.strokeStyle = colors[i]; ctx.lineWidth = 2.5; ctx.stroke()
  })
  const xs = [60, 460, 720, 1000, 1265]
  ctx.fillStyle = '#f1f4f8'; ctx.fillRect(60, 665, 1480, 47)
  ;['전략', '완료 거래', '수수료', '평균 진입 위험', '체결 기록 수'].forEach((v, i) => text(v, xs[i] + 12, 696, 18, '#63758b'))
  arms.forEach((a, i) => {
    const y = 750 + i * 52
    const values = [a.label, `${a.completed_trades}건`, money(a.fees), a.average_entry_risk != null ? money(a.average_entry_risk) : '—', `${a.fill_count}건`]
    values.forEach((v, j) => text(v, xs[j] + 12, y, 20, j === 0 ? colors[i] : '#192a3e', j === 0))
    line(60, y + 16, 1540, y + 16)
  })
  text('동일 시세·수수료·체결 지연 적용. 단일 지수 전략은 해당 지수만으로 신호를 계산하며, 진입 시점은 서로 다릅니다.', 60, 921, 16, '#63758b')
  text('가상 체결: 후속 호가 잔량 / 0.3초 이상 지연 / 편도 1틱 추가. 수수료 가정: 마이크로 $2, 미니 $8 편도. 실제 계좌 체결 아님.', 60, 951, 15, '#63758b')
  if (run.enabled) text('중간 결과 · 수익성 미검증', 1270, 981, 15, '#63758b')
  const blob = await new Promise<Blob>((resolve, reject) => canvas.toBlob(b => b ? resolve(b) : reject(new Error('PNG export failed')), 'image/png'))
  const url = URL.createObjectURL(blob), anchor = document.createElement('a')
  anchor.href = url; anchor.download = `strategy-comparison-${mode}.png`; anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export async function saveRotationMonitorFigure(run: RotationRun, mode: string, selected: string) {
  await document.fonts.ready
  const canvas = document.createElement('canvas')
  canvas.width = 1600; canvas.height = 1200
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('Canvas unavailable')
  const arms = run.arms.filter(a => a.sizing_mode === mode).slice(0, 3)
  const main = run.arms.find(a => a.sizing_mode === mode && a.name === selected) || arms[0]
  const state = displayState(run, main), risk = tradeRisk(run, main), last = lastClosedTrade(main)
  const pair = pairTradeView(run, main)
  const account = accountSummary(run, main), capital = capitalSummary(run, main), statistics = tradeStatistics(run, main)
  const colors = ['#008369', '#397fc5', '#ac7b28']
  const fmt = (v: number | null | undefined, d = 2) => v == null ? '—' : v.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d })
  const money = (v: number | null | undefined) => v == null ? '—' : `${v < 0 ? '−' : ''}$${fmt(Math.abs(v))}`
  const duration = (v: number) => `${String(Math.floor(Math.abs(v) / 3600)).padStart(2, '0')}:${String(Math.floor(Math.abs(v) / 60) % 60).padStart(2, '0')}:${String(Math.floor(Math.abs(v)) % 60).padStart(2, '0')}`
  const text = (value: string, x: number, y: number, size = 18, color = '#27384d', bold = false) => {
    ctx.font = `${bold ? '600' : '400'} ${size}px "Malgun Gothic",sans-serif`; ctx.fillStyle = color; ctx.fillText(value, x, y)
  }
  const line = (x: number, y: number, x2: number, y2: number, color = '#dce3eb') => {
    ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x2, y2); ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.stroke()
  }
  const chart = (points: ChartPoint[], x: number, y: number, width: number, height: number, color: string, price = false, code?: string) => {
    const valid = points.filter(p => p.valid !== false && Number.isFinite(p.y))
    const markers = code ? pair ? tradeCycleMarkers(run, main, code, pair.trade) : tradeMarkers(run, main, code) : []
    const x0 = points.length ? Math.min(...points.map(p => p.x)) : 0, x1 = Math.max(x0 + 1, ...points.map(p => p.x))
    const visible = markers.filter(p => p.x >= x0 && p.x <= x1)
    const values = [...valid.map(p => p.y), ...visible.map(p => p.y), ...(price ? [] : [0])]
    const low = values.length ? Math.min(...values) : 0, high = values.length ? Math.max(...values) : 0
    const pad = Math.max((high - low) * .18, price ? .5 : .005), lo = low - pad, hi = high + pad
    const left = x + (price ? 84 : 63), right = x + width - 15, top = y + 10, bottom = y + height - 25
    const px = (v: number) => left + (v - x0) / (x1 - x0) * (right - left)
    const py = (v: number) => top + (hi - v) / (hi - lo) * (bottom - top)
    for (let i = 0; i < 3; i++) {
      const value = lo + (hi - lo) * i / 2
      line(left, py(value), right, py(value)); text(fmt(value, price ? 2 : 3) + (price ? '' : '%'), x, py(value) + 5, 12, '#738196')
      const t = x0 + (x1 - x0) * i / 2
      text(`${t < 0 ? '−' : '+'}${duration(t)}`, px(t) - 31, y + height - 5, 12, '#738196')
    }
    if (!price) line(left, py(0), right, py(0), '#a1afc0')
    ctx.beginPath(); let connected = false
    for (const p of points) {
      if (p.valid === false || !Number.isFinite(p.y)) { connected = false; continue }
      if (connected) ctx.lineTo(px(p.x), py(p.y)); else ctx.moveTo(px(p.x), py(p.y))
      connected = true
    }
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke()
    const labels = markerLabels(visible.map(p => ({ ...p, x:px(p.x), y:py(p.y) })), { left, right, top:y+2, bottom })
    visible.forEach((p, i) => {
      const color = !p.entry ? '#758295' : p.tone === 'long' ? '#ba5253' : '#397fc5'
      ctx.beginPath()
      if (p.entry) {
        const direction = p.tone === 'long' ? 1 : -1
        ctx.moveTo(px(p.x), py(p.y) - 5 * direction); ctx.lineTo(px(p.x) - 5, py(p.y) + 4 * direction); ctx.lineTo(px(p.x) + 5, py(p.y) + 4 * direction); ctx.closePath()
      } else ctx.arc(px(p.x), py(p.y), 4, 0, Math.PI * 2)
      ctx.fillStyle = p.entry ? color : '#fff'; ctx.strokeStyle = color; ctx.fill(); ctx.stroke()
      const label = labels[i]
      if (label) { ctx.fillStyle = '#fff'; ctx.fillRect(label.box.x, label.box.y, label.box.width, label.box.height); text(p.label, label.x-p.label.length*6, label.y+4, 12, color) }
    })
    if (!valid.length) text('가격 자료 수신 대기', left + 40, top + 50, 16, '#738196')
  }
  ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, 1600, 1200)
  ctx.fillStyle = '#007f68'; ctx.fillRect(0, 0, 1600, 6)
  text('NH FUTURES / STRATEGY MONITOR', 50, 43, 14, '#6c7c91')
  text('보유 현황 · 순손익 · 수익률', 50, 92, 34, '#172c45', true)
  text('실전 시세 · 가상 체결', 1280, 47, 19, '#007f68', true)
  text(`${mode === 'max_affordable' ? '최대 수량' : '위험 제한 비교'} · 경과 +${duration((run.updated_at - run.started_at) / 1000)}`, 1160, 85, 17, '#6c7c91')
  text(`${main.name === 'spread' ? '주 전략' : '비교군'} · ${main.label}  /  ${state.label}`, 50, 137, 23, '#172c45', true)
  text(state.detail, 870, 137, 16, '#6c7c91')
  ctx.fillStyle = '#f1f5f8'; ctx.fillRect(50, 156, 1500, 105)
  ;[['총 순손익', money(account.total)], ['수익률', `${account.returnPct > 0 ? '+' : ''}${fmt(account.returnPct, 3)}%`],
    ['현재 거래 손익', money(account.current)], ['청산 완료 순손익', money(account.closed)],
    ['거래 횟수', `${statistics.entries}회 (완료 ${statistics.completed})`], ['MDD · 원금 기준', `${fmt(statistics.drawdownPct, 3)}%`],
    ['평가자산', money(account.equity)], ['누적 수수료', money(account.fees)]].forEach(([label, value], i) => {
    const x = 67 + i * 186; text(label, x, 185, 15, '#6c7c91'); text(value, x, 225, 22, '#24394f', true)
  })
  text(`사용 증거금 ${money(capital.used)} (${fmt(capital.usedPct, 1)}%)    명목보유금액 ${money(capital.gross)} (${fmt(capital.exposureMultiple, 1)}배)`, 50, 290, 18, '#455970')
  text(`수량 계산 한도 ${money(capital.budget)}    손절 ${money(risk?.stop)} / 익절 ${money(risk?.target)}`, 50, 321, 17, '#455970')
  text(last ? `${exitLabel(last.exit_reason).label} 완료 · ${money(last.net)}` : '최근 청산 없음', 1100, 321, 16, '#6c7c91')
  ctx.fillStyle = '#f1f5f8'; ctx.fillRect(50, 340, 1500, 32)
  const columns = [62, 270, 430, 600, 825, 1070, 1335]
  ;(pair ? ['체결 종목', '진입 방향', '진입 체결 / 주문', '평균 진입가', '평균 청산가', '레그 순손익', '거래 상태'] : ['종목', '방향', '보유 계약', '평균 진입가', '청산 평가가', '평가손익', '개별 증거금']).forEach((v, i) => text(v, columns[i], 362, 15, '#6c7c91'))
  const held = heldPositions(main)
  if (!held.length && !pair) text('보유 없음 · 아직 주문되지 않은 검토 후보는 보유에 포함하지 않습니다.', 62, 402, 17, '#6c7c91')
  if (pair) pair.legs.forEach((p, i) => {
    const y = 401 + i * 30
    ;[p.code, p.direction > 0 ? '롱' : '숏', `${p.entryQuantity} / ${p.planned}`, fmt(p.entryPrice), fmt(p.exitPrice), money(p.net), pair.closed ? '청산 완료' : p.held ? `보유 ${p.held}계약` : '미체결']
      .forEach((v, j) => text(v, columns[j], y, 17, j === 1 ? p.direction > 0 ? '#b64f50' : '#397fc5' : '#27384d', j === 2))
    line(50, y + 10, 1550, y + 10)
  })
  else held.forEach((p, i) => {
    const y = 401 + i * 30
    ;[p.code, p.quantity > 0 ? '롱 · 매수' : '숏 · 매도', `${Math.abs(p.quantity)}계약`, fmt(p.average), fmt(p.mark_price), money(p.unrealized_net), money(positionMargin(run, p.code, p.quantity))]
      .forEach((v, j) => text(v, columns[j], y, 17, j === 1 ? p.quantity > 0 ? '#b64f50' : '#397fc5' : '#27384d', j === 2))
    line(50, y + 10, 1550, y + 10)
  })
  if (pair) text(`${pair.label}${pair.legacy ? ' · 종목 제한 적용 전 기록' : ''} · 합산 ${money(pair.net)}`, 50, 501, 17, '#6c7c91')
  ;(['a', 'b'] as const).forEach((family, i) => {
    const leg = pair?.legs.find(l => l.family === family)
    const x = 50 + 775 * i, code = leg?.code || preferredCode(main, family, run), mid = currentMid(run, code), holding = codeState(main, code)
    text(`${familyInfo(run, family).label}  ${code}`, x, 544, 23, colors[i + 1], true)
    text(leg ? `${leg.direction > 0 ? '롱' : '숏'} ${leg.entryQuantity}계약${pair?.closed ? ' · 청산 완료' : ''}` : holding.label, x + 440, 543, 18, '#455970', true)
    text(fmt(pair?.closed ? leg?.exitPrice : mid?.price), x, 577, 26, '#172c45', true)
    if (leg) text(`진입 ${fmt(leg.entryPrice)} · 레그 순손익 ${money(leg.net)}`, x + 245, 577, 16, '#455970')
    chart(pair ? tradePricePoints(run, code, pair.trade) : marketPoints(run, code), x, 592, 725, 168, colors[i + 1], true, code)
  })
  line(50, 783, 1550, 783)
  text('전략별 수익률 · 독립 자금 $100,000', 50, 817, 21, '#172c45', true)
  arms.forEach((a, i) => {
    const x = 50 + i * 515
    text(a.label, x, 854, 20, colors[i], true)
    text(displayState(run, a).label, x + 302, 854, 16, '#6c7c91')
    text(`${a.return_pct > 0 ? '+' : ''}${fmt(a.return_pct, 3)}%`, x, 894, 30, colors[i], true)
    text(money(a.net), x + 225, 891, 18, '#455970')
    text(`거래 ${tradeStatistics(run, a).entries}회 · MDD(원금) ${fmt(a.drawdown_pct, 3)}% · 수수료 ${money(a.fees)}`, x, 920, 13, '#6c7c91')
  })
  const scale = comparisonScale(arms), left = 130, right = 1535, top = 947, bottom = 1070
  const px = (t: number) => left + t / scale.x1 * (right-left)
  const py = (v: number) => top + (scale.high-v) / (scale.high-scale.low) * (bottom-top)
  for (let i=0; i<4; i++) {
    const v=scale.low+(scale.high-scale.low)*i/3, t=scale.x1*i/3
    line(left,py(v),right,py(v)); text(fmt(v,3)+'%',50,py(v)+4,12,'#738196')
    text('+'+duration(t),Math.min(right-70,px(t)),1094,12,'#738196')
  }
  arms.forEach((a,i) => {
    ctx.beginPath(); let connected=false
    for (const p of a.curve) {
      if (!p.fresh) { connected=false; continue }
      if (connected) ctx.lineTo(px(p.elapsed),py(p.return_pct)); else ctx.moveTo(px(p.elapsed),py(p.return_pct))
      connected=true
    }
    ctx.strokeStyle=colors[i]; ctx.lineWidth=2; ctx.stroke()
  })
  if (run.execution_policy?.effective_at) {
    const at=(run.execution_policy.effective_at-run.started_at)/1000
    ctx.setLineDash([2,5]); line(px(at),top,px(at),bottom,'#a4b2c2'); ctx.setLineDash([])
    text('MNQ·MES 전용 적용',Math.min(right-160,px(at)+5),top-5,12,'#738196')
  }
  if (run.entry_policy) {
    const at = (run.entry_policy.effective_at - run.started_at) / 1000
    ctx.setLineDash([3, 5]); line(px(at),top,px(at),bottom,'#a09a7c'); ctx.setLineDash([])
    text('진입 조건 변경 · 이전 손익 포함',Math.min(right-210,px(at)+5),top-5,12,'#738196')
  }
  line(50, 1110, 1550, 1110)
  text('총 순손익 = 청산 완료 순손익 + 현재 거래 손익. 현재 거래는 진입 수수료와 예상 청산 비용을 포함합니다. 증거금은 실제 보유 수량 기준입니다.', 50, 1141, 15, '#6c7c91')
  text('실제 계좌 체결 아님 · 수수료 가정: 마이크로 $2, 미니 $8 편도 · 추가 불리한 체결 1틱 · 수익성 미검증', 50, 1174, 14, '#6c7c91')
  const blob = await new Promise<Blob>((resolve, reject) => canvas.toBlob(b => b ? resolve(b) : reject(new Error('PNG export failed')), 'image/png'))
  const url = URL.createObjectURL(blob), link = document.createElement('a')
  link.href = url; link.download = `strategy-monitor-${mode}.png`; link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
