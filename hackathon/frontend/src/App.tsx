import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { api, ApiError } from './api';
import type { Comparison, FleetState, Job, MemoryRecord, Pos, ScenarioInfo, Tractor } from './api';
import { ChartFrame, LegendItem, LineChart } from './charts';
import { POLICY_COLOR, POLICY_LABEL, nightBands } from './chartUtils';
import LabView from './LabView';
import MemoryView from './MemoryView';
import './App.css';

const POLICIES = ['agent', 'optimizer', 'fcfs', 'priority-nearest'] as const;
const TABS = [
  { id: 'live', label: 'Live operations' },
  { id: 'lab', label: 'Simulation lab' },
  { id: 'memory', label: 'Agent memory' },
] as const;
type Tab = (typeof TABS)[number]['id'];

const readTab = (): Tab => {
  const fromUrl = new URLSearchParams(window.location.search).get('tab');
  if (TABS.some((x) => x.id === fromUrl)) return fromUrl as Tab;
  try {
    const t = window.localStorage.getItem('farm.tab');
    return TABS.some((x) => x.id === t) ? (t as Tab) : 'live';
  } catch {
    return 'live';
  }
};
const TRACTOR_COLORS = ['#38bdf8', '#f59e0b', '#a78bfa', '#f472b6', '#34d399'];
const EVENT_TYPES = ['breakdown', 'repair', 'job_interrupted', 'route_blocked', 'route_opened', 'route_replanned',
  'priority_changed', 'fuel_empty', 'deadline_missed', 'job_completed', 'plan_rejected'];

const STATUS_CLASS: Record<string, string> = {
  idle: 'ok', working: 'busy', traveling: 'busy', to_refuel: 'warn', refueling: 'warn',
  broken: 'bad', stranded: 'bad', blocked: 'warn',
  pending: 'muted', assigned: 'busy', in_progress: 'busy', interrupted: 'warn', completed: 'ok',
};

