import { useEffect, useRef, useState } from 'react'
import { readHynixDesk, startHynixTask, type HynixDesk, type PaperEvent } from '../../data/hynixPaper'
import './HynixPaperDesk.css'

const won = new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 0, signDisplay: 'exceptZero' })
const time = new Intl.DateTimeFormat('ko-KR', {
  timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
})
const stamp = (value: string | null) => value ? time.format(new Date(value)) : '—'
const money = (value: number | null) => value === null ? '—' : won.format(value) + '원'
const modeLabels = { before: '장 시작 전', overnight: '새벽 연결 점검', day: '주간 운용', closed: '운용 시간 종료' }
const taskLabels = { ready: '시작 대기', running: '주문 프로그램 실행 중', queued: '시작 요청됨', unknown: '작업 상태 미확인', unavailable: '예약 확인 필요' }
const ledgerLabels: Record<string, string> = { monitoring: '주간 감시', finished_flat: '왕복 체결 · 잔고 확인',
  finished_no_trade: '장 종료 · 거래 없음', unreconciled: '마감 잔고 확인 필요' }
const names: Record<string, string> = { signal: '진입 판단', paper_exit_signal: '청산 판단', order_intent: '주문 요청',
  ack: '주문 접수', confirmed_fill: 'NH 확인 체결', order_refused: '주문 거부', ack_unknown: '접수 확인 중',
  ack_recovered_from_history: '접수 확인', unfilled_terminal: '미체결 종료', cancel_intent: '취소 요청', session_end: '운용 마감' }
const actions = ['대기', '45분 매도', '45분 매수', '계획 청산까지 매도', '계획 청산까지 매수']

function eventText(e: PaperEvent) {
  if (e.kind === 'signal') return actions[e.action ?? -1] ?? '—'
  if (e.kind === 'paper_exit_signal') return e.exit ? '조기 청산' : '보유 유지'
  if (e.kind === 'session_end') return ledgerLabels[e.phase ?? ''] ?? '—'
  return [e.role === 'entry' ? '진입' : e.role === 'exit' ? '청산' : null,
    e.side === 1 ? '매수' : e.side === -1 ? '매도' : null, e.quantity ? e.quantity + '계약' : null,
    e.price ? e.price.toLocaleString('ko-KR') + '원' : null, e.order_id ? '주문 ' + e.order_id : null].filter(Boolean).join(' · ') || '—'
}

function ConnectionItem({ label, ok }: { label: string; ok: boolean | null }) {
  return <div><dt>{label}</dt><dd className={ok === false ? 'hp-warn' : ''}>
    {ok === null ? '미확인' : ok ? '조회 성공' : '조회 실패'}</dd></div>
}

