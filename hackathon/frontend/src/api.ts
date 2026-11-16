// Types mirror the Python simulator's JSON (farm_sim.FarmSimulator.fleet_state)
// relayed unchanged by the Spring Boot backend.

export type Pos = [number, number];

export interface Tractor {
  id: string;
  status: 'idle' | 'traveling' | 'working' | 'to_refuel' | 'refueling' | 'broken' | 'stranded' | 'blocked' | string;
  position: Pos;
  implements: string[];
  fuel_liters: number;
  fuel_pct: number;
  tank_liters: number;
  fuel_reserve_liters: number;
  job_id: string | null;
  job_progress_pct: number;
  route: Pos[];
  waiting_ticks: number;
  repair_at_tick: number | null;
}

export interface FieldInfo { id: string; name: string; position: Pos; acres: number }

export interface Job {
  id: string;
  field_id: string;
  job_type: string;
  required_implement: string;
  acres: number;
  completed_acres: number;
  remaining_acres: number;
  progress_pct: number;
  priority: number;
  deadline_tick: number;
  status: 'pending' | 'assigned' | 'in_progress' | 'interrupted' | 'completed' | string;
  assigned_tractor_id: string | null;
  completed_tick: number | null;
}

export interface Metrics {
  travel_cells: number;
  fuel_used_liters: number;
  idle_tractor_ticks: number;
  work_tractor_ticks: number;
  breakdowns: number;
  dispatches: number;
  replans: number;
  completed_jobs: number;
  on_time_jobs: number;
  late_completed_jobs: number;
  unfinished_jobs: number;
  acres_completed: number;
  total_job_acres: number;
  completion_rate_pct: number;
}

export interface SimEvent { tick: number; type: string; message: string; details: Record<string, unknown> }

export interface ScheduledEvent { tick: number; name: string; applied_tick?: number; [k: string]: unknown }

export interface Decision {
  tick: number;
  trigger: string;
  explanation: string;
  source: string;
  assignments: Array<{ tractor_id: string; job_id: string }>;
  refuels: Array<{ tractor_id: string; reason: string }>;
}

export interface Comparison {
  comparison_id: string;
  comparable: boolean;
  reasons: string[];
  baseline_policy: string;
  coordinator_policy: string;
  baseline_metrics: Partial<Metrics>;
  observed_metrics: Partial<Metrics>;
  deltas?: Partial<Record<keyof Metrics, number>>;
  horizon_ticks: { baseline: number | null; coordinator: number | null };
  note: string;
}

export interface CrewLogEntry { tick: number; task: string; answer: string; changes: string[]; steps: number }

export interface FleetState {
  version: number;
  tick: number;
  time_minutes: number;
  tick_minutes: number;
  grid: { width: number; height: number; blocked_cells: Pos[]; base: Pos };
  tractors: Tractor[];
  fields: FieldInfo[];
  jobs: Job[];
  metrics: Metrics;
  recent_events: SimEvent[];
  scenario_name: string;
  policy: string;
  ticks_requested: number;
  done: boolean;
  assessing: boolean;
  crew_running?: boolean;
  crew_log?: CrewLogEntry[];
  scheduled_events: { applied: ScheduledEvent[]; pending: ScheduledEvent[] };
  manual_events: ScheduledEvent[];
  manual_dispatches: number;
  decisions: Decision[];
  memory_path: string;
  learning_path: string;
  last_comparison: (Comparison & { baseline_score?: number; observed_score?: number }) | null;
  scenario: ScenarioMeta;
  conditions: Conditions;
  skill: SkillRef | null;
  timeline: TimelineSample[];
}

export interface ScenarioMeta {
  id: string;
  title: string;
  summary: string;
  region: string;
  tags: string[];
  horizon: number;
  seed?: number;
  realism: string[];
  sources: string[];
  machines?: Record<string, Record<string, string>>;
}

export interface ScenarioInfo extends ScenarioMeta {
  tick_minutes: number;
  fields: number;
  tractors: number;
  jobs: number;
  acres: number;
  events: number;
  weather: Array<{ tick: number; wind_mph: number; rain_mm_hr: number }>;
}

