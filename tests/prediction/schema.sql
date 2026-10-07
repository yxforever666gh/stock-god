-- Disposable current schema fixture; no user data or historical migration execution.
CREATE TABLE `email_send_logs` (`id` integer PRIMARY KEY AUTOINCREMENT,`created_at` datetime,`updated_at` datetime,`deleted_at` datetime,`send_type` text,`triggered_at` datetime,`status` text,`recipients` text,`subject` text,`error_message` text,`report_stock_code` text,`report_stock_name` text,`report_created_at` datetime,`attachment_names` text,`attachment_count` integer,`attachment_bytes` integer,`extra_summary` text);
CREATE TABLE `research2_account_capital_events` (`id` integer PRIMARY KEY AUTOINCREMENT,`event_id` text NOT NULL,`slot` text NOT NULL,`event_type` text NOT NULL,`amount` real NOT NULL,`external` numeric NOT NULL,`source` text NOT NULL,`effective_at` datetime NOT NULL,`trading_date` text NOT NULL,`created_at` datetime);
CREATE TABLE `research2_account_daily_valuations` (`id` integer PRIMARY KEY AUTOINCREMENT,`valuation_id` text NOT NULL,`slot` text NOT NULL,`trading_date` text NOT NULL,`valued_at` datetime NOT NULL,`cash` real NOT NULL,`position_value` real NOT NULL,`net_asset_value` real NOT NULL,`neutral_funding` real NOT NULL,`daily_return` real,`data_status` text NOT NULL,`source_status_json` text NOT NULL DEFAULT "[]",`failure_reason` text,`created_at` datetime,`updated_at` datetime);
CREATE TABLE `research2_account_ledger_snapshots` (`id` integer PRIMARY KEY AUTOINCREMENT,`snapshot_id` text NOT NULL,`slot` text NOT NULL,`valued_at` datetime NOT NULL,`trading_date` text NOT NULL,`snapshot_type` text NOT NULL,`cash` real NOT NULL,`position_value` real NOT NULL,`net_asset_value` real NOT NULL,`cumulative_external_capital` real NOT NULL,`net_internal_transfer` real NOT NULL,`net_profit` real NOT NULL,`cumulative_capital_return` real NOT NULL,`valuation_basis` text NOT NULL,`created_at` datetime);
CREATE TABLE `research2_account_snapshots` (`slot` text NOT NULL DEFAULT "09:50",`id` integer PRIMARY KEY AUTOINCREMENT,`snapshot_id` text NOT NULL,`valued_at` datetime NOT NULL,`trading_date` text NOT NULL,`snapshot_type` text NOT NULL,`cash` real,`position_value` real,`net_asset_value` real,`net_profit` real,`return_rate` real,`created_at` datetime);
CREATE TABLE `research2_accounts` (`slot` text NOT NULL DEFAULT "09:50",`baseline_at` datetime,`baseline_net_asset_value` real,`seed_cash` real,`archived_at` datetime,`id` integer PRIMARY KEY AUTOINCREMENT,`initial_cash` real NOT NULL,`cash` real NOT NULL,`created_at` datetime,`updated_at` datetime);
CREATE TABLE `research2_allocation_replays` (`id` integer PRIMARY KEY AUTOINCREMENT,`replay_id` text NOT NULL,`policy_version` text NOT NULL,`plan_hash` text NOT NULL,`status` text NOT NULL,`candidate_count` integer NOT NULL,`buy_count` integer NOT NULL,`sell_count` integer NOT NULL,`missing_buy_count` integer NOT NULL,`missing_sell_count` integer NOT NULL,`summary_json` text NOT NULL DEFAULT "{}",`started_at` datetime NOT NULL,`completed_at` datetime,`created_at` datetime,`updated_at` datetime);
CREATE TABLE `research2_analysis_runs` (`scheduled_slot` text NOT NULL DEFAULT "09:50",`slot` text,`published` numeric NOT NULL DEFAULT false,`archive_reason` text,`persisted_at` datetime,`id` integer PRIMARY KEY AUTOINCREMENT,`run_id` text NOT NULL,`trading_date` text NOT NULL,`attempt_no` integer NOT NULL DEFAULT 1,`chain_id` text,`parent_run_id` text,`trigger_source` text NOT NULL DEFAULT "legacy-unversioned",`requested_slots` integer NOT NULL DEFAULT 0,`primary_count` integer NOT NULL DEFAULT 0,`standby_count` integer NOT NULL DEFAULT 0,`scheduled_for` datetime NOT NULL,`started_at` datetime NOT NULL,`evidence_window_start_at` datetime,`evidence_cutoff_at` datetime,`evidence_coverage_pct` real,`degraded` numeric,`generated_at` datetime,`status` text NOT NULL,`provider_name` text,`model_name` text,`report_markdown` text,`source_status_json` text NOT NULL DEFAULT "[]",`model_attempt_log_json` text NOT NULL DEFAULT "[]",`strategy_version` text,`evidence_profile_version` text,`evidence_set_id` text,`failure_reason` text,`recommendation_count` integer,`on_time` numeric,`created_at` datetime,`updated_at` datetime);
CREATE TABLE `research2_email_deliveries` (`id` integer PRIMARY KEY AUTOINCREMENT,`analysis_run_id` text NOT NULL,`status` text NOT NULL,`attempt_count` integer,`next_attempt_at` datetime,`sent_at` datetime,`recipients` text NOT NULL,`sender` text NOT NULL,`subject` text NOT NULL,`body` text NOT NULL,`message_id` text NOT NULL,`last_error` text,`created_at` datetime,`updated_at` datetime);
CREATE TABLE `research2_execution_chains` (`slot` text NOT NULL DEFAULT "09:50",`winner_run_id` text,`sell_completed_at` datetime,`allocation_base_cash` real,`allocation_policy` text NOT NULL DEFAULT "legacy_recorded",`id` integer PRIMARY KEY AUTOINCREMENT,`chain_id` text NOT NULL,`trading_date` text NOT NULL,`scheduled_for` datetime NOT NULL,`status` text NOT NULL,`target_slots` integer NOT NULL DEFAULT 3,`filled_slots` integer NOT NULL DEFAULT 0,`root_run_id` text,`latest_run_id` text,`stop_reason` text,`started_at` datetime NOT NULL,`completed_at` datetime,`created_at` datetime,`updated_at` datetime);
CREATE TABLE `research2_recommendations` (`slot` text NOT NULL DEFAULT "09:50",`legacy_slot_exception` numeric,`baseline_value` real,`period_pn_l` real,`id` integer PRIMARY KEY AUTOINCREMENT,`recommendation_id` text NOT NULL,`analysis_run_id` text NOT NULL,`selection_role` text NOT NULL DEFAULT "legacy-unversioned",`selection_rank` integer NOT NULL DEFAULT 0,`replaces_recommendation_id` text,`promotion_reason` text,`stock_code` text NOT NULL,`stock_name` text NOT NULL,`signal_at` datetime NOT NULL,`market_score` real,`sector_score` real,`stock_score` real,`catalyst_score` real,`risk_deduction` real,`final_score` real,`reference_price` real,`buy_lower` real,`buy_upper` real,`estimated_lot_cost` real,`summary` text,`quant_data` text,`fresh_catalyst` text,`old_background` text,`main_risk` text,`cancel_conditions` text,`source_refs` text,`status` text NOT NULL,`late` numeric,`target_buy_at` datetime NOT NULL,`execution_failure_code` text,`execution_quote_price` real,`execution_quote_at` datetime,`execution_limit_price` real,`execution_limit_distance_pct` real,`buy_at` datetime,`buy_market_price` real,`buy_price` real,`quantity` integer,`buy_fees` real,`current_price` real,`current_price_at` datetime,`target_sell_at` datetime,`sell_at` datetime,`sell_market_price` real,`sell_price` real,`sell_fees` real,`net_pn_l` real,`net_yield_rate` real,`hit_five_before_sell` numeric,`hit_limit_up_full_day` numeric,`hit_minus_three` numeric,`metrics_finalized` numeric,`buy_day_limit_outcome` text,`buy_day_limit_status` text NOT NULL DEFAULT "pending",`buy_day_limit_evaluated_at` datetime,`buy_day_limit_attempt_count` integer NOT NULL DEFAULT 0,`buy_day_limit_source_json` text NOT NULL DEFAULT "[]",`buy_day_limit_failure_reason` text,`historical_replay_id` text,`historical_sell_blocked` numeric NOT NULL DEFAULT false,`failure_reason` text,`created_at` datetime,`updated_at` datetime);
CREATE TABLE `research2_trades` (`slot` text NOT NULL DEFAULT "09:50",`quote_at` datetime,`price_stale` numeric,`id` integer PRIMARY KEY AUTOINCREMENT,`trade_id` text NOT NULL,`recommendation_id` text NOT NULL,`side` text NOT NULL,`traded_at` datetime NOT NULL,`market_price` real,`execution_price` real,`quantity` integer,`commission` real,`stamp_duty` real,`transfer_fee` real,`slippage_amount` real,`net_cash_flow` real,`price_source` text,`execution_mode` text,`created_at` datetime);
CREATE TABLE research_audit_payloads (
  payload_id TEXT NOT NULL PRIMARY KEY CHECK (length(trim(payload_id)) > 0),
  owner_type TEXT NOT NULL CHECK (owner_type IN ('research1', 'research2', 'replay')),
  owner_id TEXT NOT NULL CHECK (length(trim(owner_id)) > 0),
  prompt_version_id TEXT CHECK (prompt_version_id IS NULL OR length(trim(prompt_version_id)) > 0),
  phase TEXT NOT NULL CHECK (length(trim(phase)) > 0),
  call_sequence INTEGER NOT NULL CHECK (call_sequence >= 1),
  attempt INTEGER NOT NULL CHECK (attempt >= 1),
  provider_name TEXT NOT NULL CHECK (length(trim(provider_name)) > 0),
  model_name TEXT NOT NULL CHECK (length(trim(model_name)) > 0),
  model_parameters_json TEXT NOT NULL DEFAULT '{}',
  cutoff_at DATETIME,
  final_prompt_codec TEXT NOT NULL DEFAULT 'gzip' CHECK (final_prompt_codec = 'gzip'),
  final_prompt_blob BLOB NOT NULL CHECK (typeof(final_prompt_blob) = 'blob' AND length(final_prompt_blob) > 0),
  final_prompt_sha256 CHAR(64) NOT NULL CHECK (length(final_prompt_sha256) = 64 AND final_prompt_sha256 NOT GLOB '*[^0-9A-Fa-f]*'),
  evidence_codec TEXT NOT NULL DEFAULT 'gzip' CHECK (evidence_codec = 'gzip'),
  evidence_blob BLOB NOT NULL CHECK (typeof(evidence_blob) = 'blob' AND length(evidence_blob) > 0),
  evidence_sha256 CHAR(64) NOT NULL CHECK (length(evidence_sha256) = 64 AND evidence_sha256 NOT GLOB '*[^0-9A-Fa-f]*'),
  tools_json TEXT NOT NULL DEFAULT '[]',
  raw_response_codec TEXT CHECK (raw_response_codec IS NULL OR raw_response_codec = 'gzip'),
  raw_response_blob BLOB,
  raw_response_sha256 CHAR(64),
  repaired_response_codec TEXT CHECK (repaired_response_codec IS NULL OR repaired_response_codec = 'gzip'),
  repaired_response_blob BLOB,
  repaired_response_sha256 CHAR(64),
  repair_log_codec TEXT CHECK (repair_log_codec IS NULL OR repair_log_codec = 'gzip'),
  repair_log_blob BLOB,
  repair_log_sha256 CHAR(64),
  redaction_manifest_json TEXT NOT NULL DEFAULT '{}',
  created_at DATETIME NOT NULL,
  UNIQUE (owner_type, owner_id, call_sequence, attempt),
  CHECK ((raw_response_codec IS NULL AND (raw_response_blob IS NULL OR length(raw_response_blob) = 0) AND raw_response_sha256 IS NULL) OR
         (raw_response_codec = 'gzip' AND typeof(raw_response_blob) = 'blob' AND length(raw_response_blob) > 0 AND length(raw_response_sha256) = 64 AND raw_response_sha256 NOT GLOB '*[^0-9A-Fa-f]*')),
  CHECK ((repaired_response_codec IS NULL AND (repaired_response_blob IS NULL OR length(repaired_response_blob) = 0) AND repaired_response_sha256 IS NULL) OR
         (repaired_response_codec = 'gzip' AND typeof(repaired_response_blob) = 'blob' AND length(repaired_response_blob) > 0 AND length(repaired_response_sha256) = 64 AND repaired_response_sha256 NOT GLOB '*[^0-9A-Fa-f]*')),
  CHECK ((repair_log_codec IS NULL AND (repair_log_blob IS NULL OR length(repair_log_blob) = 0) AND repair_log_sha256 IS NULL) OR
         (repair_log_codec = 'gzip' AND typeof(repair_log_blob) = 'blob' AND length(repair_log_blob) > 0 AND length(repair_log_sha256) = 64 AND repair_log_sha256 NOT GLOB '*[^0-9A-Fa-f]*'))
);
CREATE TABLE research_audit_prompt_versions (
  prompt_version_id TEXT NOT NULL PRIMARY KEY CHECK (length(trim(prompt_version_id)) > 0),
  research_scope TEXT NOT NULL CHECK (research_scope IN ('research1', 'research2')),
  phase TEXT NOT NULL CHECK (length(trim(phase)) > 0),
  version TEXT NOT NULL CHECK (length(trim(version)) > 0),
  template_codec TEXT NOT NULL DEFAULT 'gzip' CHECK (template_codec = 'gzip'),
  template_blob BLOB NOT NULL CHECK (typeof(template_blob) = 'blob' AND length(template_blob) > 0),
  template_sha256 CHAR(64) NOT NULL CHECK (length(template_sha256) = 64 AND template_sha256 NOT GLOB '*[^0-9A-Fa-f]*'),
  created_at DATETIME NOT NULL,
  UNIQUE (research_scope, phase, version)
);
CREATE TABLE research_audit_run_states (
  owner_type TEXT NOT NULL CHECK (owner_type IN ('research1', 'research2', 'replay')),
  owner_id TEXT NOT NULL CHECK (length(trim(owner_id)) > 0),
  status TEXT NOT NULL CHECK (status IN ('capturing', 'complete', 'failed', 'legacy_unavailable')),
  payload_count INTEGER NOT NULL DEFAULT 0 CHECK (payload_count >= 0),
  last_error TEXT,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  UNIQUE (owner_type, owner_id)
);
CREATE TABLE research_evidence_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  evidence_item_id TEXT NOT NULL,
  evidence_set_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_name TEXT NOT NULL,
  source_ref TEXT,
  category TEXT NOT NULL,
  entity_type TEXT,
  entity_id TEXT,
  event_at DATETIME,
  available_at DATETIME,
  collected_at DATETIME NOT NULL,
  status TEXT NOT NULL,
  summary TEXT,
  payload BLOB NOT NULL DEFAULT X'',
  payload_encoding TEXT NOT NULL DEFAULT 'identity',
  content_hash TEXT NOT NULL,
  error_message TEXT,
  created_at DATETIME NOT NULL
);
CREATE TABLE research_evidence_sets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  evidence_set_id TEXT NOT NULL,
  owner_type TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  cutoff_at DATETIME NOT NULL,
  collector_version TEXT NOT NULL,
  evidence_profile_version TEXT NOT NULL,
  status TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  frozen_at DATETIME,
  created_at DATETIME NOT NULL
);
CREATE TABLE `research_settings` (`center` text,`config_json` text NOT NULL,`revision` integer NOT NULL,PRIMARY KEY (`center`),CONSTRAINT `research_revision` CHECK (revision > 0),CONSTRAINT `research_center` CHECK (center IN ('research1','research2')));
CREATE TRIGGER identity_research_audit_run_states_update
BEFORE UPDATE ON research_audit_run_states
WHEN NEW.owner_type IS NOT OLD.owner_type
  OR NEW.owner_id IS NOT OLD.owner_id
  OR NEW.created_at IS NOT OLD.created_at
  OR (OLD.status != 'capturing' AND NEW.status IS NOT OLD.status)
  OR (OLD.status = 'capturing' AND NEW.status NOT IN ('capturing', 'complete', 'failed'))
  OR NEW.payload_count < OLD.payload_count
  OR (OLD.status != 'capturing' AND NEW.payload_count IS NOT OLD.payload_count)
