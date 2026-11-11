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

// ==================== NVDA SIGNAL MONITOR TYPES ====================

export interface NvdaBanner {
  code: string;
  severity: 'standby' | 'watching' | 'armed' | 'signal' | 'danger';
  headline: string;
  subtext: string;
}

export interface NvdaPeer {
  symbol: string;
  prev_close: number;
  last: number;
  chg_pct: number;
  pm_high: number | null;
  pm_low: number | null;
  pm_vwap: number | null;
  vs_pm_vwap_pct: number | null;
  rth_high: number | null;
  rth_low: number | null;
  rth_vwap: number | null;
  pm_vol_m: number;
  rth_vol_m: number;
  last_bar_ts: string;
}

export interface NvdaGate {
  id: string;
  name: string;
  state: 'PASS' | 'WARN' | 'STANDBY' | 'ARMED' | 'FAIL' | 'CLOSED';
  current: string;
  threshold: string;
  note: string;
}

export interface NvdaSignal {
  setup: 'SETUP_A' | 'SETUP_B';
  bar_open_ts: string;
  confirmed_at: string;
  bar_epoch_ms: number;
  entry_price: number;
  flush_low?: number;
  hurdle_cleared?: number;
  hard_stop: number;
  tight_stop: number;
  target_1: number;
  target_2: number;
  soxl_at_signal: number;
  crwv_at_signal: number;
  in_primary_window?: boolean;
}

export interface NvdaChecklistItem {
  label: string;
  done: boolean;
  detail: string;
}

export interface NvdaSetupPlan {
  is_confirmed: boolean;
  entry: number;
  limit_cap: number;
  hard_stop: number;
  tight_stop: number;
  target_1: number;
  target_2: number;
  swing_note: string;
}

export interface NvdaSetupCard {
  name: string;
  window: string;
  status: string;
  signal: NvdaSignal | null;
  checklist: NvdaChecklistItem[];
  plan: NvdaSetupPlan;
}

export interface NvdaChartBar {
  ts: string;
  epoch_ms: number;
  session: 'PM' | 'RTH';
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
  rth_vwap: number | null;
  forming: boolean;
}

export interface NvdaLevels {
  prev_close: number;
  hard_stop: number;
  flush_zone_low: number;
  flush_zone_high: number;
  pm_low: number;
  gap_fill_50: number;
  s4_open_flush: number;
  rth_open: number | null;
  rth_low: number | null;
  rth_high: number | null;
  rth_vwap: number | null;
  pm_vwap: number;
  setup_b_hurdle: number;
  pm_high: number;
  target_180: number;
}

export interface NvdaEvent {
  key: string;
  ts: string;
  level: 'INFO' | 'ARMED' | 'SIGNAL' | 'DANGER';
  title: string;
  detail: string;
}

export interface NvdaMonitorState {
  ok: boolean;
  status?: string;
  message?: string;
  last_error?: string;
  updated_at_et?: string;
  session_date?: string;
  prev_session_date?: string;
  market_phase?: 'PREMARKET' | 'RTH' | 'AFTER_HOURS' | 'HISTORICAL';
  sec_to_bell?: number;
  sec_to_next_5m_close?: number;
  banner?: NvdaBanner;
  levels?: NvdaLevels;
  nvda?: NvdaPeer;
  gates?: NvdaGate[];
  setup_a?: NvdaSetupCard;
  setup_b?: NvdaSetupCard;
  peers?: NvdaPeer[];
  chart_bars?: NvdaChartBar[];
  signals?: NvdaSignal[];
  events?: NvdaEvent[];
}

// ==================== WHOLE MARKET MULTI-STRATEGY SCANNER & AUDIT TYPES ====================

export interface WholeMarketGate {
  name: string;
  pass: boolean;
  observed: string;
  required: string;
}

export interface WholeMarketCandidate {
  id: string;
  sleeve_id: 'S1' | 'S2' | 'S3' | 'S4' | 'S5';
  sleeve_short_name: string;
  direction: 'LONG' | 'SHORT';
  status:
    | 'SIGNAL_ACTIVE'
    | 'MORNING_COHORT_LIVE'
    | 'FORMING_IN_WINDOW'
    | 'PRE_3PM_WATCH'
    | 'NEAR_TRIGGER_WATCH';
  sort_score: number;
  cohort_rank: number;
  in_top_cohort: boolean;
  symbol: string;
  name: string;
  guild: string;
  sector: string;
  industry: string;
  adtv20_usd: number;
  prev_close: number;
  open: number;
  high: number;
  low: number;
  last: number;
  day_chg_pct: number;
  gap_pct: number;
  ibs: number;
  pace_rvol: number;
  sigma20_pct: number;
  beta_spy: number;
  primary_label: string;
  secondary_label: string;
  metric_value: number;
  ref_price: number;
  stop_price: number;
  shares: number;
  notional_usd: number;
  hedge_side: 'BUY' | 'SHORT' | 'NONE';
  spy_hedge_shares: number;
  rt_cost_bps: number;
  live_mtm_bps: number | null;
  live_mtm_usd: number | null;
  catalyst_warning?: boolean;
  stopped_out?: boolean;
  entry_bell: string;
  exit_bell: string;
  gates: WholeMarketGate[];
  copy_ticket: string;
}