export interface Conditions {
  enabled: boolean;
  clock?: string;
  day?: number;
  hour?: number;
  daylight?: boolean;
  wind_mph?: number;
  rain_mm_hr?: number;
  blocked_implements?: Record<string, string>;
  soils?: Record<string, { soil: string; wet_until: number | null }>;
  job_blocks?: Record<string, string>;
  forecast?: Array<{ tick: number; wind_mph: number; rain_mm_hr: number; daylight: boolean }>;
  weather_halts?: number;
  rain_damaged_acres?: number;
  yard_moves?: number;
}

export interface SkillRef {
  id: string;
  name: string;
  status: string;
  why: string;
  params: Record<string, number>;
}

export interface TimelineSample {
  tick: number;
  hour: number | null;
  acres: number;
  jobs: number;
  on_time: number;
  idle: number;
  travel: number;
  fuel: number;
  wind: number | null;
  rain: number | null;
  daylight: boolean;
  tractors: Record<string, [string, string | null]>;
}

export interface RunSummary {
  run_id: string;
  scenario_id: string;
  title: string;
  tags: string[];
  seed: number;
  policy: string;
  skill_id: string | null;
  skill: SkillRef | null;
  params: Record<string, number> | null;
  horizon: number;
  tick_minutes: number;
  final_tick: number;
  metrics: Metrics & { weather_halts: number; rain_damaged_acres: number; yard_moves: number };
  score: number;
  tractors: Record<string, { implements: string[]; jobs_completed: number; acres_completed: number; breakdowns: number; halts: number }>;
  halts: Array<{ tick: number; tractor_id: string; job_id: string; reason: string }>;
  decisions: number;
  duration_s: number;
}

export interface RunDetail extends RunSummary {
  timeline: TimelineSample[];
  jobs: Job[];
  decision_log: Array<{ tick: number; source: string; trigger: string; explanation: string }>;
  map: { grid: { width: number; height: number; blocked_cells: Pos[] }; base: Pos; fields: FieldInfo[] };
}

export interface JobStatus<T = unknown> {
  id: string;
  kind: 'lab' | 'train';
  label: string;
  status: 'running' | 'done' | 'error';
  progress: number;
  total: number;
  message: string;
  error: string | null;
  result?: T | null;
}

export interface Skill {
  id: string;
  name: string;
  description: string;
  params: Record<string, number>;
  parent: string | null;
  version: number;
  scenario_id: string | null;
  contexts: string[];
  status: 'default' | 'candidate' | 'proven' | 'rejected' | string;
  created_at: string;
  updated_at: string;
  evidence: Record<string, { train_n?: number; train_delta?: number; wins?: number; holdout_n?: number; holdout_delta?: number; vs_default?: number }>;
  history: Array<{ at: string; event: string }>;
}

export interface CurvePoint {
  at: string;
  round: number;
  scenario_id: string;
  champion: string;
  agent: number;
  optimizer: number;
  fcfs: number;
  outcome: string;
}

export interface Episode {
  id: string;
  at: string;
  role: string;
  scenario_id: string;
  title: string;
  seed: number;
  policy: string;
  skill_id: string | null;
  score: number;
  metrics: Record<string, number>;
  jobs_total: number;
}

export interface LearningSummary {
  root: string;
  skills: Skill[];
  skill_counts: Record<string, number>;
  facts: {
    scenarios: Record<string, { title: string; runs: number; by_policy: Record<string, { n: number; mean_score: number; best_score: number; mean_jobs: number; mean_acres: number; mean_idle: number }> }>;
    machines: Record<string, { scenario_id: string; tractor_id: string; implements: string[]; n: number; mean_acres: number; mean_jobs: number; breakdowns: number; halts: number }>;
    conditions: Record<string, { n: number; halt_reasons: Record<string, number>; mean_halts: number; mean_rain_damage: number }>;
    updated_at?: string;
  };
  curve: CurvePoint[];
  episode_count: number;
  insights: Array<MemoryRecord & { key: string; observations?: number; updated_at?: string; scenario_id?: string }>;
}

export interface MemoryRecord {
  id: string;
  status: string;
  created_at: string;
  scenario: string;
  tags: { tractors: string[]; event_types: string[]; operations: string[] };
  lesson?: string;
  comparable?: boolean | null;
  deltas_vs_baseline?: Record<string, number> | null;
  // full records (from /memory/recent)
  interpretation?: { lesson: string; author: string; claims_improvement: boolean };
  measured?: { comparison?: { comparable: boolean; deltas: Record<string, number> | null } };
}

