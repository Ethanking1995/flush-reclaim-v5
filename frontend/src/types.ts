export type CandidateCategory =
  | 'READY_FOR_REVIEW'
  | 'WATCHING'
  | 'BLOCKED'
  | 'DATA_UNAVAILABLE';

export type GateStatus = 'PASS' | 'FAIL' | 'UNKNOWN';

export interface GateEvidence {
  gate_id: string;
  gate_name: string;
  status: GateStatus;
  observed_value: string;
  required_threshold: string;
  threshold?: string;
  timestamp_et: string;
  explanation: string;
  summary?: string;
  details?: string;
}

export interface ConsistencyCheck {
  status: string;
  comparison_basis: string;
  is_independent_provider: boolean;
  provider_relationship: string;
  provenance_relation?: string;
  primary_price: number | null;
  secondary_price: number | null;
  spread_pct: number | null;
  threshold_pct: number;
  observation_skew_seconds: number | null;
  explanation: string;
}

export interface DataProvenance {
  vendor_id: string;
  endpoint_id: string;
  observation_timestamp_et: string;
  fetch_timestamp_et: string;
  age_seconds: number;
  is_delayed: boolean;
  delay_seconds: number;
  update_mode: string;
}

export interface CandidateRow {
  signal_id: string;
  signal_version: number;
  symbol: string;
  session_date: string;
  signal_state: string;
  category: CandidateCategory;
  primary_status_badge: string;
  concise_reason: string;
  signal_time_et: string | null;
  confirmed_signal_time_et?: string | null;
  signal_age_seconds: number | null;
  is_actionable: boolean;
  actionability_reason: string;
  observation_time_et: string;
  bar_open_et: string;
  bar_close_et: string;
  fetch_time_et: string;
  decision_time_et: string;
  strategy_version: string;
  last_price: number;
  session_vwap: number;
  vs_vwap_pct: number;
  morning_high: number;
  flush_low: number;
  flush_pct: number;
  volume_climax_mult: number;
  why_now_or_missing: string;
  gates: GateEvidence[];
  reference_trigger_price: number | null;
  reference_trigger_timestamp_et: string | null;
  reference_trigger_meaning: string;
  consistency_check: ConsistencyCheck;
  provenance: DataProvenance;
}

export interface ChartBar {
  time: number;
  bar_open_et: string;
  bar_close_et: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  vwap: number;
  ema12: number;
  ema20: number;
  vol_ma10_ex_open: number;
  is_completed: boolean;
}

export interface ChartMarker {
  time: number;
  bar_close_et: string;
  kind: string;
  price: number;
  label: string;
  color: string;
}

export interface ChartPriceLevel {
  level_id: string;
  kind: string;
  price: number;
  label: string;
  source: 'PROPOSED_PLAN' | 'BROKER_CONFIRMED_ORDER' | 'MANUAL_USER_REPORTED';
}

export interface SymbolChartPayload {
  symbol: string;
  bars: ChartBar[];
  markers: ChartMarker[];
  levels: ChartPriceLevel[];
  last_completed_bar_close_et: string;
  forming_bar_open_et: string | null;
}

export interface BrokerHandoffLink {
  symbol: string;
  destination_url: string;
  link_capability: string;
  mode: string;
  prefill_supported: boolean;
  prefills_ticket_natively: boolean;
  user_notice: string;
  documentation_ref: string;
  platforms_tested: string[];
}

export interface OrderPlan {
  plan_id: string;
  plan_version: number;
  signal_id: string;
  signal_version: number;
  symbol: string;
  side: string;
  strategy_version: string;
  signal_bar_close_et: string;
  created_at_et: string;
  allocation_usd: number;
  shares: number;
  leg1_shares: number;
  leg2_shares: number;
  tranche1_shares?: number;
  tranche2_shares?: number;
  entry_order_type: string;
  limit_price: number;
  stop_price: number;
  leg1_target: number;
  leg2_target: number;
  runner_stretch_target: number;
  runner_ema20_level: number | null;
  notional_usd: number;
  estimated_notional_usd?: number;
  planned_risk_usd: number;
  planned_risk_pct: number;
  estimated_fees_usd: number;
  is_manual_override: boolean;
  user_edited?: boolean;
  override_notes: string;
  masked_account_id: string;
  available_buying_power_usd: number;
  buying_power_timestamp_et: string;
  quote_freshness_seconds: number;
  is_executable: boolean;
  execution_blocked_reason: string;
  execution_blocked_reasons?: string[];
  flatten_cutoff_et: string;
  active_preview_id: string | null;
  copy_ticket_text: string;
  broker_handoff_link: BrokerHandoffLink;
}