export function HynixPaperDesk() {
  const [desk, setDesk] = useState<HynixDesk | null>(null)
  const [refresh, setRefresh] = useState(0)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const sending = useRef(false)
  useEffect(() => {
    let active = true
    let timer = 0
    const controller = new AbortController()
    async function poll() {
      try {
        const next = await readHynixDesk(controller.signal)
        if (active) { setDesk(next); setError('') }
      } catch (e) {
        if (active) setError(e instanceof Error ? e.message : '상태 조회 실패')
      } finally {
        if (active) timer = window.setTimeout(poll, 10_000)
      }
    }
    void poll()
    return () => { active = false; controller.abort(); window.clearTimeout(timer) }
  }, [refresh])

  async function submit() {
    if (!desk?.action || sending.current || error) return
    sending.current = true
    setBusy(true)
    setNotice('')
    try {
      const result = await startHynixTask(desk.action)
      setNotice(result.message)
    } catch (e) {
      setNotice(e instanceof Error ? e.message : '예약 작업 실행 실패')
    } finally {
      sending.current = false
      setBusy(false)
      setRefresh(n => n + 1)
    }
  }
  if (!desk) return <div className="card hp-loading" role="status">{error || '모델과 Windows 예약을 확인하고 있습니다.'}
    {error && <button className="btn btn-quiet" onClick={() => setRefresh(n => n + 1)}>다시 조회</button>}</div>
  const { connection, live, model } = desk
  const canStart = Boolean(desk.action) && !busy && !error
  const cta = busy ? '요청 중…' : desk.action === 'probe' ? '모의계좌 다시 조회'
    : desk.phase === 'closed' ? '운용 시간 종료' : desk.task === 'running' ? '모의거래 실행 중' : '모의거래 시작·재개'
  const preopen = desk.phase === 'before'
  const monitoring = desk.task === 'running' && desk.heartbeat
  const clockStatus = monitoring ? (preopen ? '모델 가동 · 국내장 개장 대기' : '새벽 점검 중') : '주간 기록 대기'
  const position = live.local_position === null ? '—' : live.local_position === 0 ? '없음' : live.local_position > 0 ? '매수 1계약' : '매도 1계약'

  return <div className="hp-desk">
    <header className="card hp-head"><div><div className="hp-identity"><h2>SK하이닉스 강화학습 모의거래</h2>
      <span className="hp-paper">주문 계좌 · NH 모의 전용</span></div>
      <p>2026.11.20 금 · 국내 주식선물 12월물 A506C000 · 하루 1계약 왕복</p></div>
      <div className="hp-buttons"><button type="button" className="btn btn-quiet" onClick={() => setRefresh(n => n + 1)}>새로고침</button>
        <button type="button" className="btn btn-primary" disabled={!canStart} onClick={() => void submit()}>{cta}</button></div>
    </header>
    {(error || notice) && <p className="hp-message" role="status">{error || notice}</p>}
    <div className="hp-grid">
      <section className="card"><h3>모의계좌 운용</h3>
        <p className="hp-stage">{modeLabels[desk.phase]} <span>{desk.archived ? '11월 20일 마감 기록' : taskLabels[desk.task]}</span></p>
        <dl className="hp-list">{desk.started && <div><dt>장전 프로그램 시작</dt><dd>{stamp(desk.started)}</dd></div>}
          <div><dt>다음 Windows 재시작 점검</dt><dd>{stamp(desk.next)}</dd></div>
          <div><dt>운용 기록</dt><dd>{ledgerLabels[live.phase ?? ''] ?? clockStatus}</dd></div>
          <div><dt>최근 운용 기록 시각</dt><dd>{stamp(live.as_of_kst || desk.heartbeat)}</dd></div></dl>
        <p className="hp-foot">국내 모의거래 09:00~15:45 · 09:30 첫 진입 판단 · 16:00 잔고 대조</p>
      </section>
      <section className="card"><h3>NH 연결 상태</h3>
        <dl className="hp-list"><ConnectionItem label={preopen || desk.phase === 'overnight' ? '직전 거래일 시세' : '시세'} ok={connection.quote} />
          <ConnectionItem label="모의 잔고" ok={connection.position} /><ConnectionItem label="모의 주문내역" ok={connection.orders} /></dl>
        <p className={'hp-foot' + (connection.stale ? ' hp-warn' : '')}>
          {connection.stale ? '최근 장중 응답 지연 · ' : '최근 조회 · '}{stamp(connection.at)} KST</p>
      </section>
    </div>
    <section className="card"><h3>오늘 주문·체결</h3>
      <dl className="hp-tiles">{[['진입 판단', live.entry_decisions], ['NH 확인 체결', live.confirmed_fills],
        ['완료 왕복', live.round_trips], ['체결 기준 보유', position], ['누적 실현손익¹', money(live.estimated_net_from_confirmed_fills_krw)]].map(([k, v]) =>
          <div key={k}><dt>{k}</dt><dd>{v ?? '—'}</dd></div>)}</dl>
      <p className="hp-foot">{live.unresolved_order && '접수 여부 확인 중 · '}¹ NH 확인 체결가에 추정 수수료를 적용합니다.</p>
      {desk.events?.length ? <div className="hp-table-wrap"><table><thead><tr><th>기록 시각</th><th>구분</th><th>내용</th></tr></thead>
        <tbody>{desk.events.map(e => <tr key={e.id}><td>{e.at.slice(11,19)}</td><td>{names[e.kind] ?? e.kind}</td><td>{eventText(e)}</td></tr>)}</tbody></table></div>
        : <p className="hp-empty">{desk.events === null ? '주문 기록을 읽는 중입니다.' : '아직 기록된 신호·주문·체결이 없습니다.'}</p>}
    </section>
    <div className="hp-grid"><section className="card hp-policy"><h3>확정 모델과 매매 규칙</h3>
      <p><b>진입</b> 5분봉으로 09:30~14:30에 30분마다 대기와 매수·매도를 비교합니다. 보유 기한은 45분 또는 15:25입니다.</p>
      <p><b>청산</b> 보유 후 30분마다 지금 청산할 때와 계속 보유할 때의 예상 순손익을 비교합니다. 보유 가치가 음수면 조기 청산합니다.</p>
      <p className="hp-foot">진입 {model.entryDays}일 · 청산 {model.exitDays}일 학습 · {stamp(model.frozen)} 확정</p>
    </section><section className="card"><h3>백테스트와 오늘의 비교 후보</h3>
      <div className="hp-table-wrap"><table><thead><tr><th>전략</th><th>검증 39일</th><th>테스트 23일</th></tr></thead>
        <tbody>{model.history.map((r, i) => <tr key={i}><td>{i === 0 ? '모의주문 모델' : '시세 비교 후보'}</td><td>{money(r.validation)}</td><td>{money(r.test)}</td></tr>)}</tbody></table></div>
      <p className="hp-foot">비교 후보는 별도로 진입·청산을 판단합니다. 오늘 시세 기준 손익 {money(desk.shadow.net)}{desk.shadow.at ? ' · ' + stamp(desk.shadow.at) : ''}</p>
    </section></div>
  </div>
}
