export type PaperAction = 'probe' | 'run'
type Stage = 'before' | 'overnight' | 'day' | 'closed'
export interface PaperEvent {
  id: number; at: string; kind: string; role?: string; action?: number; exit?: boolean
  side?: number; quantity?: number; price?: number; order_id?: string; broker_fill_time?: string; phase?: string
}
export interface HynixDesk {
  archived?: boolean
  now: string; phase: Stage; task: 'ready' | 'running' | 'queued' | 'unknown' | 'unavailable'
  next: string | null; action: PaperAction | null; heartbeat: string | null; started: string | null
  model: { entry: string; exit: string; entryDays: number; exitDays: number; frozen: string
    history: { validation: number; test: number; wins: number; days: number }[] }
  connection: { at: string | null; quote: boolean | null; orders: boolean | null; position: boolean | null; stale: boolean }
  live: { as_of_kst: string | null; phase: string | null; confirmed_fills: number | null; round_trips: number | null
    local_position: number | null; unresolved_order: boolean | null; estimated_net_from_confirmed_fills_krw: number | null
    five_minute_bars: number | null; entry_decisions: number | null }
  events: PaperEvent[] | null
  shadow: { at: string | null; net: number | null; outcome: string | null }
}

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(typeof body.detail === 'string' ? body.detail : '운용 상태를 읽지 못했습니다.')
  }
  return response.json()
}

export function readHynixDesk(signal: AbortSignal) {
  return fetch('/api/strategy/hynix-paper', { signal, cache: 'no-store' }).then(json<HynixDesk>)
}

export function startHynixTask(action: PaperAction) {
  return fetch('/api/strategy/hynix-paper/start', { method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-NH-Paper-Desk': '1' },
    body: JSON.stringify({ action }) }).then(json<{ message: string }>)
}
