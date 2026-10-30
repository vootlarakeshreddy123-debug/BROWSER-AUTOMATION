export type TaskStatus =
  | "created"
  | "running"
  | "success"
  | "failed"
  | "cancelled"
  | "timeout"
  | "awaiting_confirmation"
  | "needs_input";

export type EventType =
  | "TASK_START" | "BROWSER_READY" | "ROUTER" | "OBSERVE" | "AI_PLAN" | "PROVIDER_STATUS"
  | "ACTION_START" | "ACTION_RESULT" | "PROGRESS" | "VERIFICATION" | "RECOVERY"
  | "GOAL_PLAN" | "GOAL_START" | "GOAL_COMPLETE" | "GOAL_FAILED" | "GOAL_BLOCKED"
  | "NEW_PAGE_DETECTED" | "PAGE_ADOPTED" | "ACTIVE_PAGE_CHANGED"
  | "UNDERSTANDING" | "CONFIRMATION_REQUESTED" | "INPUT_REQUESTED"
  | "TASK_SUCCESS" | "TASK_FAILURE" | "TASK_TIMEOUT";

export type Outcome = "success" | "partial" | "blocked" | "failed" | "cancelled" | "awaiting_user";

export type GoalStatus = "PENDING" | "IN_PROGRESS" | "COMPLETED" | "BLOCKED" | "FAILED";

export interface GoalInfo {
  id: number;
  kind: string;
  text: string;
  label: string;
  status: GoalStatus;
}

/** One structured backend event (agent/events.py). Only recorded fields are present. */
export interface TaskEvent {
  seq: number;
  type: EventType;
  ts: number;
  elapsed_ms: number;
  step?: number;
  provider?: string;
  state?: string;
  failure?: string;
  action?: string;
  target?: string;
  url?: string;
  title?: string;
  duration_ms?: number;
  result?: string;
  error?: string;
  reason?: string;
  route?: string;
  phase?: string;
  classification?: string;
  claimed_done?: boolean;
  actions?: number;
  count?: number;
  level?: number;
  strategy?: string;
  streak?: number;
  timeout_s?: number;
  status?: string;
  message?: string;
  emergency?: boolean;
  goal?: number;
  goal_kind?: string;
  goals?: GoalInfo[];
  total?: number;
  index?: number;
  label?: string;
  verdict?: string;
  page_id?: number;
  page_count?: number;
  [key: string]: unknown;
}

/** Statistics computed by the backend from recorded events only (null = not measured). */
export interface TaskStats {
  elapsed_ms: number;
  ai_calls: number;
  gemini_calls: number;
  ollama_calls: number;
  providers: Record<string, { calls: number; ok: number; failed: number; total_ms: number }>;
  ai_total_ms: number;
  avg_ai_latency_ms: number | null;
  browser_actions: number;
  browser_actions_ok: number;
  browser_total_ms: number;
  avg_browser_latency_ms: number | null;
  recoveries: number;
  verifications: number;
  observations: number;
  progress: Record<string, number>;
  false_done_claims: number;
  final_status: string | null;
  provider_used?: string | null;
  providers_used?: string[];
  provider_selection?: string | null;
}

/** What a paused task is waiting for (agent/safety.py ConfirmationRequest / understanding.py Clarification). */
export interface PendingRequest {
  type: "confirmation" | "clarification";
  question: string;
  options?: string[];
  kind?: string;          // send | publish | purchase | delete | submit
  description?: string;
  target?: string;
  field?: string;         // recipient | body | subject | title ...
  goal_id?: number;
}

export interface UnderstandingInfo {
  intent?: string;
  objective?: string;
  risk?: string;
  confidence?: number;
  source?: string;
  requires_confirmation?: boolean;
  subgoals?: { kind: string; text: string; expected?: string }[];
  missing?: { field: string; question: string; options?: string[] }[];
  [key: string]: unknown;
}

