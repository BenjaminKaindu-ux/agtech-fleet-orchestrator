import type { TimelineSample } from './api';

// Fixed categorical order (validated: dark & light, adjacent CVD ΔE ≥ 8.4).
// Colour follows the policy, never its rank.
export const POLICY_COLOR: Record<string, string> = {
  agent: 'var(--series-1)',
  fcfs: 'var(--series-2)',
  optimizer: 'var(--series-3)',
  'priority-nearest': 'var(--series-4)',
};
export const POLICY_LABEL: Record<string, string> = {
  agent: 'Agent (learned)',
  fcfs: 'FCFS baseline',
  optimizer: 'Optimizer (default)',
  'priority-nearest': 'Priority-nearest',
};

export const ACTIVITY: Array<{ key: string; label: string; color: string; match: string[] }> = [
  { key: 'working', label: 'Working', color: 'var(--series-1)', match: ['working'] },
  { key: 'traveling', label: 'Travelling', color: 'var(--series-3)', match: ['traveling', 'to_refuel'] },
  { key: 'refueling', label: 'Refuelling', color: 'var(--series-4)', match: ['refueling'] },
  { key: 'blocked', label: 'Blocked route', color: 'var(--series-2)', match: ['blocked'] },
  { key: 'broken', label: 'Broken down', color: 'var(--critical)', match: ['broken', 'stranded'] },
  { key: 'idle', label: 'Idle / waiting', color: 'var(--neutral)', match: ['idle'] },
];
export const activityOf = (status: string) => ACTIVITY.find((a) => a.match.includes(status)) ?? ACTIVITY[ACTIVITY.length - 1];


/** Night bands (ticks where daylight is false) from a timeline. */
export function nightBands(tl: TimelineSample[]): Array<{ from: number; to: number }> {
  const out: Array<{ from: number; to: number }> = [];
  let start: number | null = null;
  tl.forEach((s, i) => {
    if (!s.daylight && start === null) start = s.tick;
    if ((s.daylight || i === tl.length - 1) && start !== null) {
      out.push({ from: start, to: s.tick });
      start = null;
    }
  });
  return out;
}