BEGIN
  SELECT RAISE(ABORT, 'research audit run state identity and terminal state are immutable');
END;
CREATE INDEX `idx_email_send_logs_deleted_at` ON `email_send_logs`(`deleted_at`);
CREATE INDEX `idx_email_send_logs_report_created_at` ON `email_send_logs`(`report_created_at`);
CREATE INDEX `idx_email_send_logs_report_stock_code` ON `email_send_logs`(`report_stock_code`);
CREATE INDEX `idx_email_send_logs_send_type` ON `email_send_logs`(`send_type`);
CREATE INDEX `idx_email_send_logs_status` ON `email_send_logs`(`status`);
CREATE INDEX `idx_email_send_logs_triggered_at` ON `email_send_logs`(`triggered_at`);
CREATE UNIQUE INDEX `idx_research2_account_capital_events_event_id` ON `research2_account_capital_events`(`event_id`);
CREATE INDEX `idx_research2_account_capital_events_event_type` ON `research2_account_capital_events`(`event_type`);
CREATE INDEX `idx_research2_account_capital_events_trading_date` ON `research2_account_capital_events`(`trading_date`);
CREATE INDEX `idx_research2_account_daily_valuations_data_status` ON `research2_account_daily_valuations`(`data_status`);
CREATE UNIQUE INDEX `idx_research2_account_daily_valuations_valuation_id` ON `research2_account_daily_valuations`(`valuation_id`);
CREATE INDEX `idx_research2_account_daily_valuations_valued_at` ON `research2_account_daily_valuations`(`valued_at`);
CREATE UNIQUE INDEX `idx_research2_account_ledger_snapshots_snapshot_id` ON `research2_account_ledger_snapshots`(`snapshot_id`);
CREATE INDEX `idx_research2_account_ledger_snapshots_snapshot_type` ON `research2_account_ledger_snapshots`(`snapshot_type`);
CREATE INDEX `idx_research2_account_ledger_snapshots_trading_date` ON `research2_account_ledger_snapshots`(`trading_date`);
CREATE INDEX `idx_research2_account_snapshots_slot` ON `research2_account_snapshots`(`slot`);
CREATE UNIQUE INDEX `idx_research2_account_snapshots_snapshot_id` ON `research2_account_snapshots`(`snapshot_id`);
CREATE INDEX `idx_research2_account_snapshots_snapshot_type` ON `research2_account_snapshots`(`snapshot_type`);
CREATE INDEX `idx_research2_account_snapshots_trading_date` ON `research2_account_snapshots`(`trading_date`);
CREATE INDEX `idx_research2_account_snapshots_valued_at` ON `research2_account_snapshots`(`valued_at`);
CREATE UNIQUE INDEX `idx_research2_accounts_slot` ON `research2_accounts`(`slot`);
CREATE INDEX `idx_research2_allocation_replays_completed_at` ON `research2_allocation_replays`(`completed_at`);
CREATE UNIQUE INDEX `idx_research2_allocation_replays_plan_hash` ON `research2_allocation_replays`(`plan_hash`);
CREATE INDEX `idx_research2_allocation_replays_policy_version` ON `research2_allocation_replays`(`policy_version`);
CREATE UNIQUE INDEX `idx_research2_allocation_replays_replay_id` ON `research2_allocation_replays`(`replay_id`);
CREATE INDEX `idx_research2_allocation_replays_status` ON `research2_allocation_replays`(`status`);
CREATE INDEX `idx_research2_analysis_runs_chain_id` ON `research2_analysis_runs`(`chain_id`);
CREATE INDEX `idx_research2_analysis_runs_evidence_set_id` ON `research2_analysis_runs`(`evidence_set_id`);
CREATE INDEX `idx_research2_analysis_runs_evidence_window_start_at` ON `research2_analysis_runs`(`evidence_window_start_at`);
CREATE INDEX `idx_research2_analysis_runs_generated_at` ON `research2_analysis_runs`(`generated_at`);
CREATE INDEX `idx_research2_analysis_runs_parent_run_id` ON `research2_analysis_runs`(`parent_run_id`);
CREATE UNIQUE INDEX `idx_research2_analysis_runs_run_id` ON `research2_analysis_runs`(`run_id`);
CREATE INDEX `idx_research2_analysis_runs_scheduled_for` ON `research2_analysis_runs`(`scheduled_for`);
CREATE INDEX `idx_research2_analysis_runs_slot` ON `research2_analysis_runs`(`slot`);
CREATE INDEX `idx_research2_analysis_runs_started_at` ON `research2_analysis_runs`(`started_at`);
CREATE INDEX `idx_research2_analysis_runs_status` ON `research2_analysis_runs`(`status`);
CREATE INDEX `idx_research2_analysis_runs_trigger_source` ON `research2_analysis_runs`(`trigger_source`);
CREATE INDEX `idx_research2_capital_events_slot_time` ON `research2_account_capital_events`(`slot`,`effective_at`);
CREATE UNIQUE INDEX `idx_research2_daily_valuation_slot_date` ON `research2_account_daily_valuations`(`slot`,`trading_date`);
CREATE UNIQUE INDEX `idx_research2_email_deliveries_analysis_run_id` ON `research2_email_deliveries`(`analysis_run_id`);
CREATE INDEX `idx_research2_email_deliveries_next_attempt_at` ON `research2_email_deliveries`(`next_attempt_at`);
CREATE INDEX `idx_research2_email_deliveries_sent_at` ON `research2_email_deliveries`(`sent_at`);
CREATE INDEX `idx_research2_email_deliveries_status` ON `research2_email_deliveries`(`status`);
CREATE INDEX `idx_research2_execution_chains_allocation_policy` ON `research2_execution_chains`(`allocation_policy`);
CREATE UNIQUE INDEX `idx_research2_execution_chains_chain_id` ON `research2_execution_chains`(`chain_id`);
CREATE INDEX `idx_research2_execution_chains_completed_at` ON `research2_execution_chains`(`completed_at`);
CREATE INDEX `idx_research2_execution_chains_latest_run_id` ON `research2_execution_chains`(`latest_run_id`);
CREATE INDEX `idx_research2_execution_chains_root_run_id` ON `research2_execution_chains`(`root_run_id`);
CREATE INDEX `idx_research2_execution_chains_status` ON `research2_execution_chains`(`status`);
CREATE UNIQUE INDEX `idx_research2_execution_chains_trading_date` ON `research2_execution_chains`(`slot`,`trading_date`);
CREATE INDEX `idx_research2_ledger_snapshots_slot_time` ON `research2_account_ledger_snapshots`(`slot`,`valued_at`);
CREATE INDEX `idx_research2_recommendations_analysis_run_id` ON `research2_recommendations`(`analysis_run_id`);
CREATE INDEX `idx_research2_recommendations_buy_at` ON `research2_recommendations`(`buy_at`);
CREATE INDEX `idx_research2_recommendations_buy_day_limit_evaluated_at` ON `research2_recommendations`(`buy_day_limit_evaluated_at`);
CREATE INDEX `idx_research2_recommendations_buy_day_limit_outcome` ON `research2_recommendations`(`buy_day_limit_outcome`);
CREATE INDEX `idx_research2_recommendations_buy_day_limit_status` ON `research2_recommendations`(`buy_day_limit_status`);
CREATE INDEX idx_research2_recommendations_current_price_at
ON research2_recommendations(current_price_at);
CREATE INDEX `idx_research2_recommendations_execution_failure_code` ON `research2_recommendations`(`execution_failure_code`);
CREATE INDEX `idx_research2_recommendations_execution_quote_at` ON `research2_recommendations`(`execution_quote_at`);
CREATE INDEX `idx_research2_recommendations_final_score` ON `research2_recommendations`(`final_score`);
CREATE INDEX `idx_research2_recommendations_historical_replay_id` ON `research2_recommendations`(`historical_replay_id`);
CREATE INDEX `idx_research2_recommendations_historical_sell_blocked` ON `research2_recommendations`(`historical_sell_blocked`);
CREATE UNIQUE INDEX `idx_research2_recommendations_recommendation_id` ON `research2_recommendations`(`recommendation_id`);
CREATE INDEX `idx_research2_recommendations_replaces_recommendation_id` ON `research2_recommendations`(`replaces_recommendation_id`);
CREATE INDEX `idx_research2_recommendations_role_rank` ON `research2_recommendations`(`selection_role`,`selection_rank`);
CREATE INDEX `idx_research2_recommendations_sell_at` ON `research2_recommendations`(`sell_at`);
CREATE INDEX `idx_research2_recommendations_signal_at` ON `research2_recommendations`(`signal_at`);
CREATE INDEX `idx_research2_recommendations_slot` ON `research2_recommendations`(`slot`);
CREATE INDEX `idx_research2_recommendations_status` ON `research2_recommendations`(`status`);
CREATE INDEX `idx_research2_recommendations_stock_code` ON `research2_recommendations`(`stock_code`);
CREATE INDEX `idx_research2_recommendations_target_buy_at` ON `research2_recommendations`(`target_buy_at`);
CREATE INDEX `idx_research2_recommendations_target_sell_at` ON `research2_recommendations`(`target_sell_at`);
CREATE UNIQUE INDEX `idx_research2_runs_date_attempt` ON `research2_analysis_runs`(`trading_date`,`scheduled_slot`,`attempt_no`);
CREATE INDEX `idx_research2_trades_recommendation_id` ON `research2_trades`(`recommendation_id`);
CREATE INDEX `idx_research2_trades_side` ON `research2_trades`(`side`);
CREATE INDEX `idx_research2_trades_slot` ON `research2_trades`(`slot`);
CREATE UNIQUE INDEX `idx_research2_trades_trade_id` ON `research2_trades`(`trade_id`);
CREATE INDEX `idx_research2_trades_traded_at` ON `research2_trades`(`traded_at`);
CREATE UNIQUE INDEX idx_research_audit_payloads_owner_call_attempt ON research_audit_payloads(owner_type, owner_id, call_sequence, attempt);
CREATE INDEX idx_research_audit_payloads_owner_phase_created ON research_audit_payloads(owner_type, owner_id, phase, created_at);
CREATE INDEX idx_research_audit_payloads_prompt_version ON research_audit_payloads(prompt_version_id);
CREATE INDEX idx_research_audit_prompt_versions_scope_phase_created ON research_audit_prompt_versions(research_scope, phase, created_at);
CREATE INDEX idx_research_audit_run_states_status_updated ON research_audit_run_states(status, updated_at);
CREATE INDEX idx_research_evidence_items_available ON research_evidence_items(evidence_set_id, available_at);
CREATE INDEX idx_research_evidence_items_category ON research_evidence_items(category);
CREATE INDEX idx_research_evidence_items_collected ON research_evidence_items(collected_at);
CREATE INDEX idx_research_evidence_items_entity ON research_evidence_items(entity_type, entity_id);
CREATE UNIQUE INDEX idx_research_evidence_items_public_id ON research_evidence_items(evidence_item_id);
CREATE UNIQUE INDEX idx_research_evidence_items_set_source ON research_evidence_items(evidence_set_id, source_id);
CREATE INDEX idx_research_evidence_sets_cutoff ON research_evidence_sets(cutoff_at);
CREATE INDEX idx_research_evidence_sets_owner ON research_evidence_sets(owner_type, owner_id);
CREATE UNIQUE INDEX idx_research_evidence_sets_public_id ON research_evidence_sets(evidence_set_id);
CREATE TRIGGER immutable_research_audit_payloads_delete
BEFORE DELETE ON research_audit_payloads
BEGIN
  SELECT RAISE(ABORT, 'research audit payload is immutable');