export interface ChatAction { name: string; payload: Record<string, unknown>; label: string }
export interface ChatReply {
  reply: string;
  intent: string;
  source: 'rules' | 'llm';
  action: ChatAction | null;
  tick: number;
  suggestions: string[];
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

// Same-origin in dev thanks to the Vite proxy; override with VITE_API_BASE for other setups.
const API_BASE: string = import.meta.env.VITE_API_BASE ?? '/api';

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    throw new ApiError(res.status, text || res.statusText);
  }
  if (!res.ok) {
    const msg = (data as { error?: string } | null)?.error ?? res.statusText;
    throw new ApiError(res.status, msg);
  }
  return data as T;
}

const post = <T>(path: string, body: unknown = {}) => call<T>(path, { method: 'POST', body: JSON.stringify(body) });

export const api = {
  health: () => call<{ backend: string; simulator: { ok: boolean; tick?: number } }>('/health'),
  state: () => call<FleetState>('/state'),
  reset: (policy: string, scenario_id?: string, seed = 0) => post<FleetState>('/reset', { policy, scenario_id, seed }),
  step: (ticks: number) => post<{ ok: boolean; tick: number; done: boolean; advanced: number }>('/step', { ticks }),
  event: (body: Record<string, unknown>) => post<{ ok: boolean; reason?: string }>('/events', body),
  estimate: (tractor_id: string, job_id: string) =>
    post<Record<string, unknown> & { feasible: boolean; reason?: string; reasons?: string[] }>('/estimate', { tractor_id, job_id }),
  dispatch: (assignments: Array<{ tractor_id: string; job_id: string }>, state_version: number, commit: boolean) =>
    post<{ valid?: boolean; accepted?: boolean; errors?: string[] }>('/dispatch', { assignments, state_version, commit }),
  compare: () => post<Comparison>('/compare'),
  searchMemory: (q: Record<string, string>) => {
    const qs = new URLSearchParams(Object.entries(q).filter(([, v]) => v.trim())).toString();
    return call<{ notice: string; count: number; records: MemoryRecord[] }>(`/memory${qs ? `?${qs}` : ''}`);
  },
  recentMemory: (limit = 10) => call<{ path: string; count: number; records: MemoryRecord[] }>(`/memory/recent?limit=${limit}`),
  saveMemory: (body: { lesson: string; comparison_id: string; claims_improvement: boolean; tractor_ids?: string; operations?: string }) =>
    post<{ saved: boolean; id?: string; error?: string; comparable?: boolean }>('/memory', body),
  crewRun: (task?: string) => post<JobStatus>('/crew/run', { task: task || undefined }),
  chat: (message: string) => post<ChatReply>('/chat', { message }),
  assess: (comparison_id?: string) => post<{ ok: boolean; answer: string }>('/assess', { comparison_id }),
  scenarios: () => call<{ scenarios: ScenarioInfo[]; policies: string[] }>('/scenarios'),
  labRun: (scenario_ids: string[], policies: string[], seeds: number[]) =>
    post<JobStatus>('/lab/run', { scenario_ids, policies, seeds }),
  labRunDetail: (id: string) => call<RunDetail>(`/lab/runs/${encodeURIComponent(id)}`),
  job: <T,>(id: string) => call<JobStatus<T>>(`/jobs/${encodeURIComponent(id)}`),
  jobs: () => call<{ jobs: JobStatus[] }>('/jobs'),
  train: (scenario_ids: string[], rounds: number) => post<JobStatus>('/learn/train', { scenario_ids, rounds }),
  learning: () => call<LearningSummary>('/learning'),
  episodes: (q: { scenario_id?: string; policy?: string; q?: string; limit?: number }) => {
    const qs = new URLSearchParams(Object.entries(q).filter(([, v]) => v !== undefined && v !== '').map(([k, v]) => [k, String(v)])).toString();
    return call<{ episodes: Episode[] }>(`/learning/episodes${qs ? `?${qs}` : ''}`);
  },
  skillMd: (id: string) => call<{ id: string; markdown: string }>(`/learning/skills/${encodeURIComponent(id)}`),
};
