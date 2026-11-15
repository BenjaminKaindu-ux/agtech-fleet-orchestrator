import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from './api';
import type { JobStatus, RunDetail, RunSummary, ScenarioInfo } from './api';
import { ActivityLegend, ChartFrame, Gantt, GroupedBars, LegendItem, LineChart } from './charts';
import { POLICY_COLOR, POLICY_LABEL, nightBands } from './chartUtils';
import { useJob } from './useJob';

const ALL_POLICIES = ['agent', 'fcfs', 'optimizer', 'priority-nearest'];
const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0);
const r1 = (v: number) => Math.round(v * 10) / 10;

export function Progress({ job }: { job: JobStatus | null }) {
  if (!job) return null;
  const pct = Math.round((100 * job.progress) / Math.max(1, job.total));
  return (
    <div className={`progress ${job.status}`} role="status">
      <div className="progress-bar"><div style={{ width: `${job.status === 'done' ? 100 : pct}%` }} /></div>
      <span className="small">{job.status === 'error' ? `Error: ${job.error}` : job.status === 'done' ? `Done · ${job.label}` : `${job.message} (${job.progress}/${job.total})`}</span>
    </div>
  );
}

export default function LabView({ onFlash }: { onFlash: (kind: 'ok' | 'err', text: string) => void }) {
  const [scenarios, setScenarios] = useState<ScenarioInfo[]>([]);
  const [picked, setPicked] = useState<string[]>([]);
  const [policies, setPolicies] = useState<string[]>(['agent', 'fcfs', 'optimizer']);
  const [nSeeds, setNSeeds] = useState(2);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [focus, setFocus] = useState<{ scenario: string; seed: number } | null>(null);
  const [focusPolicy, setFocusPolicy] = useState('agent');
  const [details, setDetails] = useState<Record<string, RunDetail>>({});
  const [openInfo, setOpenInfo] = useState<string | null>(null);

  useEffect(() => {
    api.scenarios().then((r) => {
      setScenarios(r.scenarios);
      setPicked(r.scenarios.filter((s) => s.id !== 'cochise-field-day').map((s) => s.id).slice(0, 2));
    }).catch((e) => onFlash('err', `Scenarios: ${e.message}`));
  }, [onFlash]);

  const onDone = useCallback((j: JobStatus<{ runs: RunSummary[] }>) => {
    if (j.status === 'done' && j.result) {
      setRuns(j.result.runs);
      setDetails({});
      const first = j.result.runs[0];
      if (first) setFocus({ scenario: first.scenario_id, seed: first.seed });
      onFlash('ok', `${j.result.runs.length} simulations finished`);
    } else if (j.status === 'error') onFlash('err', j.error ?? 'Lab run failed');
  }, [onFlash]);
  const [job, setJob] = useJob<{ runs: RunSummary[] }>(onDone);

  const start = async () => {
    try {
      setJob(await api.labRun(picked, policies, Array.from({ length: nSeeds }, (_, i) => i)) as JobStatus<{ runs: RunSummary[] }>);
    } catch (e) {
      onFlash('err', `Run: ${(e as Error).message}`);
    }
  };

  // Load full timelines for every policy run of the focused scenario+seed.
  const focusRuns = useMemo(() => runs.filter((r) => focus && r.scenario_id === focus.scenario && r.seed === focus.seed), [runs, focus]);
  useEffect(() => {
    focusRuns.filter((r) => !details[r.run_id]).forEach((r) => {
      api.labRunDetail(r.run_id).then((d) => setDetails((prev) => ({ ...prev, [r.run_id]: d }))).catch(() => undefined);
    });
  }, [focusRuns, details]);

  const usedPolicies = ALL_POLICIES.filter((p) => runs.some((r) => r.policy === p));
  const usedScenarios = scenarios.filter((s) => runs.some((r) => r.scenario_id === s.id));
  const agg = (sid: string, pol: string, f: (r: RunSummary) => number) => {
    const xs = runs.filter((r) => r.scenario_id === sid && r.policy === pol).map(f);
    return xs.length ? r1(mean(xs)) : undefined;
  };

  const loaded = focusRuns.map((r) => details[r.run_id]).filter(Boolean) as RunDetail[];
  const ref = loaded[0];
  const fmtTick = (tick: number) => {
    if (!ref) return `t${tick}`;
    const h = ref.timeline.find((s) => s.tick === tick)?.hour;
    return h == null ? `t${tick}` : `${String(Math.floor(h)).padStart(2, '0')}:${String(Math.round((h % 1) * 60)).padStart(2, '0')}`;
  };
  const focusDetail = loaded.find((d) => d.policy === focusPolicy) ?? loaded[0];

  return (
    <div className="lab">
      <section className="panel">
        <div className="panel-head">
          <h2>Scenario library</h2>
          <span className="hint">Built on the tractor_fleet_sim engine plus field conditions (wind, rain, soil, daylight, curing).</span>
        </div>
        <div className="scenario-grid">
          {scenarios.map((s) => {
            const on = picked.includes(s.id);
            return (
              <article key={s.id} className={`scenario-card${on ? ' on' : ''}`}>
                <label className="row">
                  <input type="checkbox" checked={on}
                    onChange={() => setPicked((p) => (on ? p.filter((x) => x !== s.id) : [...p, s.id]))} />
                  <strong>{s.title}</strong>
                </label>
                <p className="small dim">{s.summary}</p>
                <div className="chips">
                  {s.tags.map((t) => <span key={t} className="chip">{t}</span>)}
                </div>
                <div className="small dim">
                  {s.region} · {s.tractors} machines · {s.jobs} jobs · {s.acres.toLocaleString()} ac ·
                  {' '}{Math.round((s.horizon * s.tick_minutes) / 60)} h horizon
                </div>
                <button type="button" className="link" onClick={() => setOpenInfo(openInfo === s.id ? null : s.id)}>
                  {openInfo === s.id ? 'Hide realism notes' : 'Realism notes & sources'}
                </button>
                {openInfo === s.id && (
                  <div className="small">
                    <ul className="tight">{s.realism.map((r) => <li key={r}>{r}</li>)}</ul>
                    {s.sources.length > 0 && (
                      <ul className="tight sources">
                        {s.sources.map((src) => {
                          const [label, url] = src.split(': http');
                          return <li key={src}>{url ? <a href={`http${url}`} target="_blank" rel="noreferrer">{label}</a> : src}</li>;
                        })}
                      </ul>
                    )}
                    {s.machines && (
                      <details>
                        <summary>Machine rates</summary>
                        <ul className="tight">
                          {Object.entries(s.machines).flatMap(([mid, imps]) =>
                            Object.entries(imps).map(([imp, basis]) => <li key={mid + imp}><strong>{mid}</strong> {imp}: {basis}</li>))}
                        </ul>
                      </details>
                    )}
                  </div>
                )}
              </article>
            );
          })}
        </div>
        <div className="controls lab-controls">
          <div className="btn-group">
            {ALL_POLICIES.map((p) => (
              <label key={p} className="inline policy-pick">
                <input type="checkbox" checked={policies.includes(p)}
                  onChange={() => setPolicies((ps) => (ps.includes(p) ? ps.filter((x) => x !== p) : [...ps, p]))} />
                <span className="key-line" style={{ background: POLICY_COLOR[p] }} />
                {POLICY_LABEL[p]}
              </label>
            ))}
          </div>
          <div className="btn-group">
            <label className="inline">Seeds
              <select value={nSeeds} onChange={(e) => setNSeeds(Number(e.target.value))}>
                {[1, 2, 3, 4, 5].map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
            </label>
            <span className="small dim">{picked.length * policies.length * nSeeds} runs</span>
            <button className="primary" disabled={!picked.length || !policies.length || job?.status === 'running'} onClick={start}>
              Run simulations
            </button>
          </div>
        </div>
        <p className="small dim">Seed 0 is the scenario as written; other seeds shift weather, breakdown timing and starting fuel, and may add an unscripted breakdown. Every policy faces identical conditions for a given seed.</p>
        <Progress job={job} />
      </section>

      {runs.length > 0 && (
        <>
          <section className="panel">
            <ChartFrame title="Score by scenario" subtitle={`Mean over ${nSeeds} seed(s). Score = on-time completions + priority + partial acres − travel − idle − rain-damaged acres.`}
              legend={usedPolicies.map((p) => <LegendItem key={p} color={POLICY_COLOR[p]} label={POLICY_LABEL[p]} line={false} />)}
              table={<ResultsTable scenarios={usedScenarios} policies={usedPolicies} agg={agg} />}>
              <GroupedBars categories={usedScenarios.map((s) => ({ id: s.id, label: s.title }))}
                series={usedPolicies.map((p) => ({ id: p, label: POLICY_LABEL[p], color: POLICY_COLOR[p],
                  values: Object.fromEntries(usedScenarios.map((s) => [s.id, agg(s.id, p, (r) => r.score)])) }))}
                yLabel="score" />
            </ChartFrame>
            <details open>
              <summary className="small">All measured results</summary>
              <ResultsTable scenarios={usedScenarios} policies={usedPolicies} agg={agg} />
            </details>
          </section>

          <section className="panel">
            <div className="panel-head">
              <h2>Run explorer</h2>
              <div className="btn-group">
                <select value={focus ? `${focus.scenario}|${focus.seed}` : ''} aria-label="Scenario and seed"
                  onChange={(e) => { const [s, seed] = e.target.value.split('|'); setFocus({ scenario: s, seed: Number(seed) }); }}>
                  {[...new Set(runs.map((r) => `${r.scenario_id}|${r.seed}`))].map((k) => {
                    const [s, seed] = k.split('|');
                    return <option key={k} value={k}>{scenarios.find((x) => x.id === s)?.title ?? s} · seed {seed}</option>;
                  })}
                </select>
              </div>
            </div>
            {!loaded.length ? <p className="dim">Loading timelines…</p> : (
              <div className="explorer">
                <ChartFrame title="Acres completed over time" subtitle="Same scenario, events, conditions and seed for every policy. Shaded = night."
                  legend={loaded.map((d) => <LegendItem key={d.policy} color={POLICY_COLOR[d.policy]} label={POLICY_LABEL[d.policy]} />)}
                  table={<TimelineTable runs={loaded} fmtTick={fmtTick} />}>
                  <LineChart x={ref.timeline.map((s) => s.tick)} xFormat={fmtTick} yLabel="acres" bands={nightBands(ref.timeline)}
                    series={loaded.map((d) => ({ id: d.policy, label: POLICY_LABEL[d.policy].split(' ')[0], color: POLICY_COLOR[d.policy],
                      values: ref.timeline.map((s) => d.timeline.find((x) => x.tick === s.tick)?.acres ?? null) }))} />
                </ChartFrame>
                {ref.timeline[0]?.wind !== null && (
                  <div className="two-col">
                    <ChartFrame title="Wind" subtitle="Spraying allowed only between the 3 and 10 mph lines">
                      <LineChart x={ref.timeline.map((s) => s.tick)} xFormat={fmtTick} yLabel="mph" height={150}
                        bands={nightBands(ref.timeline)} directLabels={false} yMin={0}
                        thresholds={[{ y: 10, label: '10 mph drift limit' }, { y: 3, label: '3 mph inversion risk' }]}
                        series={[{ id: 'wind', label: 'Wind', color: 'var(--series-1)', values: ref.timeline.map((s) => s.wind) }]} />
                    </ChartFrame>
                    <ChartFrame title="Rain" subtitle="Rain stops work; soaking rain closes soil for 12–48 h by texture">
                      <LineChart x={ref.timeline.map((s) => s.tick)} xFormat={fmtTick} yLabel="mm/h" height={150}
                        bands={nightBands(ref.timeline)} directLabels={false} yMin={0}
                        series={[{ id: 'rain', label: 'Rain', color: 'var(--series-3)', values: ref.timeline.map((s) => s.rain) }]} />
                    </ChartFrame>
                  </div>
                )}
                <ChartFrame title="Machine activity" subtitle={focusDetail ? `${POLICY_LABEL[focusDetail.policy]} · score ${focusDetail.score}` : ''}
                  legend={<ActivityLegend />}>
                  <div className="btn-group seg">
                    {loaded.map((d) => (
                      <button key={d.policy} type="button" className={d.policy === focusDetail?.policy ? 'primary' : ''}
                        onClick={() => setFocusPolicy(d.policy)}>{POLICY_LABEL[d.policy]}</button>
                    ))}
                  </div>
                  {focusDetail && <Gantt timeline={focusDetail.timeline} />}
                </ChartFrame>
                {focusDetail && <RunNotes run={focusDetail} />}
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}

function ResultsTable({ scenarios, policies, agg }: {
  scenarios: ScenarioInfo[]; policies: string[];
  agg: (sid: string, pol: string, f: (r: RunSummary) => number) => number | undefined;
}) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr><th>Scenario</th><th>Policy</th><th>Score</th><th>Jobs done</th><th>Acres</th><th>Idle ticks</th><th>Condition halts</th><th>Rain-damaged ac</th></tr>
        </thead>
        <tbody>
          {scenarios.flatMap((s) => policies.map((p, i) => (
            <tr key={s.id + p}>
              <td>{i === 0 ? <strong>{s.title}</strong> : ''}</td>
              <td><span className="key-line" style={{ background: POLICY_COLOR[p] }} /> {POLICY_LABEL[p]}</td>
              <td><strong>{agg(s.id, p, (r) => r.score) ?? '–'}</strong></td>
              <td>{agg(s.id, p, (r) => r.metrics.completed_jobs) ?? '–'}</td>
              <td>{agg(s.id, p, (r) => r.metrics.acres_completed) ?? '–'}</td>
              <td>{agg(s.id, p, (r) => r.metrics.idle_tractor_ticks) ?? '–'}</td>
              <td>{agg(s.id, p, (r) => r.metrics.weather_halts) ?? '–'}</td>
              <td>{agg(s.id, p, (r) => r.metrics.rain_damaged_acres) ?? '–'}</td>
            </tr>
          )))}
        </tbody>
      </table>
    </div>
  );
}

function TimelineTable({ runs, fmtTick }: { runs: RunDetail[]; fmtTick: (t: number) => string }) {
  const ticks = runs[0].timeline.filter((_, i) => i % 4 === 0).map((s) => s.tick);
  return (
    <table>
      <thead><tr><th>Time</th>{runs.map((r) => <th key={r.policy}>{POLICY_LABEL[r.policy]}</th>)}</tr></thead>
      <tbody>
        {ticks.map((t) => (
          <tr key={t}><td>{fmtTick(t)} (t{t})</td>{runs.map((r) => <td key={r.policy}>{r.timeline.find((s) => s.tick === t)?.acres ?? '–'}</td>)}</tr>
        ))}
      </tbody>
    </table>
  );
}

function RunNotes({ run }: { run: RunDetail }) {
  return (
    <div className="two-col">
      <div>
        <h3>Jobs</h3>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Job</th><th>Task</th><th>Done</th><th>Status</th></tr></thead>
            <tbody>
              {run.jobs.map((j) => (
                <tr key={j.id}>
                  <td><strong>{j.id}</strong></td><td>{j.job_type}</td>
                  <td>{j.completed_acres}/{j.acres} ac</td>
                  <td><span className={`badge ${j.status === 'completed' ? 'ok' : j.completed_acres > 0 ? 'warn' : 'muted'}`}>{j.status.replace('_', ' ')}</span>
                    {j.completed_tick != null && <span className="dim"> t{j.completed_tick}</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {run.skill && <p className="small dim">Skill used: <strong>{run.skill.name}</strong> ({run.skill.status}) – {run.skill.why}</p>}
      </div>
      <div>
        <h3>Stopped by field conditions</h3>
        {run.halts.length === 0 ? <p className="dim small">No work had to stop.</p> : (
          <ul className="tight small">
            {run.halts.map((h, i) => <li key={i}><span className="dim">t{h.tick}</span> {h.tractor_id} on {h.job_id}: {h.reason}</li>)}
          </ul>
        )}
        <h3>Last decisions</h3>
        <div className="log short">
          {run.decision_log.slice(-4).reverse().map((d, i) => <pre key={i}>{d.explanation}</pre>)}
        </div>
      </div>
    </div>
  );
}
