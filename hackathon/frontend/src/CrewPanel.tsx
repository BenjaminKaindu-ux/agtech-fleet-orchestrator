import { useCallback, useState } from 'react';
import type { FormEvent } from 'react';
import { api, ApiError } from './api';
import type { FleetState, JobStatus } from './api';
import { useJob } from './useJob';

interface CrewResult { answer: string; changes: string[]; steps: number }

interface Props {
  state: FleetState;
  onStart: () => void;
  onChanged: () => void;
  onFlash: (kind: 'ok' | 'err', text: string) => void;
}

export default function CrewPanel({ state, onStart, onChanged, onFlash }: Props) {
  const [task, setTask] = useState('');
  const onDone = useCallback((job: JobStatus<CrewResult>) => {
    onChanged();
    if (job.status === 'error') onFlash('err', `AI crew: ${job.error}`);
    else onFlash('ok', `AI crew finished: ${job.result?.changes.length ? job.result.changes.join(', ') : 'no changes committed'}`);
  }, [onChanged, onFlash]);
  const [job, setJob] = useJob<CrewResult>(onDone);
  const running = state.crew_running || job?.status === 'running';

  const start = async (e: FormEvent) => {
    e.preventDefault();
    onStart();
    try {
      setJob((await api.crewRun(task.trim())) as JobStatus<CrewResult>);
      onChanged();
    } catch (err) {
      onFlash('err', `AI crew: ${err instanceof ApiError ? err.message : String(err)}`);
    }
  };

  const log = [...(state.crew_log ?? [])].reverse();
  return (
    <div>
      <form className="crew-form" onSubmit={start}>
        <input value={task} onChange={(e) => setTask(e.target.value)} maxLength={500} disabled={running}
          placeholder="Optional instruction, e.g. “prioritise the lygus sprays”" aria-label="Instruction for the AI crew" />
        <button type="submit" className="primary" disabled={running || state.done}>
          {running ? 'Crew is working…' : 'Ask AI crew to plan'}
        </button>
      </form>
      {running && (
        <p className="crew-status">
          Fleet, navigation, agronomy and safety agents are reviewing the live farm (about 1–3 min). The clock is paused;
          anything they commit appears on the map and in the reasoning log right away.
        </p>
      )}
      {!running && !log.length && (
        <p className="dim">Runs the LLM agent team on this exact simulator (no copy). Its dispatches become live data.</p>
      )}
      {log.length > 0 && (
        <ul className="crew-log">
          {log.map((r, i) => (
            <li key={`${r.tick}-${i}`}>
              <div className="row"><span className="badge src-crew">crew</span> <span className="dim">t{r.tick} · {r.steps} steps</span></div>
              <div>{r.answer}</div>
              <div className="changes">
                {r.changes.length ? r.changes.map((c) => <span key={c} className="chip">{c}</span>) : <span className="dim">no changes committed</span>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