export interface Task {
  id: string;
  instruction: string;
  status: TaskStatus;
  created_at: number;
  finished_at: number | null;
  duration_ms: number | null;
  plan_source: "fast_path" | "ai" | "fallback" | "workflow" | null;
  actions_count: number;
  retries: number;
  final_url: string | null;
  final_title: string | null;
  screenshot_base64: string | null;
  message: string | null;
  result: string | null;
  error: string | null;
  provider?: string | null;        // what the user selected (auto | gemini | ...)
  provider_used?: string | null;   // the provider that actually answered for this task
  outcome?: Outcome | null;        // evidence-based: success | partial | blocked | failed | cancelled | awaiting_user
  verification_status?: VerificationStatus | null;   // the backend's single verdict; only VERIFIED_COMPLETE means done
  contract_state?: VerificationStatus | null;
  contract?: TaskContractView | null;
  persistence?: PersistenceInfo | null;
  pending?: PendingRequest | null;
  understanding?: UnderstandingInfo | null;
  events?: TaskEvent[];
  stats?: TaskStats | Record<string, never>;
}

export interface WsEvent {
  type: "status" | "result" | "event";
  event?: TaskEvent;
  events?: TaskEvent[];
  stats?: TaskStats;
  status?: TaskStatus;
  final_url?: string | null;
  final_title?: string | null;
  screenshot_base64?: string | null;
  message?: string | null;
  result?: string | null;
  error?: string | null;
  duration_ms?: number | null;
  plan_source?: string | null;
  retries?: number;
  actions_count?: number;
  pending?: PendingRequest | null;
  understanding?: UnderstandingInfo | null;
  provider_used?: string | null;
  outcome?: Outcome | null;
  /** The backend's single verification verdict (identical in REST, WebSocket, history and the database). */
  verification_status?: VerificationStatus | null;
  contract_state?: VerificationStatus | null;
  contract?: TaskContractView | null;
  persistence?: PersistenceInfo | null;
}

export type VerificationStatus = "PENDING" | "RUNNING" | "ACTION_SUCCEEDED" | "VERIFIED_COMPLETE" | "BLOCKED" | "FAILED" | "CANCELLED";

export interface RequirementView {
  id: number; kind: string; text: string; state: VerificationStatus; blocker?: string | null; expected?: string; verification?: string;
}
export interface TaskContractView { state: VerificationStatus; requirements: RequirementView[]; provider?: string | null }

/** Honest persistence state: "saved" only after the database confirmed every write. */
export interface PersistenceInfo { status: "disabled" | "pending" | "saved" | "failed"; detail?: string | null }

/** A row of public.tasks as returned by /api/persisted/tasks (own tasks only - enforced by Row Level Security). */
export interface PersistedTask {
  id: string; instruction: string; status: TaskStatus; provider: string | null; result_summary: string | null;
  verification_status: VerificationStatus | null; started_at: string | null; finished_at: string | null; created_at: string;
}
export interface PersistedEvent { id: string; task_id: string; event_type: string; message: string | null; details: Record<string, unknown>; seq: number | null; created_at: string }

export type ProviderName = "groq" | "gemini" | "ollama" | "openai" | "claude" | "grok";
export type ProviderSelection = "auto" | ProviderName;

/** Safe provider status from /api/providers - never contains a key. */
export interface ProviderHealthInfo {
  label?: string;
  configured: boolean;
  enabled?: boolean;
  state: "available" | "unavailable" | "cooldown" | "unconfigured";
  reason: string;
  model?: string | null;
  may_bill?: boolean;
  in_auto_order?: boolean;
  selectable?: boolean;
}

export interface ProvidersResponse {
  default_selection: string;
  selectable: string[];
  auto_order: string[];
  providers: Record<string, ProviderHealthInfo>;
  browser_execution_mode?: string;
  note?: string;
}

export export interface HealthStatus {
  status: string;
  ai_configured: boolean;
  browser_ready: boolean;
  auth_enabled?: boolean;
  auth_configured?: boolean;
  providers?: Record<string, ProviderHealthInfo>;
}

export interface BrowserState {
  ready: boolean;
  live_tab_open: boolean;
  url: string | null;
  title: string | null;
}