END;
CREATE TRIGGER immutable_research_audit_payloads_update
BEFORE UPDATE ON research_audit_payloads
BEGIN
  SELECT RAISE(ABORT, 'research audit payload is immutable');
END;
CREATE TRIGGER immutable_research_audit_prompt_versions_delete
BEFORE DELETE ON research_audit_prompt_versions
BEGIN
  SELECT RAISE(ABORT, 'research audit prompt version is immutable');
END;
CREATE TRIGGER immutable_research_audit_prompt_versions_update
BEFORE UPDATE ON research_audit_prompt_versions
BEGIN
  SELECT RAISE(ABORT, 'research audit prompt version is immutable');
END;
CREATE TRIGGER immutable_research_audit_run_states_delete
BEFORE DELETE ON research_audit_run_states
BEGIN
  SELECT RAISE(ABORT, 'research audit run state identity is immutable');
END;

CREATE TABLE research2_base43_samples (sample_id TEXT PRIMARY KEY, trade_date TEXT NOT NULL, code TEXT NOT NULL, feature_json TEXT NOT NULL, roi REAL, mature_date TEXT, label_status TEXT NOT NULL DEFAULT 'pending', evidence_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX idx_research2_base43_samples_maturity ON research2_base43_samples(label_status,mature_date);
CREATE TABLE research2_base43_models (prediction_date TEXT PRIMARY KEY, trained_at TEXT NOT NULL, sample_digest TEXT NOT NULL, model_blob BLOB NOT NULL, metadata_json TEXT NOT NULL);
CREATE TABLE research2_base43_daily_tasks (trading_date TEXT NOT NULL, task_type TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT, payload_json TEXT NOT NULL DEFAULT '{}', error TEXT, PRIMARY KEY(trading_date,task_type));

ALTER TABLE research2_recommendations ADD COLUMN allocation_base_cash REAL;
ALTER TABLE research2_recommendations ADD COLUMN allocation_policy TEXT NOT NULL DEFAULT 'legacy_recorded';