export interface WholeMarketSleeveSummary {
  id: 'S1' | 'S2' | 'S3' | 'S4' | 'S5';
  name: string;
  short_name: string;
  weight: number;
  max_positions: number;
  direction: 'LONG' | 'SHORT';
  hedge_mode: string;
  entry_bell: string;
  exit_bell: string;
  window_kind: string;
  audited_sharpe: number;
  audited_t: number;
  audited_net_bps: number;
  sleeve_capital_usd: number;
  qualifying_count: number;
  cohort_selected_count: number;
  near_count: number;
  today_cohort_mtm_bps: number | null;
}

export interface WholeMarketScanPayload {
  ok: boolean;
  fetched_at_et: string;
  feed_mode: string;
  scan_latency_ms: number;
  as_of_prior_session: string;
  total_audited_universe: number;
  live_scanner_universe_count: number;
  quotes_received: number;
  book_capital_usd: number;
  market_window: {
    phase: string;
    badge: string;
    active_bells: string;
    guidance: string;
  };
  p1500_anchor_locked: boolean;
  spy: {
    symbol: string;
    prev_close: number;
    open: number;
    high: number;
    low: number;
    last: number;
    p1500: number | null;
    day_chg_pct: number;
    intraday_chg_pct: number;
  };
  sleeves: WholeMarketSleeveSummary[];
  candidates: WholeMarketCandidate[];
  near_candidates: WholeMarketCandidate[];
  total_active_or_forming: number;
}

export interface WholeMarketAuditStrategy {
  id: 'S1' | 'S2' | 'S3' | 'S4' | 'S5';
  name: string;
  short_name: string;
  weight_pct: number;
  direction: string;
  clock_window: string;
  holding_period: string;
  universe_rule: string;
  signal_rule: string;
  hedge_rule: string;
  structural_why: string;
  trades: number;
  active_days: number;
  gross_bps: number;
  cost_bps: number;
  net_bps: number;
  win_rate_pct: number;
  train_t: number;
  oos_t: number;
  full_t: number;
  sharpe: number;
  ex_top3_tickers_sharpe: number;
  ex_top3_days_sharpe: number;
  friction_2x_sharpe: number;
  placebo_note: string;
  recent_trades: Array<{
    date: string;
    symbol: string;
    entry_px: number;
    metric: number;
    gross_bps: number;
    net_bps: number;
  }>;
}

export interface WholeMarketAuditSummary {
  generated_at: string;
  overview: {
    total_specs_tested: number;
    surviving_specs: number;
    rejected_specs: number;
    daily_universe_tickers: number;
    daily_stock_days: number;
    intraday_5m_60d_tickers: number;
    intraday_5m_1y_tickers: number;
    deflated_sharpe_ratio: number;
    deflated_sharpe_pvalue: string;
    mean_pairwise_corr: number;
  };
  portfolio_windows: Array<{
    label: string;
    days: number;
    compounded_return_pct: number;
    sharpe: number;
    daily_t_stat: number;
    max_drawdown_pct: number;
    winning_months_pct: number;
  }>;
  strategies: WholeMarketAuditStrategy[];
  correlation_matrix: Record<string, Record<string, number>>;
  monthly_returns: Array<{
    month: string;
    port: number;
    s1: number;
    s2: number;
    s3: number;
    s4: number;
    s5: number;
  }>;
  equity_curve: Array<{
    date: string;
    port: number;
    s1: number;
    s2: number;
    s3: number;
    s4: number;
    s5: number;
    dd_pct: number;
  }>;
  sls_case_study: {
    symbol: string;
    company: string;
    cik: string;
    filing: string;
    ocf_ttm_usd: number;
    days_audited_5m: number;
    dip_150bps_freq_pct: number;
    reclaim_open_win_rate_pct: number;
    capped_target_net_bps: number;
    hold_to_close_net_bps: number;
    reg_nms_tick_drag_bps: number;
    broad_habit_train_gross_bps: number;
    broad_habit_oos_net_bps: number;
    verdict: string;
  };
  graveyard: Array<{
    family: string;
    specs_tested: number;
    gross_bps: number;
    net_bps: number;
    oos_t: number;
    why_failed: string;
  }>;
}
