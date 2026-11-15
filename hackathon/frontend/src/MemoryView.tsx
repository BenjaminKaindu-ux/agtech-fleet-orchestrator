import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from './api';
import type { Episode, JobStatus, LearningSummary, ScenarioInfo, Skill } from './api';
import { ChartFrame, LegendItem, LineChart } from './charts';
import { POLICY_COLOR, POLICY_LABEL } from './chartUtils';
import { Progress } from './LabView';
import { useJob } from './useJob';

const STATUS_CLASS: Record<string, string> = { proven: 'ok', default: 'busy', candidate: 'warn', rejected: 'muted' };

export default function MemoryView({ onFlash }: { onFlash: (kind: 'ok' | 'err', text: string) => void }) {
  const [data, setData] = useState<LearningSummary | null>(null);
  const [scenarios, setScenarios] = useState<ScenarioInfo[]>([]);
  const [trainSet, setTrainSet] = useState<string[]>([]);
  const [rounds, setRounds] = useState(2);
  const [curveScenario, setCurveScenario] = useState('');
  const [skillMd, setSkillMd] = useState<{ id: string; markdown: string } | null>(null);
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [epFilter, setEpFilter] = useState({ scenario_id: '', policy: '', q: '' });
  const [showRejected, setShowRejected] = useState(false);

  const load = useCallback(async () => {
    try {
      const d = await api.learning();
      setData(d);
      setCurveScenario((cur) => cur || d.curve[d.curve.length - 1]?.scenario_id || '');
    } catch (e) {
      onFlash('err', `Memory: ${(e as Error).message}`);
    }
  }, [onFlash]);

  useEffect(() => {
    const t = window.setTimeout(load, 0);
    api.scenarios().then((r) => {
      setScenarios(r.scenarios);
      setTrainSet(r.scenarios.filter((s) => s.id !== 'cochise-field-day').map((s) => s.id));
    }).catch(() => undefined);
    return () => window.clearTimeout(t);
  }, [load]);

  useEffect(() => {
    let live = true;
    const t = window.setTimeout(() => {
      api.episodes({ ...epFilter, limit: 40 }).then((r) => { if (live) setEpisodes(r.episodes); }).catch(() => undefined);
    }, 150);
    return () => { live = false; window.clearTimeout(t); };
  }, [epFilter, data]);

  const onDone = useCallback((j: JobStatus) => {
    load();
    onFlash(j.status === 'done' ? 'ok' : 'err', j.status === 'done' ? 'Training finished – memory updated' : j.error ?? 'Training failed');
  }, [load, onFlash]);
  const [job, setJob] = useJob(onDone);
  useEffect(() => {
    if (job?.status !== 'running' || job.progress === 0) return;
    const t = window.setTimeout(load, 0);
    return () => window.clearTimeout(t);
  }, [job?.progress, job?.status, load]);

  const train = async () => {
    try {
      setJob(await api.train(trainSet, rounds));
    } catch (e) {
      onFlash('err', `Train: ${(e as Error).message}`);
    }
  };

  const titleOf = useCallback((sid: string | null) => scenarios.find((s) => s.id === sid)?.title ?? sid ?? 'any scenario', [scenarios]);
  const curve = useMemo(() => (data?.curve ?? []).filter((p) => p.scenario_id === curveScenario), [data, curveScenario]);
  const curveScenarios = useMemo(() => [...new Set((data?.curve ?? []).map((p) => p.scenario_id))], [data]);
  const skills = (data?.skills ?? []).filter((s) => showRejected || s.status !== 'rejected');

  if (!data) return <p className="dim">Loading memory…</p>;
  const counts = data.skill_counts;
  const insights = [...data.insights].reverse();

  return (
    <div className="memory-view">
      <div className="metrics">
        <Tile label="Episodes remembered" value={data.episode_count} sub="every simulated run" />
        <Tile label="Proven skills" value={counts.proven ?? 0} sub="won on held-out seeds" />
        <Tile label="Rejected variants" value={counts.rejected ?? 0} sub="did not generalise" />
        <Tile label="Insights" value={data.insights.length} sub="measured, candidate" />
        <Tile label="Scenarios learned" value={Object.keys(data.facts.scenarios).length} sub="with stored facts" />
        <Tile label="Training rounds" value={data.curve.length} sub="scenario-rounds" />
      </div>

      <section className="panel">
        <div className="panel-head">
          <h2>Self-improvement</h2>
          <span className="hint">Hermes-style loop: practise → measure → keep only what wins on unseen conditions → write it down.</span>
        </div>
        <div className="train-row">
          <div className="chips">
            {scenarios.map((s) => {
              const on = trainSet.includes(s.id);
              return (
                <label key={s.id} className={`chip pick${on ? ' on' : ''}`}>
                  <input type="checkbox" checked={on}
                    onChange={() => setTrainSet((t) => (on ? t.filter((x) => x !== s.id) : [...t, s.id]))} />
                  {s.title}
                </label>
              );
            })}
          </div>
          <div className="btn-group">
            <label className="inline">Rounds
              <select value={rounds} onChange={(e) => setRounds(Number(e.target.value))}>
                {[1, 2, 3, 4].map((n) => <option key={n}>{n}</option>)}
              </select>
            </label>
            <button className="primary" disabled={!trainSet.length || job?.status === 'running'} onClick={train}>Train agent</button>
          </div>
        </div>
        <p className="small dim">
          Each round per scenario: three challenger strategies (refine, bold step, random explore) play 5 paired seeds against the current champion;
          the best must also win on 3 held-out seeds before it is promoted. Roughly 15–40 s per scenario-round.
        </p>
        <Progress job={job} />
      </section>

      <section className="panel">
        <ChartFrame title="Learning curve"
          subtitle="Mean score of the agent's current champion vs the default optimizer and FCFS, on the same 8 seeds after each training round."
          legend={['agent', 'optimizer', 'fcfs'].map((p) => <LegendItem key={p} color={POLICY_COLOR[p]} label={POLICY_LABEL[p]} />)}
          table={<CurveTable points={curve} />}>
          <div className="btn-group seg">
            {curveScenarios.length === 0 && <span className="dim small">Train the agent to see its learning curve.</span>}
            {curveScenarios.map((s) => (
              <button key={s} type="button" className={s === curveScenario ? 'primary' : ''} onClick={() => setCurveScenario(s)}>{titleOf(s)}</button>
            ))}
          </div>
          {curve.length > 0 && (
            <LineChart x={curve.map((_, i) => i + 1)} xFormat={(v) => `round ${v}`} yLabel="score"
              series={['agent', 'optimizer', 'fcfs'].map((p) => ({
                id: p, label: POLICY_LABEL[p].split(' ')[0], color: POLICY_COLOR[p],
                values: curve.map((pt) => pt[p as 'agent' | 'optimizer' | 'fcfs']),
              }))} />
          )}
        </ChartFrame>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>Skill library</h2>
          <label className="inline small"><input type="checkbox" checked={showRejected} onChange={(e) => setShowRejected(e.target.checked)} /> show rejected</label>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Skill</th><th>Scenario</th><th>Status</th><th>Look-ahead</th><th>Weights (prio / urgency / acres / travel)</th><th>Δ vs parent (train → held-out)</th><th>Δ vs default</th><th></th></tr></thead>
            <tbody>
              {skills.map((s: Skill) => {
                const ev = s.scenario_id ? s.evidence[s.scenario_id] : undefined;
                return (
                  <tr key={s.id}>
                    <td><strong>{s.name}</strong><div className="dim small">{s.id} · v{s.version}</div></td>
                    <td>{titleOf(s.scenario_id)}</td>
                    <td><span className={`badge ${STATUS_CLASS[s.status] ?? ''}`}>{s.status}</span></td>
                    <td>{s.params.horizon} ticks</td>
                    <td className="mono small">{[s.params.w_priority, s.params.w_urgency, s.params.w_acres, s.params.w_travel].map((v) => Math.round(v * 100) / 100).join(' / ')}</td>
                    <td>{ev ? <>{signed(ev.train_delta)} <span className="dim">({ev.wins}/{ev.train_n} wins)</span>{ev.holdout_delta !== undefined && <> → {signed(ev.holdout_delta)}</>}</> : '–'}</td>
                    <td>{ev?.vs_default !== undefined ? <strong>{signed(ev.vs_default)}</strong> : '–'}</td>
                    <td><button className="link" onClick={() => api.skillMd(s.id).then(setSkillMd)}>SKILL.md</button></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {skillMd && (
          <div className="skill-md">
            <div className="row"><strong>{skillMd.id}/SKILL.md</strong><button className="link" onClick={() => setSkillMd(null)}>close</button></div>
            <pre>{skillMd.markdown}</pre>
          </div>
        )}
      </section>

      <div className="layout">
        <section className="panel">
          <div className="panel-head"><h2>What the agent has learned</h2><span className="hint">Measured insights, updated as runs accumulate. Status stays <em>candidate</em>.</span></div>
          {insights.length === 0 ? <p className="dim">Run simulations or training to build insights.</p> : (
            <div className="mem-list">
              {insights.map((r) => (
                <div key={r.key} className="mem">
                  <div className="row">
                    <span className="badge warn">{r.status}</span>
                    {r.measured?.comparison?.comparable && <span className="badge ok">paired comparison</span>}
                    <span className="dim small">seen {r.observations ?? 1}× · updated {new Date(r.updated_at ?? r.created_at).toLocaleString()}</span>
                  </div>
                  <p>{r.interpretation?.lesson}</p>
                </div>
              ))}
            </div>
          )}
        </section>
        <section className="panel">
          <div className="panel-head"><h2>Facts per scenario</h2><span className="hint">Running averages over every remembered run</span></div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Scenario</th><th>Policy</th><th>Runs</th><th>Mean score</th><th>Best</th><th>Mean jobs</th></tr></thead>
              <tbody>
                {Object.entries(data.facts.scenarios).flatMap(([sid, sc]) => Object.entries(sc.by_policy).map(([pol, st], i) => (
                  <tr key={sid + pol}>
                    <td>{i === 0 ? <strong>{sc.title}</strong> : ''}</td>
                    <td><span className="key-line" style={{ background: POLICY_COLOR[pol] }} /> {POLICY_LABEL[pol] ?? pol}</td>
                    <td>{st.n}</td><td>{st.mean_score}</td><td>{st.best_score}</td><td>{st.mean_jobs}</td>
                  </tr>
                )))}
              </tbody>
            </table>
          </div>
          <h3>Field conditions observed</h3>
          <ul className="tight small">
            {Object.entries(data.facts.conditions).filter(([, c]) => Object.keys(c.halt_reasons).length).map(([sid, c]) => (
              <li key={sid}><strong>{titleOf(sid)}</strong>: {c.mean_halts} stops/run ·{' '}
                {Object.entries(c.halt_reasons).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([k, v]) => `${k} (${v})`).join(', ')}
                {c.mean_rain_damage > 0 && ` · ${c.mean_rain_damage} ac rain-damaged/run`}</li>
            ))}
          </ul>
        </section>
      </div>

      <section className="panel">
        <div className="panel-head">
          <h2>Episode history</h2>
          <div className="btn-group">
            <select value={epFilter.scenario_id} onChange={(e) => setEpFilter({ ...epFilter, scenario_id: e.target.value })} aria-label="Scenario filter">
              <option value="">all scenarios</option>
              {scenarios.map((s) => <option key={s.id} value={s.id}>{s.title}</option>)}
            </select>
            <select value={epFilter.policy} onChange={(e) => setEpFilter({ ...epFilter, policy: e.target.value })} aria-label="Policy filter">
              <option value="">all policies</option>
              {Object.keys(POLICY_LABEL).map((p) => <option key={p} value={p}>{POLICY_LABEL[p]}</option>)}
            </select>
            <input placeholder="search" value={epFilter.q} onChange={(e) => setEpFilter({ ...epFilter, q: e.target.value })} />
          </div>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>When</th><th>Source</th><th>Scenario</th><th>Seed</th><th>Policy</th><th>Score</th><th>Jobs</th><th>Halts</th></tr></thead>
            <tbody>
              {episodes.map((e) => (
                <tr key={e.id}>
                  <td className="small">{new Date(e.at).toLocaleTimeString()}</td>
                  <td><span className="badge muted">{e.role}</span></td>
                  <td>{e.title}</td><td>{e.seed}</td>
                  <td><span className="key-line" style={{ background: POLICY_COLOR[e.policy] }} /> {POLICY_LABEL[e.policy] ?? e.policy}</td>
                  <td><strong>{e.score}</strong></td>
                  <td>{e.metrics.completed_jobs}/{e.jobs_total}</td>
                  <td>{e.metrics.weather_halts ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="foot">Memory folder: <code>{data.root}</code></p>
      </section>
    </div>
  );
}

const signed = (v: number | undefined) => (v === undefined ? '–' : `${v > 0 ? '+' : ''}${Math.round(v * 10) / 10}`);

function Tile({ label, value, sub }: { label: string; value: number | string; sub: string }) {
  return (
    <div className="tile">
      <span className="tile-label">{label}</span>
      <span className="tile-value">{value}</span>
      <span className="tile-sub">{sub}</span>
    </div>
  );
}

function CurveTable({ points }: { points: LearningSummary['curve'] }) {
  return (
    <table>
      <thead><tr><th>Round</th><th>Champion</th><th>Agent</th><th>Optimizer</th><th>FCFS</th><th>Outcome</th></tr></thead>
      <tbody>
        {points.map((p, i) => (
          <tr key={i}><td>{i + 1}</td><td className="small">{p.champion}</td><td><strong>{p.agent}</strong></td><td>{p.optimizer}</td><td>{p.fcfs}</td><td className="small">{p.outcome}</td></tr>
        ))}
      </tbody>
    </table>
  );
}
