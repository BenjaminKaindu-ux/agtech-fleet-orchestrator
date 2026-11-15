import { useEffect, useState } from 'react';
import { api } from './api';
import type { JobStatus } from './api';

/** Poll a background job until it finishes. */
export function useJob<T>(onDone?: (job: JobStatus<T>) => void) {
  const [job, setJob] = useState<JobStatus<T> | null>(null);
  useEffect(() => {
    if (!job || job.status !== 'running') return;
    const id = window.setTimeout(async () => {
      try {
        const next = await api.job<T>(job.id);
        setJob(next);
        if (next.status !== 'running') onDone?.(next);
      } catch { /* keep polling */ }
    }, 900);
    return () => window.clearTimeout(id);
  }, [job, onDone]);
  return [job, setJob] as const;
}