const clock = (tick: number, tickMinutes: number) => {
  const m = 6 * 60 + tick * tickMinutes; // field day starts 06:00
  return `${String(Math.floor(m / 60) % 24).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
};

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

type RunFn = (label: string, fn: () => Promise<unknown>, okText?: (r: unknown) => string | null) => Promise<unknown>;

export default function App() {
  const [state, setState] = useState<FleetState | null>(null);
  const [online, setOnline] = useState<'unknown' | 'ok' | 'sim-down' | 'backend-down'>('unknown');
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);
  const [tab, setTabState] = useState<Tab>(readTab);
  const [scenarios, setScenarios] = useState<ScenarioInfo[]>([]);
  const urlParams = useMemo(() => new URLSearchParams(window.location.search), []);
  const [playing, setPlaying] = useState(() => urlParams.get('play') === '1');
  const [minutes, setMinutes] = useState(() => Number(urlParams.get('pace')) || 5);
  const [loop, setLoop] = useState(true);
  const [skipNights, setSkipNights] = useState(true);
  const busyRef = useRef(false);
  const playRef = useRef(false);
  useEffect(() => { playRef.current = playing; }, [playing]);
  const tabRef = useRef(tab);
  useEffect(() => { tabRef.current = tab; }, [tab]);

  const setTab = (t: Tab) => {
    setTabState(t);
    try { window.localStorage.setItem('farm.tab', t); } catch { /* storage unavailable */ }
  };

  const flash = useCallback((kind: 'ok' | 'err', text: string) => {
    setToast({ kind, text });
    window.setTimeout(() => setToast((t) => (t?.text === text ? null : t)), 5000);
  }, []);

  useEffect(() => {
    if (online === 'ok' && !scenarios.length) api.scenarios().then((r) => setScenarios(r.scenarios)).catch(() => undefined);
  }, [online, scenarios.length]);

  const refresh = useCallback(async () => {
    try {
      setState(await api.state());
      setOnline('ok');
    } catch (e) {
      setOnline(e instanceof ApiError && e.status === 502 ? 'sim-down' : 'backend-down');
    }
  }, []);

  useEffect(() => {
    const first = window.setTimeout(refresh, 0);
    const id = window.setInterval(() => {
      if (!busyRef.current && !playRef.current && tabRef.current === 'live') refresh();
    }, 2000);
    return () => { window.clearTimeout(first); window.clearInterval(id); };
  }, [refresh]);


  const run: RunFn = useCallback(async (label, fn, okText) => {
    busyRef.current = true;
    setBusy(true);
    try {
      const r = await fn();
      const text = okText ? okText(r) : null;
      if (text) flash('ok', text);
      return r;
    } catch (e) {
      flash('err', `${label}: ${errText(e)}`);
      return undefined;
    } finally {
      busyRef.current = false;
      setBusy(false);
      refresh();
    }
  }, [refresh, flash]);

  const step = (ticks: number) => run('Step', () => api.step(ticks));
  const runToEnd = () => run('Run', async () => {
    for (let i = 0; i < 20; i++) {
      const r = await api.step(10);
      setState(await api.state());
      if (r.done) break;
    }
  }, () => 'Workday complete');
  const reset = (policy: string, scenarioId: string, seed: number) =>
    run('Reset', () => api.reset(policy, scenarioId, seed), () => `Loaded ${scenarios.find((s) => s.id === scenarioId)?.title ?? scenarioId} · ${POLICY_LABEL[policy] ?? policy}`);
  // Real-time playback: one tick at a time, paced so a whole scenario takes `minutes`.
  const tickMs = state ? Math.max(400, (minutes * 60000) / Math.max(1, state.scenario?.horizon ?? 60)) : 1000;
  const quiet = !!state && state.conditions?.enabled && !state.conditions.daylight
    && state.tractors.every((t) => !['working', 'traveling', 'to_refuel', 'refueling'].includes(t.status));
  const delayMs = skipNights && quiet ? 150 : tickMs;
  useEffect(() => {
    if (!playing || !state || tab !== 'live') return;
    const t = window.setTimeout(async () => {
      if (busyRef.current) {
        setState((s) => (s ? { ...s } : s)); // re-arm the timer after a manual action
        return;
      }
      try {
        if (state.done) {
          if (!loop) {
            setPlaying(false);
            flash('ok', 'Run finished');
            return;
          }
          const list = scenarios.length ? scenarios : [{ id: state.scenario.id, title: state.scenario.title } as ScenarioInfo];
          const idx = list.findIndex((x) => x.id === state.scenario.id);
          const next = list[(idx + 1) % list.length];
          const seed = Math.floor(Math.random() * 6);
          await api.reset(state.policy, next.id, seed);
          flash('ok', `Now playing: ${next.title}${seed ? ` · variant ${seed}` : ''}`);
        } else {
          await api.step(1);
        }
        setState(await api.state());
      } catch (e) {
        setPlaying(false);
        flash('err', `Playback stopped: ${errText(e)}`);
      }
    }, delayMs);
    return () => window.clearTimeout(t);
  }, [playing, state, tab, delayMs, loop, scenarios, flash]);

  const togglePlay = async () => {
    if (!playing && state?.done && !loop) await run('Reset', () => api.reset(state.policy, state.scenario.id, state.scenario.seed ?? 0));
    setPlaying((p) => !p);
  };

  const sendEvent = (body: Record<string, unknown>, label: string) =>
    run(label, async () => {
      const r = await api.event(body);
      if (!r.ok) throw new Error(r.reason ?? 'rejected by simulator');
      return r;
    }, () => `${label} applied`);

  return (
    <div className="app">
      <header className="topbar">
        <div>
          <h1>AI Farm Orchestrator — Command Center</h1>
          <p className="sub">
            {state ? state.scenario_name : 'Autonomous fleet dispatch'} · simulator is the source of truth
          </p>
        </div>
        <div className="topbar-right">
          {state && tab === 'live' && (
            <div className="clock">
              <span className="clock-time">{state.conditions?.clock ?? clock(state.tick, state.tick_minutes)}</span>
              <span className="clock-tick">{state.conditions?.day ? `day ${state.conditions.day} · ` : ''}tick {state.tick}/{state.scenario?.horizon}</span>
            </div>
          )}
          <span className={`pill ${online === 'ok' ? 'ok' : online === 'unknown' ? 'muted' : 'bad'}`}>
            {online === 'ok' ? '● Online' : online === 'sim-down' ? '● Simulator offline'
              : online === 'backend-down' ? '● Backend offline' : '● Connecting'}
          </span>
        </div>
      </header>

      {(online === 'sim-down' || online === 'backend-down') && (
        <div className="banner">
          {online === 'sim-down'
            ? 'Spring Boot is up but cannot reach the Python simulator service. Start it with: .venv/bin/python api_server.py'
            : 'Cannot reach the Spring Boot backend on :8080. Start it with: ./mvnw spring-boot:run'}
        </div>
      )}

      {toast && <div className={`toast ${toast.kind}`} role="status">{toast.text}</div>}

      <nav className="tabs" role="tablist" aria-label="Views">
        {TABS.map((t) => (
          <button key={t.id} role="tab" aria-selected={tab === t.id} className={tab === t.id ? 'tab on' : 'tab'}
            onClick={() => setTab(t.id)}>{t.label}</button>
        ))}
      </nav>

      {online === 'ok' && tab === 'lab' && <LabView onFlash={flash} />}
      {online === 'ok' && tab === 'memory' && <MemoryView onFlash={flash} />}

      {state && tab === 'live' && (
        <>
          <Controls key={`${state.policy}|${state.scenario?.id}|${state.scenario?.seed}`} state={state} busy={busy || playing}
            scenarios={scenarios} onStep={step} onRunToEnd={runToEnd} onReset={reset} />
          <PlaybackBar state={state} playing={playing} minutes={minutes} loop={loop} tickMs={tickMs}
            onToggle={togglePlay} onMinutes={setMinutes} onLoop={setLoop}
            skipNights={skipNights} onSkipNights={setSkipNights} fastForward={playing && skipNights && quiet} />
          {playing && <NowDoing state={state} />}
          <ScenarioBanner state={state} />
          <MetricsRow state={state} />
          {state.conditions?.enabled && <ConditionsPanel state={state} />}
          <LiveProgress state={state} />
          <main className="layout">
            <div className="col">
              <Panel title="Field map" hint="Click an open cell to block it; click a hatched cell to reopen it.">
                <FieldMap state={state} disabled={busy} moveMs={playing ? Math.min(1500, tickMs * 0.85) : 350}
                  onToggleCell={(p, blocked) => sendEvent({ name: blocked ? 'unblock_cell' : 'block_cell', position: p },
                    blocked ? `Reopen (${p[0]},${p[1]})` : `Block (${p[0]},${p[1]})`)} />
              </Panel>
              <Panel title="Jobs">
                <JobsTable state={state} disabled={busy}
                  onPriority={(job, priority) => sendEvent({ name: 'change_priority', job_id: job.id, priority }, `${job.id} → P${priority}`)} />
              </Panel>
              <Panel title="Manual dispatch" hint="Estimate and validate against the simulator before committing.">
                <DispatchForm state={state} busy={busy} run={run} />
              </Panel>
            </div>
            <div className="col">
              <Panel title="Fleet">
                <FleetList state={state} disabled={busy} onEvent={sendEvent} />
              </Panel>
              <Panel title="Agent reasoning log">
                <DecisionLog state={state} />
              </Panel>
              <Panel title="Events">
                <EventsPanel state={state} />
              </Panel>
            </div>
          </main>
          <MemoryPanel state={state} busy={busy} run={run} />
          <footer className="foot">Lessons: <code>{state.memory_path}</code> · Learning memory: <code>{state.learning_path}</code></footer>
        </>
      )}
    </div>
  );
}

function Panel({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>{title}</h2>
        {hint && <span className="hint">{hint}</span>}
      </div>
      {children}
    </section>
  );
}

function Controls({ state, busy, scenarios, onStep, onRunToEnd, onReset }: {
  state: FleetState; busy: boolean; scenarios: ScenarioInfo[];
  onStep: (n: number) => void; onRunToEnd: () => void; onReset: (p: string, s: string, seed: number) => void;
}) {
  const [policy, setPolicy] = useState(state.policy);
  const [scenarioId, setScenarioId] = useState(state.scenario?.id ?? 'cochise-field-day');
  const [seed, setSeed] = useState(state.scenario?.seed ?? 0);
  return (
    <div className="controls">
      <div className="btn-group">
        <button disabled={busy || state.done} onClick={() => onStep(1)}>Step 1</button>
        <button disabled={busy || state.done} onClick={() => onStep(5)}>Step 5</button>
        <button disabled={busy || state.done} onClick={() => onStep(20)}>Step 20</button>
        <button className="primary" disabled={busy || state.done} onClick={onRunToEnd}>Run workday</button>
      </div>
      <span className="status-text">
        {state.done ? 'Run finished' : busy ? 'Working…' : POLICY_LABEL[state.policy] ?? state.policy}
        {state.assessing && ' · assessment running'}
      </span>
      <div className="btn-group">
        <label className="inline">
          Scenario
          <select value={scenarioId} onChange={(e) => setScenarioId(e.target.value)} disabled={busy}>
            {(scenarios.length ? scenarios : [{ id: scenarioId, title: state.scenario?.title ?? scenarioId }]).map((s) =>
              <option key={s.id} value={s.id}>{s.title}</option>)}
          </select>
        </label>
        <label className="inline">
          Seed
          <select value={seed} onChange={(e) => setSeed(Number(e.target.value))} disabled={busy}>
            {[0, 1, 2, 3, 4, 5].map((n) => <option key={n} value={n}>{n === 0 ? '0 (as written)' : n}</option>)}
          </select>
        </label>
        <label className="inline">
          Policy
          <select value={policy} onChange={(e) => setPolicy(e.target.value)} disabled={busy}>
            {POLICIES.map((p) => <option key={p} value={p}>{POLICY_LABEL[p]}</option>)}
          </select>
        </label>
        <button className="primary" disabled={busy} onClick={() => onReset(policy, scenarioId, seed)}>Load</button>
      </div>
    </div>
  );
}

function MetricsRow({ state }: { state: FleetState }) {
  const m = state.metrics;
  const c = state.conditions;
  const tiles: Array<[string, string, string]> = [
    ['Jobs done', `${m.completed_jobs}/${m.completed_jobs + m.unfinished_jobs}`, `${m.on_time_jobs} on time`],
    ['Acres', `${Math.round(m.acres_completed)}`, `of ${Math.round(m.total_job_acres)}`],
    ['Travel', `${m.travel_cells}`, 'cells'],
    ['Idle', `${m.idle_tractor_ticks}`, 'tractor-ticks'],
    ['Fuel used', `${Math.round(m.fuel_used_liters)}`, 'litres'],
    c?.enabled
      ? ['Condition stops', `${c.weather_halts ?? 0}`, `${m.breakdowns} breakdowns · ${c.rain_damaged_acres ?? 0} ac rain-damaged`]
      : ['Breakdowns', `${m.breakdowns}`, `${m.replans} replans`],
  ];
  return (
    <div className="metrics">
      {tiles.map(([label, value, sub]) => (
        <div className="tile" key={label}>
          <span className="tile-label">{label}</span>
          <span className="tile-value">{value}</span>
          <span className="tile-sub">{sub}</span>
        </div>
      ))}
    </div>
  );
}

function FieldMap({ state, disabled, onToggleCell, moveMs = 350 }: {
  state: FleetState; disabled: boolean; onToggleCell: (p: Pos, blocked: boolean) => void; moveMs?: number;
}) {
  const { width, height, base } = state.grid;
  const C = 10;
  const sy = (y: number) => (height - 1 - y) * C; // y = 0 is the south edge
  const blocked = new Set(state.grid.blocked_cells.map((p) => `${p[0]},${p[1]}`));
  const fieldAt = new Map(state.fields.map((f) => [`${f.position[0]},${f.position[1]}`, f]));
  const colorOf = (id: string) => TRACTOR_COLORS[state.tractors.findIndex((t) => t.id === id) % TRACTOR_COLORS.length];
  const activeJobByField = new Map(state.jobs.filter((j) => j.status !== 'completed').map((j) => [j.field_id, j]));

  const coords = Array.from({ length: width * height }, (_, i) => [Math.floor(i / height), i % height] as const);
  const cells = coords.map(([x, y]) => {
      const k = `${x},${y}`;
      const isBlocked = blocked.has(k);
      const special = fieldAt.has(k) || (x === base[0] && y === base[1]);
      const clickable = !special && !disabled;
      return (
        <rect key={k} x={x * C} y={sy(y)} width={C} height={C}
          className={`cell${isBlocked ? ' blocked' : ''}${clickable ? ' clickable' : ''}`}
          onClick={() => clickable && onToggleCell([x, y], isBlocked)}>
          <title>{isBlocked ? `(${x},${y}) blocked — click to reopen` : `(${x},${y})`}</title>
        </rect>
      );
  });

  return (
    <svg className="map" viewBox={`0 0 ${width * C} ${height * C}`} role="img" aria-label="Farm grid map"
      style={{ ['--move-ms' as string]: `${Math.round(moveMs)}ms` }}>
      <defs>
        <pattern id="hatch" width="2.5" height="2.5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="2.5" className="hatch" />
        </pattern>
      </defs>
      {cells}
      <g className="base">
        <rect x={base[0] * C + 1} y={sy(base[1]) + 1} width={C - 2} height={C - 2} rx="1.5" />
        <text x={base[0] * C + C / 2} y={sy(base[1]) + C / 2 + 1.2}>BASE</text>
      </g>
      {state.fields.map((f) => {
        const job = activeJobByField.get(f.id);
        const soil = state.conditions?.soils?.[f.id];
        const wet = soil?.wet_until != null;
        return (
          <g key={f.id} className={`field${job ? '' : ' field-done'}${wet ? ' field-wet' : ''}`}>
            <rect x={f.position[0] * C + 0.8} y={sy(f.position[1]) + 0.8} width={C - 1.6} height={C - 1.6} rx="1.5" />
            <text x={f.position[0] * C + C / 2} y={sy(f.position[1]) + 4.2}>{f.id}</text>
            <text className="field-sub" x={f.position[0] * C + C / 2} y={sy(f.position[1]) + 7.6}>{job ? job.id : '✓'}</text>
            <title>{`${f.name} (${f.acres} ac${soil ? `, ${soil.soil}` : ''})${wet ? ` — too wet until t${soil?.wet_until}` : ''}${job ? ` — ${job.id} ${job.job_type}, ${job.progress_pct}%` : ' — all jobs done'}`}</title>
          </g>
        );
      })}
      {state.tractors.map((t) => t.route.length > 0 && (
        <polyline key={`r-${t.id}`} className="route" stroke={colorOf(t.id)}
          points={[t.position, ...t.route].map((p) => `${p[0] * C + C / 2},${sy(p[1]) + C / 2}`).join(' ')} />
      ))}
      {state.tractors.map((t, i) => {
        const stacked = state.tractors.filter((o, j) => j < i && o.position[0] === t.position[0] && o.position[1] === t.position[1]).length;
        const cx = t.position[0] * C + C / 2 + stacked * 2.6;
        const cy = sy(t.position[1]) + C / 2 - stacked * 2.6;
        return (
          <g key={t.id} className={`tractor ${STATUS_CLASS[t.status] ?? ''} st-${t.status}`}
            style={{ transform: `translate(${cx}px, ${cy}px)` }}>
            {t.status === 'working' && <circle r="3" className="pulse" stroke={colorOf(t.id)} />}
            <circle r="3" fill={colorOf(t.id)} />
            <text y={1.1}>{t.id.length <= 2 ? t.id.replace('T', '') : t.id}</text>
            <title>{`${t.id}: ${t.status}${t.job_id ? ` on ${t.job_id}` : ''}, fuel ${t.fuel_pct}%`}</title>
          </g>
        );
      })}
    </svg>
  );
}

function JobsTable({ state, disabled, onPriority }: {
  state: FleetState; disabled: boolean; onPriority: (j: Job, p: number) => void;
}) {
  const fieldName = (id: string) => state.fields.find((f) => f.id === id)?.name ?? id;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr><th>Job</th><th>Field</th><th>Task</th><th>Prio</th><th>Due</th><th>Progress</th><th>Status</th></tr>
        </thead>
        <tbody>
          {state.jobs.map((j) => {
            const late = j.status !== 'completed' && state.tick > j.deadline_tick;
            return (
              <tr key={j.id}>
                <td><strong>{j.id}</strong></td>
                <td>{fieldName(j.field_id)}</td>
                <td>{j.job_type} <span className="dim">({j.required_implement})</span></td>
                <td>
                  <select value={j.priority} disabled={disabled || j.status === 'completed'}
                    onChange={(e) => onPriority(j, Number(e.target.value))} aria-label={`Priority for ${j.id}`}>
                    {[1, 2, 3, 4, 5].map((p) => <option key={p} value={p}>P{p}</option>)}
                  </select>
                </td>
                <td className={late ? 'bad-text' : ''}>t{j.deadline_tick}</td>
                <td>
                  <div className="bar" title={`${j.completed_acres}/${j.acres} ac`}>
                    <div style={{ width: `${j.progress_pct}%` }} />
                  </div>
                </td>
                <td>
                  <span className={`badge ${STATUS_CLASS[j.status] ?? ''}`}>{j.status.replace('_', ' ')}</span>
                  {j.assigned_tractor_id && <span className="dim"> {j.assigned_tractor_id}</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function FleetList({ state, disabled, onEvent }: {
  state: FleetState; disabled: boolean; onEvent: (b: Record<string, unknown>, label: string) => void;
}) {
  return (
    <div className="fleet">
      {state.tractors.map((t: Tractor, i) => {
        const fuelLow = t.fuel_liters <= t.fuel_reserve_liters * 1.5;
        return (
          <div className="tractor-card" key={t.id} style={{ borderLeftColor: TRACTOR_COLORS[i % TRACTOR_COLORS.length] }}>
            <div className="row">
              <strong>{t.id}</strong>
              <span className="dim">{t.implements.join(' · ')}</span>
              <span className={`badge ${STATUS_CLASS[t.status] ?? ''}`}>{t.status.replace('_', ' ')}</span>
            </div>
            <div className="row small">
              <span>{t.job_id ? `${t.job_id} · ${t.job_progress_pct}%` : 'no job'}</span>
              <span className="dim">pos ({t.position[0]},{t.position[1]})</span>
              {t.repair_at_tick != null && <span className="warn-text">repair t{t.repair_at_tick}</span>}
            </div>
            <div className="fuel" title={`${t.fuel_liters} / ${t.tank_liters} L (reserve ${t.fuel_reserve_liters} L)`}>
              <div className={fuelLow ? 'low' : ''} style={{ width: `${t.fuel_pct}%` }} />
              <span>{t.fuel_pct}% fuel</span>
            </div>
            <div className="row actions">
              <button disabled={disabled || t.status === 'broken'}
                onClick={() => onEvent({ name: 'breakdown', tractor_id: t.id, repair_after_ticks: 6 }, `${t.id} breakdown`)}>
                Break down
              </button>
              <button disabled={disabled || !['broken', 'stranded'].includes(t.status)}
                onClick={() => onEvent({ name: 'repair', tractor_id: t.id, add_fuel_liters: 0 }, `${t.id} repair`)}>
                Repair
              </button>
              <button disabled={disabled || t.status !== 'idle'}
                onClick={() => onEvent({ name: 'refuel', tractor_id: t.id, target_pct: 100 }, `${t.id} refuel`)}>
                Refuel
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function DispatchForm({ state, busy, run }: { state: FleetState; busy: boolean; run: RunFn }) {
  const idle = state.tractors.filter((t) => t.status === 'idle' && !t.job_id);
  const open = state.jobs.filter((j) => (j.status === 'pending' || j.status === 'interrupted') && !j.assigned_tractor_id);
  const [tractorId, setTractorId] = useState('');
  const [jobId, setJobId] = useState('');
  const [result, setResult] = useState<string | null>(null);
  const t = idle.some((x) => x.id === tractorId) ? tractorId : idle[0]?.id ?? '';
  const j = open.some((x) => x.id === jobId) ? jobId : open[0]?.id ?? '';

  const estimate = () => run('Estimate', async () => {
    const r = await api.estimate(t, j);
    const problems = r.reasons?.length ? r.reasons : r.reason ? [r.reason] : [];
    setResult(r.feasible
      ? `Feasible: ${r.travel_cells} cells, finishes ~t${r.estimated_completion_tick}, needs ${r.fuel_needed_liters} L of ${r.fuel_available_liters} L.`
      : `Not feasible: ${problems.join('; ')}`);
  });
  const submit = (commit: boolean) => run(commit ? 'Commit' : 'Validate', async () => {
    const r = await api.dispatch([{ tractor_id: t, job_id: j }], state.version, commit);
    const ok = commit ? r.accepted : r.valid;
    setResult(ok ? (commit ? `Committed ${t} → ${j}.` : `Valid at state v${state.version}.`)
      : `Rejected: ${(r.errors ?? []).join('; ')}`);
  });

  if (!idle.length || !open.length) {
    return <p className="dim">{!idle.length ? 'No idle tractors right now.' : 'No open jobs to dispatch.'}</p>;
  }
  return (
    <form className="dispatch" onSubmit={(e) => { e.preventDefault(); submit(true); }}>
      <select value={t} onChange={(e) => { setTractorId(e.target.value); setResult(null); }} aria-label="Tractor">
        {idle.map((x) => <option key={x.id} value={x.id}>{x.id} ({x.implements.join(', ')})</option>)}
      </select>
      <span className="dim">→</span>
      <select value={j} onChange={(e) => { setJobId(e.target.value); setResult(null); }} aria-label="Job">
        {open.map((x) => <option key={x.id} value={x.id}>{x.id} {x.job_type} · P{x.priority} · {x.required_implement}</option>)}
      </select>
      <button type="button" disabled={busy} onClick={estimate}>Estimate</button>
      <button type="button" disabled={busy} onClick={() => submit(false)}>Validate</button>
      <button type="submit" className="primary" disabled={busy}>Commit</button>
      {result && <p className="result">{result}</p>}
    </form>
  );
}

function DecisionLog({ state }: { state: FleetState }) {
  const items = [...state.decisions].reverse();
  if (!items.length) return <p className="dim">No decisions yet. Step the simulation to let the coordinator dispatch.</p>;
  return (
    <div className="log">
      {items.map((d) => (
        <div className={`log-item${d.tick >= state.tick - 2 ? ' fresh' : ''}`} key={`${d.tick}-${d.source}-${d.explanation.slice(0, 60)}`}>
          <div className="row">
            <span className={`badge src-${d.source}`}>{d.source}</span>
            <span className="dim">t{d.tick}</span>
          </div>
          <pre>{d.explanation}</pre>
        </div>
      ))}
    </div>
  );
}

function EventsPanel({ state }: { state: FleetState }) {
  const notable = state.recent_events
    .filter((e) => !['traffic_wait', 'refueling', 'plan_explanation'].includes(e.type))
    .slice(-14).reverse();
  return (
    <div className="events">
      <div className="chips">
        {state.scheduled_events.pending.map((e, i) => <span key={`p${i}`} className="chip">t{e.tick} {e.name}</span>)}
        {state.scheduled_events.applied.map((e, i) => <span key={`a${i}`} className="chip done">t{e.applied_tick ?? e.tick} {e.name} ✓</span>)}
        {state.manual_events.map((e, i) => <span key={`m${i}`} className="chip manual">t{e.tick} {e.name} (operator)</span>)}
      </div>
      <ul>
        {notable.map((e, i) => (
          <li key={i} className={`ev-${e.type}`}><span className="dim">t{e.tick}</span> {e.message}</li>
        ))}
      </ul>
    </div>
  );
}

const DELTA_ROWS: Array<[keyof NonNullable<Comparison['deltas']>, string, boolean]> = [
  ['completed_jobs', 'Jobs completed', true], ['on_time_jobs', 'On time', true], ['acres_completed', 'Acres', true],
  ['travel_cells', 'Travel cells', false], ['idle_tractor_ticks', 'Idle ticks', false], ['fuel_used_liters', 'Fuel (L)', false],
];

function MemoryPanel({ state, busy, run }: { state: FleetState; busy: boolean; run: RunFn }) {
  const [filters, setFilters] = useState({ tractor_id: '', event_type: '', operation: '', scenario: '' });
  const [hits, setHits] = useState<MemoryRecord[] | null>(null);
  const [recent, setRecent] = useState<MemoryRecord[]>([]);
  const [lesson, setLesson] = useState('');
  const [claims, setClaims] = useState(false);
  const [tags, setTags] = useState({ tractor_ids: '', operations: '' });
  const [assessment, setAssessment] = useState<string | null>(null);
  const cmp = state.last_comparison;

  const loadRecent = useCallback(() => api.recentMemory(8).then((r) => setRecent(r.records)).catch(() => undefined), []);
  useEffect(() => { loadRecent(); }, [loadRecent]);

  const operations = useMemo(() => [...new Set(state.jobs.flatMap((j) => [j.job_type, j.required_implement]))], [state.jobs]);

  const search = (e: FormEvent) => {
    e.preventDefault();
    run('Memory search', async () => setHits((await api.searchMemory(filters)).records));
  };
  const compare = () => run('Compare', () => api.compare(), (r) => {
    const c = r as Comparison;
    return c.comparable ? 'Comparison ready' : `Not comparable: ${c.reasons.join('; ')}`;
  });
  const save = (e: FormEvent) => {
    e.preventDefault();
    run('Save lesson', async () => {
      const r = await api.saveMemory({ lesson, comparison_id: cmp?.comparison_id ?? 'none', claims_improvement: claims, ...tags });
      if (!r.saved) throw new Error(r.error ?? 'not saved');
      setLesson('');
      setClaims(false);
      await loadRecent();
      return r;
    }, () => 'Saved as candidate lesson');
  };
  const assess = () => run('Assessment', async () => {
    const r = await api.assess(cmp?.comparison_id);
    setAssessment(r.answer);
    await loadRecent();
  }, () => 'Assessment finished');

  return (
    <section className="panel memory">
      <div className="panel-head">
        <h2>Fleet memory</h2>
        <span className="hint">Local JSONL. Lessons are <em>candidates</em> for human review, never rules.</span>
      </div>
      <div className="memory-grid">
        <div>
          <h3>1 · Compare with FCFS baseline</h3>
          <p className="dim small">
            Replays FCFS on the same scenario, scripted + operator events, and horizon ({state.ticks_requested} ticks).
          </p>
          <button disabled={busy || state.ticks_requested === 0} onClick={compare}>Run comparison</button>
          {cmp && (
            <div className="cmp">
              <div className="row">
                <span className={`badge ${cmp.comparable ? 'ok' : 'warn'}`}>{cmp.comparable ? 'comparable' : 'not comparable'}</span>
                <span className="dim small">{cmp.comparison_id}</span>
              </div>
              <table>
                <thead><tr><th>Measured</th><th>{cmp.baseline_policy}</th><th>{cmp.coordinator_policy}</th><th>Δ</th></tr></thead>
                <tbody>
                  {DELTA_ROWS.map(([k, label, higherIsBetter]) => {
                    const d = cmp.deltas?.[k];
                    const cls = d === undefined || d === 0 ? '' : (d > 0) === higherIsBetter ? 'good-text' : 'bad-text';
                    return (
                      <tr key={k}>
                        <td>{label}</td>
                        <td>{cmp.baseline_metrics[k] ?? '–'}</td>
                        <td>{cmp.observed_metrics[k] ?? '–'}</td>
                        <td className={cls}>{d === undefined ? '–' : d > 0 ? `+${d}` : d}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <p className="dim small">{cmp.note}</p>
            </div>
          )}
        </div>

        <div>
          <h3>2 · Save a candidate lesson</h3>
          <form onSubmit={save} className="stack">
            <textarea value={lesson} maxLength={300} rows={3} onChange={(e) => setLesson(e.target.value)}
              placeholder="e.g. After T2's breakdown, J2 resumed on T1 with completed acres kept." />
            <div className="row">
              <input placeholder="tractors (T1,T2)" value={tags.tractor_ids} onChange={(e) => setTags({ ...tags, tractor_ids: e.target.value })} />
              <input placeholder="operations (mow)" value={tags.operations} onChange={(e) => setTags({ ...tags, operations: e.target.value })} />
            </div>
            <label className="inline small">
              <input type="checkbox" checked={claims} disabled={!cmp?.comparable} onChange={(e) => setClaims(e.target.checked)} />
              Claims one strategy performed better{cmp?.comparable ? '' : ' (needs a comparable run)'}
            </label>
            <div className="row">
              <span className="dim small">{lesson.length}/300</span>
              <button type="submit" className="primary" disabled={busy || !lesson.trim()}>Save candidate</button>
            </div>
          </form>
          <button className="ghost" disabled={busy || !cmp} onClick={assess}
            title="Runs the read-only smolagents assessment agent (needs HF_TOKEN on the simulator service)">
            Ask AI assessment agent
          </button>
          {assessment && <pre className="assessment">{assessment}</pre>}
        </div>

        <div>
          <h3>3 · Retrieve (max 5)</h3>
          <form onSubmit={search} className="stack">
            <div className="row">
              <select value={filters.tractor_id} onChange={(e) => setFilters({ ...filters, tractor_id: e.target.value })} aria-label="Tractor filter">
                <option value="">any tractor</option>
                {state.tractors.map((t) => <option key={t.id}>{t.id}</option>)}
              </select>
              <select value={filters.event_type} onChange={(e) => setFilters({ ...filters, event_type: e.target.value })} aria-label="Event filter">
                <option value="">any event</option>
                {EVENT_TYPES.map((t) => <option key={t}>{t}</option>)}
              </select>
            </div>
            <div className="row">
              <select value={filters.operation} onChange={(e) => setFilters({ ...filters, operation: e.target.value })} aria-label="Operation filter">
                <option value="">any operation</option>
                {operations.map((o) => <option key={o}>{o}</option>)}
              </select>
              <input placeholder="scenario" value={filters.scenario} onChange={(e) => setFilters({ ...filters, scenario: e.target.value })} />
            </div>
            <button type="submit" disabled={busy || !Object.values(filters).some((v) => v.trim())}>Search</button>
          </form>
          <MemoryList records={hits ?? recent} empty={hits ? 'No matching memories.' : 'No memories saved yet.'}
            title={hits ? `${hits.length} match${hits.length === 1 ? '' : 'es'}` : 'Most recent'} />
        </div>
      </div>
    </section>
  );
}

function MemoryList({ records, empty, title }: { records: MemoryRecord[]; empty: string; title: string }) {
  return (
    <div className="mem-list">
      <span className="dim small">{title}</span>
      {records.length === 0 && <p className="dim small">{empty}</p>}
      {records.map((r) => {
        const text = r.lesson ?? r.interpretation?.lesson;
        const comparable = r.comparable ?? r.measured?.comparison?.comparable;
        return (
          <div key={r.id} className="mem">
            <div className="row">
              <span className="badge warn">{r.status}</span>
              {comparable && <span className="badge ok">comparable</span>}
              <span className="dim small">{new Date(r.created_at).toLocaleString()}</span>
            </div>
            <p>{text}</p>
            <div className="chips">
              {[...r.tags.tractors, ...r.tags.operations].slice(0, 6).map((t) => <span key={t} className="chip">{t}</span>)}
            </div>
          </div>
        );
      })}
    </div>
  );
}


function ScenarioBanner({ state }: { state: FleetState }) {
  const sc = state.scenario;
  if (!sc) return null;
  return (
    <section className="scenario-banner">
      <div>
        <strong>{sc.title}</strong>{sc.seed ? <span className="dim"> · variant {sc.seed}</span> : null}
        <span className="dim small"> · {sc.region}</span>
        <p className="small dim">{sc.summary}</p>
      </div>
      <div className="chips">
        {sc.tags?.map((t) => <span key={t} className="chip">{t}</span>)}
        {state.policy === 'agent' && state.skill && (
          <span className={`badge ${state.skill.status === 'proven' ? 'ok' : 'busy'}`} title={state.skill.why}>
            skill: {state.skill.name}
          </span>
        )}
      </div>
    </section>
  );
}

function ConditionsPanel({ state }: { state: FleetState }) {
  const c = state.conditions;
  const blocked = Object.entries(c.blocked_implements ?? {});
  const wet = Object.entries(c.soils ?? {}).filter(([, v]) => v.wet_until !== null);
  const jobBlocks = Object.entries(c.job_blocks ?? {});
  const windOk = (c.wind_mph ?? 0) >= 3 && (c.wind_mph ?? 0) <= 10;
  return (
    <section className="panel conditions">
      <div className="cond-now">
        <div className="cond-item"><span className="tile-label">Time</span><span className="tile-value">{c.clock}</span><span className="tile-sub">{c.daylight ? 'daylight' : 'night'} · day {c.day}</span></div>
        <div className="cond-item"><span className="tile-label">Wind</span><span className="tile-value">{c.wind_mph} <small>mph</small></span><span className={`tile-sub ${windOk ? 'good-text' : 'warn-text'}`}>{windOk ? 'inside 3–10 mph spray window' : 'outside spray window'}</span></div>
        <div className="cond-item"><span className="tile-label">Rain</span><span className="tile-value">{c.rain_mm_hr} <small>mm/h</small></span><span className="tile-sub">{(c.rain_mm_hr ?? 0) > 0 ? 'field work stops' : 'dry'}</span></div>
        <div className="cond-item"><span className="tile-label">Wet fields</span><span className="tile-value">{wet.length}</span><span className="tile-sub">{wet.map(([f, v]) => `${f} (${v.soil}) → t${v.wet_until}`).join(', ') || 'all workable'}</span></div>
      </div>
      <div className="cond-detail">
        {blocked.length > 0 && (
          <div className="chips">
            {blocked.map(([imp, why]) => <span key={imp} className="chip manual" title={why}>⛔ {imp}: {why}</span>)}
          </div>
        )}
        {jobBlocks.length > 0 && (
          <details>
            <summary className="small">{jobBlocks.length} job(s) waiting on conditions or dependencies</summary>
            <ul className="tight small">{jobBlocks.map(([j, why]) => <li key={j}><strong>{j}</strong>: {why}</li>)}</ul>
          </details>
        )}
      </div>
      {c.forecast && c.forecast.length > 1 && (
        <ChartFrame title="Forecast (next 8 h)" subtitle="Wind; spraying allowed between the dashed lines"
          table={<table><tbody>{c.forecast.map((f) => <tr key={f.tick}><td>t{f.tick}</td><td>{f.wind_mph} mph</td><td>{f.rain_mm_hr} mm/h</td></tr>)}</tbody></table>}>
          <LineChart x={c.forecast.map((f) => f.tick)} height={120} yLabel="mph" directLabels={false} yMin={0}
            thresholds={[{ y: 10, label: '10 mph' }, { y: 3, label: '3 mph' }]}
            bands={c.forecast.filter((f) => !f.daylight).map((f) => ({ from: f.tick, to: f.tick + 1 }))}
            series={[{ id: 'wind', label: 'Wind', color: 'var(--series-1)', values: c.forecast.map((f) => f.wind_mph) }]} />
        </ChartFrame>
      )}
    </section>
  );
}

function LiveProgress({ state }: { state: FleetState }) {
  const tl = state.timeline ?? [];
  if (tl.length < 2) return null;
  const color = POLICY_COLOR[state.policy] ?? 'var(--series-1)';
  return (
    <section className="panel">
      <ChartFrame title="Progress this run" subtitle={`Acres completed by ${POLICY_LABEL[state.policy] ?? state.policy}. Shaded = night.`}
        legend={<LegendItem color={color} label={POLICY_LABEL[state.policy] ?? state.policy} />}
        table={<table><thead><tr><th>Tick</th><th>Acres</th><th>Jobs</th><th>Idle</th></tr></thead><tbody>
          {tl.filter((_, i) => i % 4 === 0).map((s) => <tr key={s.tick}><td>t{s.tick}</td><td>{s.acres}</td><td>{s.jobs}</td><td>{s.idle}</td></tr>)}
        </tbody></table>}>
        <LineChart x={tl.map((s) => s.tick)} height={170} yLabel="acres" bands={nightBands(tl)} directLabels={false}
          series={[{ id: 'acres', label: 'Acres', color, values: tl.map((s) => s.acres) }]} />
      </ChartFrame>
    </section>
  );
}


function PlaybackBar({ state, playing, minutes, loop, tickMs, onToggle, onMinutes, onLoop, skipNights, onSkipNights, fastForward }: {
  state: FleetState; playing: boolean; minutes: number; loop: boolean; tickMs: number;
  onToggle: () => void; onMinutes: (m: number) => void; onLoop: (l: boolean) => void;
  skipNights: boolean; onSkipNights: (v: boolean) => void; fastForward: boolean;
}) {
  const horizon = state.scenario?.horizon ?? 60;
  const pct = Math.min(100, (100 * state.tick) / horizon);
  const remainingS = Math.max(0, Math.round(((horizon - state.tick) * tickMs) / 1000));
  return (
    <section className={`playback${playing ? ' on' : ''}`}>
      <button className="primary play" onClick={onToggle} aria-pressed={playing}>
        {playing ? '❚❚ Pause' : '▶ Watch in real time'}
      </button>
      <div className="play-track" aria-label="Scenario progress">
        <div className="play-fill" style={{ width: `${pct}%` }} />
      </div>
      <span className="small dim play-meta">
        tick {state.tick}/{horizon} · {(tickMs / 1000).toFixed(1)} s per tick
        {playing && (fastForward ? ' · ⏩ quiet night, fast-forwarding'
          : ` · ~${Math.floor(remainingS / 60)}:${String(remainingS % 60).padStart(2, '0')} left`)}
      </span>
      <label className="inline small">
        Pace
        <select value={minutes} onChange={(e) => onMinutes(Number(e.target.value))}>
          {[...new Set([1, 3, 5, 10, 15, minutes])].sort((x, y) => x - y).map((m) => <option key={m} value={m}>{m} min per scenario</option>)}
        </select>
      </label>
      <label className="inline small">
        <input type="checkbox" checked={loop} onChange={(e) => onLoop(e.target.checked)} /> loop through scenarios
      </label>
      <label className="inline small">
        <input type="checkbox" checked={skipNights} onChange={(e) => onSkipNights(e.target.checked)} /> skip quiet nights
      </label>
    </section>
  );
}

function NowDoing({ state }: { state: FleetState }) {
  const jobs = new Map(state.jobs.map((j) => [j.id, j]));
  const blocks = state.conditions?.job_blocks ?? {};
  const say = (t: Tractor) => {
    const j = t.job_id ? jobs.get(t.job_id) : undefined;
    switch (t.status) {
      case 'working': return `${j?.job_type ?? 'working'} on ${j?.field_id ?? '?'} · ${t.job_progress_pct}%`;
      case 'traveling': return `driving to ${j?.field_id ?? 'field'} for ${t.job_id}`;
      case 'to_refuel': return 'heading to base for fuel';
      case 'refueling': return `refuelling · ${t.fuel_pct}%`;
      case 'broken': return `broken down${t.repair_at_tick != null ? ` · repair t${t.repair_at_tick}` : ''}`;
      case 'stranded': return 'out of fuel – needs recovery';
      case 'blocked': return 'route blocked – waiting';
      default: {
        const waiting = Object.entries(blocks).find(([jid]) => state.jobs.find((x) => x.id === jid && t.implements.includes(x.required_implement)));
        return waiting ? `idle – ${waiting[0]} ${waiting[1]}` : 'idle – no feasible job';
      }
    }
  };
  return (
    <div className="now-doing" aria-live="polite">
      {state.tractors.map((t) => (
        <div key={t.id} className={`now-item st-${t.status}`}>
          <strong>{t.id}</strong> <span>{say(t)}</span>
        </div>
      ))}
    </div>
  );
}
