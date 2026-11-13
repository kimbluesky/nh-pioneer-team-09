export interface RotationPoint { elapsed: number; net: number; return_pct: number; fresh: boolean }
export interface RotationSignal {
  eligible: boolean; reason: string; direction: number; beta?: number; correlation?: number
  strength?: number; up_excess_bps?: number; down_excess_bps?: number; available_closes?: number
  required_closes?: number; up_count?: number; down_count?: number; stop_return?: number
  sigma?: number; breadth?: number; book_pressure?: number | null; relative_move_bps?: number
  breakout_confirmed?: boolean; chase_fraction?: number; target_return?: number
  channel_low?: number; channel_high?: number; channel_level?: number; channel_width?: number
}
export interface RotationTicket { code: string; quantity: number }
export interface RotationRoute { tickets: RotationTicket[]; notional: number; equivalent_micro_quantity: number; margin?: number }
export interface RotationPlan {
  reason?: string
  available_cash?: number
  chosen?: { leg_a?: RotationRoute; leg_b?: RotationRoute; leg?: RotationRoute; risk: number
    target_net: number; estimated_round_trip_cost: number; margin: number; gross_notional: number
    hedge_error_pct?: number; cost_acceptable: boolean; cost_fraction_of_risk: number
    net_reward_risk?: number; target_gross?: number }
}
export interface RotationPosition {
  code: string; quantity: number; average: number; multiplier: number; family: string
  mark_price?: number; unrealized_net?: number
}
export interface RotationFill {
  code: string; time: number; side: string; quantity: number; price: number; fee: number; purpose: string
  orderId?: string
  family?: string; multiplier?: number
}
export interface RotationOrder {
  id: string; code: string; side: string; quantity: number; filled: number; status: string; purpose: string; created: number
}
export interface RotationTrade {
  risk: number; target_net: number; peak_net: number; current_net?: number; starting_net: number
  entry_at: number; opened_at?: number | null; closed_at?: number; net?: number; filled?: boolean
  plan: NonNullable<RotationPlan['chosen']>; exit_reason?: string | null; expected: Record<string, number>
  family?: string | null
}
export interface RotationArm {
  id: string; name: string; label: string; sizing_mode: string; state: string; reason: string; reason_label: string
  positions: Record<string, RotationPosition>; signal: RotationSignal; plan: RotationPlan
  net: number; realized: number; return_pct: number; drawdown_pct: number; completed_trades: number; wins: number
  fees: number; slippage: number; average_entry_risk: number | null; fill_count: number; mark_stale: boolean
  curve: RotationPoint[]; fills: RotationFill[]; marked_attribution?: Record<string, number>
  chart_fills?: RotationFill[]
  orders: RotationOrder[]; trades: RotationTrade[]; trade: RotationTrade | null
}
export interface RotationMarket {
  code: string; family: string; quote_at: number; received_at: number; multiplier: number
  margin?: number | null; available_amount?: number; new_capacity?: number; capacity_at?: number
  bids: { price: number; quantity: number }[]; asks: { price: number; quantity: number }[]
}
export interface RotationRun {
  session_phase?: 'ready' | 'running'
  model?: string
  strategy_rules?: { version?: string; revision?: string; lookback?: number; entry_style?: string; max_cost_fraction_of_risk?: number; entry_z?: number; failed_breakout_bars?: number; max_trade_loss?: number; max_trade_profit?: number; session_loss?: number | null; channel_closes?: number; target_closes?: number; confirm_seconds?: number; confirm_observations?: number; max_chase_fraction?: number | null; cooldown_seconds?: number }
  entry_policy?: { version: string; effective_at: number; previous_model: string; current_model: string; prior_pnl_and_deadline_preserved: boolean; open_trades_keep_frozen_exits: boolean }
  execution_policy?: { version: string; allowed_codes: string[]; effective_at?: number }
  market_families?: Record<'a' | 'b', { label: string; codes: string[] }>
  id: string; enabled: boolean; started_at: number; end_at: number; updated_at: number; ended_at?: number
  stop_requested: boolean; settings: { cash: number; trade_risk_fraction: number; session_loss_fraction: number; min_strength?: number; min_positive_fraction?: number; max_cost_fraction_of_risk?: number }
  execution: { duration_seconds: number; warmup_seconds: number; trail_start_r: number; trail_distance_r: number; flatten_before_end?: number }; warmup_valid_ms: number
  feed_ok: boolean; feed_error: string | null; capacity_error?: string | null; entry_lock: boolean
  arms: RotationArm[]; markets: RotationMarket[]; bars: { time: number; a: number; b: number }[]
  market_curve: ({ elapsed: number; fresh: boolean } & Record<string, number | boolean>)[]
  events: { kind: string; elapsed_seconds: number; arm: string | null; reason?: string }[]
}
export interface RotationResponse {
  run: RotationRun | null; server_now: number
  mock?: { environment: string; state: string; note: string; orders: number; fills: number;
    trade_at?: number | null; can_manual_close?: boolean; close_requested?: boolean;
    last_exit_reason?: string; last_trade_net?: number;
    recovery_attempts?: number; recoveries?: number; retry_in_seconds?: number;
    net: number; return_pct: number; completed_trades: number; quote_age_seconds?: number; target?: number; risk?: number; drawdown_pct?: number; estimated_round_trip_cost?: number;
    positions: { code: string; quantity: number; average: number; state: string }[] } | null
}
export async function rotationRequest(action = '', body?: object): Promise<RotationResponse> {
  const response = await fetch(`/api/strategy/rotation${action}`, body ? {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  } : undefined)
  if (!response.ok) {
    const value = await response.json().catch(() => ({}))
    throw new Error(typeof value.detail === 'string' ? value.detail : `요청 실패 (${response.status})`)
  }
  return response.json()
}