export interface BrokerPreview {
  preview_id: string;
  plan_id: string;
  plan_version: number;
  signal_id: string;
  signal_version: number;
  account_id: string;
  masked_account_id: string;
  symbol: string;
  side: string;
  shares: number;
  leg1_shares: number;
  leg2_shares: number;
  limit_price: number;
  stop_price: number;
  leg1_target: number;
  leg2_target: number;
  environment: string;
  validation_mode: string;
  status: string;
  created_at_et: string;
  expires_at_et: string;
  expires_epoch: number;
  is_valid_for_submission: boolean;
  message: string;
  errors?: string[];
  tranche_a_payload: Record<string, unknown>;
  tranche_b_payload: Record<string, unknown>;
}

export interface PositionRecord {
  position_id: string;
  intent_id?: string | null;
  symbol: string;
  source_mode: 'LIVE_BROKER' | 'PAPER' | 'DEMO' | 'MANUAL_USER_REPORTED';
  reconciliation_status?: string;
  reconciliation_badge?: string;
  is_strategy_managed: boolean;
  filled_qty: number | null;
  remaining_qty: number | null;
  tranche1_qty?: number | null;
  tranche2_qty?: number | null;
  leg1_exited_qty: number | null;
  avg_fill_price: number;
  current_price: number | null;
  mark_timestamp_et: string;
  realized_pnl_usd: number | null;
  unrealized_pnl_usd: number | null;
  reported_approx_pnl_usd: number | null;
  working_stop_price: number | null;
  working_stop_qty: number | null;
  leg1_target_price: number | null;
  leg2_target_price: number | null;
  protection_state: string;
  runner_state: string;
  cutoff_time_et?: string;
  requires_attention: boolean;
  attention_reason: string | null;
  notes: string;
}

export interface OrderIntentRecord {
  intent_id: string;
  external_identifier: string;
  idempotency_key: string;
  plan_id: string;
  plan_version: number;
  preview_id: string;
  signal_id: string;
  symbol: string;
  shares: number;
  filled_qty?: number;
  leg1_shares: number;
  leg2_shares: number;
  limit_price: number;
  stop_price: number;
  leg1_target: number;
  leg2_target: number;
  order_state: string;
  protection_state: string;
  source_mode: string;
  created_at_et: string;
  updated_at_et: string;
  notes: string;
}

export interface EventRecord {
  event_id: string;
  timestamp_et: string;
  event_type: string;
  severity: string;
  symbol: string | null;
  source_mode: string;
  summary: string;
  details: Record<string, unknown>;
}

export interface AlertRecord {
  alert_id: string;
  dedupe_key: string;
  timestamp_et: string;
  alert_type: string;
  severity: string;
  symbol: string | null;
  message: string;
  acknowledged: boolean;
}

export interface DashboardSnapshot {
  server_time_et: string;
  session_window_label: string;
  flatten_cutoff_et: string;
  entry_window_et: string;
  operating_mode: string;
  fixture_mode: string;
  pause_new_entries: boolean;
  worker_health: {
    running: boolean;
    started_at_et: string;
    last_heartbeat_et: string;
    last_scan_et: string;
    scan_cycle_count: number;
    reconciliation_healthy: boolean;
    reconciliation_reason: string;
    last_reconciled_at_et: string;
    persistence_note: string;
  };
  spy_gate: {
    symbol: string;
    last: number;
    vwap: number;
    vs_vwap_pct: number;
    drawdown_from_high_pct: number;
    max_intraday_drop_pct: number;
    gate_status: GateStatus;
    status_label: string;
    observation_timestamp_et: string;
    age_seconds: number;
  };
  broker_status: {
    connected: boolean;
    environment: string;
    masked_account_id: string;
    trading_enabled: boolean;
    account_state_current: boolean;
    status_label: string;
    setup_instructions: string;
  };
  candidates: CandidateRow[];
  grouped_candidates: Record<CandidateCategory, CandidateRow[]>;
  order_plans_by_symbol: Record<string, OrderPlan>;
  broker_previews_by_id: Record<string, BrokerPreview>;
  positions: PositionRecord[];
  order_intents: OrderIntentRecord[];
  events: EventRecord[];
  alerts: AlertRecord[];
  settings: Record<string, unknown>;
}
