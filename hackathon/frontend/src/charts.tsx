import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { TimelineSample } from './api';
import { ACTIVITY, activityOf, nightBands } from './chartUtils';

function useWidth<T extends HTMLElement>(fallback = 600) {
  const ref = useRef<T>(null);
  const [w, setW] = useState(fallback);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(260, Math.floor(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

function niceTicks(min: number, max: number, count = 4): number[] {
  if (max <= min) max = min + 1;
  const raw = (max - min) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const out = [];
  for (let v = Math.floor(min / step) * step; v <= max + step * 0.001; v += step) out.push(Math.round(v * 1000) / 1000);
  return out;
}

const fmt = (v: number) => (Math.abs(v) >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 }) : String(Math.round(v * 10) / 10));

export function ChartFrame({ title, subtitle, legend, table, children }: {
  title: string; subtitle?: string; legend?: ReactNode; table?: ReactNode; children: ReactNode;
}) {
  const [showTable, setShowTable] = useState(false);
  return (
    <figure className="chart">
      <figcaption className="chart-head">
        <div>
          <div className="chart-title">{title}</div>
          {subtitle && <div className="chart-sub">{subtitle}</div>}
        </div>
        {table && (
          <button type="button" className="link" onClick={() => setShowTable((v) => !v)} aria-pressed={showTable}>
            {showTable ? 'Chart' : 'Table'}
          </button>
        )}
      </figcaption>
      {legend && !showTable && <div className="legend">{legend}</div>}
      {showTable ? <div className="table-wrap chart-table">{table}</div> : children}
    </figure>
  );
}

export function LegendItem({ color, label, line = true }: { color: string; label: string; line?: boolean }) {
  return (
    <span className="legend-item">
      <span className={line ? 'key-line' : 'key-box'} style={{ background: color }} />
      {label}
    </span>
  );
}

export interface Series { id: string; label: string; color: string; values: Array<number | null> }

/** Multi-series line chart over a shared x (ticks). Crosshair + tooltip lists every series. */
export function LineChart({ x, series, height = 220, yLabel, xFormat = (v) => `t${v}`, bands = [], thresholds = [],
  yMin, directLabels = true }: {
  x: number[]; series: Series[]; height?: number; yLabel?: string; xFormat?: (v: number) => string;
  bands?: Array<{ from: number; to: number }>; thresholds?: Array<{ y: number; label: string }>;
  yMin?: number; directLabels?: boolean;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const labelRoom = directLabels && series.length <= 4 ? 96 : 12;
  const m = { l: 44, r: labelRoom, t: yLabel ? 20 : 10, b: 26 };
  const iw = Math.max(50, width - m.l - m.r);
  const ih = height - m.t - m.b;
  const all = series.flatMap((s) => s.values.filter((v): v is number => v !== null));
  const lo = yMin ?? Math.min(0, ...all, ...thresholds.map((t) => t.y));
  const hi = Math.max(1, ...all, ...thresholds.map((t) => t.y));
  const yt = niceTicks(lo, hi);
  const y0 = yt[0], y1 = yt[yt.length - 1];
  const x0 = x[0] ?? 0, x1 = x[x.length - 1] ?? 1;
  const sx = (v: number) => m.l + ((v - x0) / Math.max(1, x1 - x0)) * iw;
  const sy = (v: number) => m.t + ih - ((v - y0) / Math.max(1e-9, y1 - y0)) * ih;
  const xt = niceTicks(x0, x1, Math.max(2, Math.floor(iw / 90))).filter((v) => v >= x0 && v <= x1);

  const paths = series.map((s) => {
    let d = '';
    s.values.forEach((v, i) => {
      if (v === null) return;
      d += `${d && s.values[i - 1] !== null ? 'L' : 'M'}${sx(x[i]).toFixed(1)},${sy(v).toFixed(1)}`;
    });
    return d;
  });

  // Direct labels at line ends, nudged apart so they never collide.
  const ends = series.map((s, i) => {
    let j = s.values.length - 1;
    while (j >= 0 && s.values[j] === null) j--;
    return { i, y: j >= 0 ? sy(s.values[j] as number) : m.t, v: j >= 0 ? s.values[j] : null };
  }).sort((a, b) => a.y - b.y);
  for (let k = 1; k < ends.length; k++) if (ends[k].y - ends[k - 1].y < 13) ends[k].y = ends[k - 1].y + 13;

  const onMove = (e: React.PointerEvent<SVGRectElement>) => {
    const r = (e.currentTarget as SVGRectElement).getBoundingClientRect();
    const px = e.clientX - r.left;
    const xv = x0 + (px / r.width) * (x1 - x0);
    let best = 0;
    x.forEach((v, i) => { if (Math.abs(v - xv) < Math.abs(x[best] - xv)) best = i; });
    setHover(best);
  };

  return (
    <div ref={ref} className="plot">
      <svg width={width} height={height} role="img" aria-label={`${yLabel ?? 'value'} over time`}>
        {bands.map((b, i) => (
          <rect key={i} className="band" x={sx(Math.max(x0, b.from))} y={m.t}
            width={Math.max(0, sx(Math.min(x1, b.to)) - sx(Math.max(x0, b.from)))} height={ih} />
        ))}
        {yt.map((v) => (
          <g key={v}>
            <line className="grid" x1={m.l} x2={m.l + iw} y1={sy(v)} y2={sy(v)} />
            <text className="tick" x={m.l - 6} y={sy(v) + 4} textAnchor="end">{fmt(v)}</text>
          </g>
        ))}
        {xt.map((v) => (
          <text key={v} className="tick" x={sx(v)} y={height - 8} textAnchor="middle">{xFormat(v)}</text>
        ))}
        {yLabel && <text className="axis-label" x={4} y={11}>{yLabel}</text>}
        {thresholds.map((t) => (
          <g key={t.label}>
            <line className="threshold" x1={m.l} x2={m.l + iw} y1={sy(t.y)} y2={sy(t.y)} />
            <text className="tick" x={m.l + iw - 4} y={sy(t.y) - 4} textAnchor="end">{t.label}</text>
          </g>
        ))}
        {series.map((s, i) => <path key={s.id} d={paths[i]} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" />)}
        {directLabels && series.length <= 4 && ends.map((e) => e.v !== null && (
          <text key={series[e.i].id} className="direct-label" x={m.l + iw + 6} y={e.y + 4}>
            {series[e.i].label} <tspan className="dim">{fmt(e.v as number)}</tspan>
          </text>
        ))}
        {hover !== null && (
          <g>
            <line className="crosshair" x1={sx(x[hover])} x2={sx(x[hover])} y1={m.t} y2={m.t + ih} />
            {series.map((s) => s.values[hover] !== null && (
              <circle key={s.id} cx={sx(x[hover])} cy={sy(s.values[hover] as number)} r={4} fill={s.color} className="marker" />
            ))}
          </g>
        )}
        <rect x={m.l} y={m.t} width={iw} height={ih} fill="transparent"
          onPointerMove={onMove} onPointerLeave={() => setHover(null)} />
      </svg>
      {hover !== null && (
        <div className="tooltip" style={{ left: Math.min(width - 170, sx(x[hover]) + 10), top: 8 }}>
          <div className="tt-head">{xFormat(x[hover])}</div>
          {series.map((s) => (
            <div key={s.id} className="tt-row">
              <span className="key-line" style={{ background: s.color }} />
              <strong>{s.values[hover] === null ? '–' : fmt(s.values[hover] as number)}</strong>
              <span>{s.label}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Grouped bars: one group per category, one bar per series. Hover tooltip per bar. */
export function GroupedBars({ categories, series, height = 240, yLabel }: {
  categories: Array<{ id: string; label: string }>;
  series: Array<{ id: string; label: string; color: string; values: Record<string, number | undefined> }>;
  height?: number; yLabel?: string;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [tip, setTip] = useState<{ x: number; y: number; cat: string; s: string; v: number } | null>(null);
  const m = { l: 48, r: 8, t: yLabel ? 22 : 12, b: 40 };
  const iw = width - m.l - m.r, ih = height - m.t - m.b;
  const vals = series.flatMap((s) => Object.values(s.values).filter((v): v is number => v !== undefined));
  const yt = niceTicks(Math.min(0, ...vals), Math.max(1, ...vals));
  const y0 = yt[0], y1 = yt[yt.length - 1];
  const sy = (v: number) => m.t + ih - ((v - y0) / (y1 - y0)) * ih;
  const gw = iw / Math.max(1, categories.length);
  const bw = Math.max(4, Math.min(28, (gw - 16) / Math.max(1, series.length) - 2));
  return (
    <div ref={ref} className="plot">
      <svg width={width} height={height} role="img" aria-label={yLabel ?? 'bar chart'}>
        {yt.map((v) => (
          <g key={v}>
            <line className={v === 0 ? 'baseline' : 'grid'} x1={m.l} x2={m.l + iw} y1={sy(v)} y2={sy(v)} />
            <text className="tick" x={m.l - 6} y={sy(v) + 4} textAnchor="end">{fmt(v)}</text>
          </g>
        ))}
        {yLabel && <text className="axis-label" x={4} y={11}>{yLabel}</text>}
        {categories.map((c, ci) => {
          const gx = m.l + ci * gw + (gw - series.length * (bw + 2)) / 2;
          return (
            <g key={c.id}>
              {series.map((s, si) => {
                const v = s.values[c.id];
                if (v === undefined) return null;
                const x = gx + si * (bw + 2);
                const top = sy(Math.max(0, v)), bottom = sy(Math.min(0, v));
                const h = Math.max(1, bottom - top);
                const r = Math.min(4, bw / 2, h);
                const d = v >= 0
                  ? `M${x},${bottom}V${top + r}Q${x},${top} ${x + r},${top}H${x + bw - r}Q${x + bw},${top} ${x + bw},${top + r}V${bottom}Z`
                  : `M${x},${top}V${bottom - r}Q${x},${bottom} ${x + r},${bottom}H${x + bw - r}Q${x + bw},${bottom} ${x + bw},${bottom - r}V${top}Z`;
                return (
                  <path key={s.id} d={d} fill={s.color} className="bar-mark" tabIndex={0}
                    onPointerEnter={() => setTip({ x: x + bw / 2, y: top, cat: c.label, s: s.label, v })}
                    onFocus={() => setTip({ x: x + bw / 2, y: top, cat: c.label, s: s.label, v })}
                    onPointerLeave={() => setTip(null)} onBlur={() => setTip(null)} />
                );
              })}
              <text className="tick cat" x={m.l + ci * gw + gw / 2} y={height - 22} textAnchor="middle">
                {c.label.length > 22 ? `${c.label.slice(0, 21)}…` : c.label}
              </text>
            </g>
          );
        })}
      </svg>
      {tip && (
        <div className="tooltip" style={{ left: Math.max(0, Math.min(width - 190, tip.x - 80)), top: Math.max(0, tip.y - 66) }}>
          <div className="tt-head">{tip.cat}</div>
          <div className="tt-row"><strong>{fmt(tip.v)}</strong><span>{tip.s}</span></div>
        </div>
      )}
    </div>
  );
}

/** Tractor activity over time (one row per machine). */
export function Gantt({ timeline, height: rowH = 22 }: { timeline: TimelineSample[]; height?: number }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [tip, setTip] = useState<{ x: number; y: number; text: string; sub: string } | null>(null);
  const ids = useMemo(() => Object.keys(timeline[0]?.tractors ?? {}).sort(), [timeline]);
  const segs = useMemo(() => {
    const out: Record<string, Array<{ from: number; to: number; status: string; job: string | null }>> = {};
    for (const id of ids) {
      const rows: Array<{ from: number; to: number; status: string; job: string | null }> = [];
      timeline.forEach((s, i) => {
        const [status, job] = s.tractors[id] ?? ['idle', null];
        const key = activityOf(status).key;
        const last = rows[rows.length - 1];
        const next = timeline[i + 1]?.tick ?? s.tick + 1;
        if (last && activityOf(last.status).key === key && last.job === job) last.to = next;
        else rows.push({ from: s.tick, to: next, status, job });
      });
      out[id] = rows;
    }
    return out;
  }, [timeline, ids]);
  const m = { l: 40, r: 8, t: 4, b: 22 };
  const x0 = timeline[0]?.tick ?? 0, x1 = timeline[timeline.length - 1]?.tick ?? 1;
  const iw = width - m.l - m.r;
  const sx = (v: number) => m.l + ((v - x0) / Math.max(1, x1 - x0)) * iw;
  const h = m.t + ids.length * (rowH + 4) + m.b;
  const xt = niceTicks(x0, x1, Math.max(2, Math.floor(iw / 90))).filter((v) => v >= x0 && v <= x1);
  return (
    <div ref={ref} className="plot">
      <svg width={width} height={h} role="img" aria-label="Tractor activity timeline">
        {nightBands(timeline).map((b, i) => (
          <rect key={i} className="band" x={sx(b.from)} y={m.t} width={Math.max(0, sx(b.to) - sx(b.from))} height={h - m.t - m.b} />
        ))}
        {ids.map((id, r) => {
          const y = m.t + r * (rowH + 4);
          return (
            <g key={id}>
              <text className="tick" x={m.l - 6} y={y + rowH / 2 + 4} textAnchor="end">{id}</text>
              {segs[id].map((sg, k) => {
                const a = activityOf(sg.status);
                const x = sx(sg.from) + 1, w = Math.max(1, sx(sg.to) - sx(sg.from) - 2);
                return (
                  <rect key={k} x={x} y={y} width={w} height={rowH} rx={3} fill={a.color} className="bar-mark"
                    onPointerEnter={() => setTip({ x: x + w / 2, y, text: `${id}: ${a.label}${sg.job ? ` · ${sg.job}` : ''}`, sub: `t${sg.from}–t${sg.to}` })}
                    onPointerLeave={() => setTip(null)} />
                );
              })}
            </g>
          );
        })}
        {xt.map((v) => <text key={v} className="tick" x={sx(v)} y={h - 6} textAnchor="middle">t{v}</text>)}
      </svg>
      {tip && (
        <div className="tooltip" style={{ left: Math.max(0, Math.min(width - 190, tip.x - 80)), top: Math.max(0, tip.y - 52) }}>
          <div className="tt-row"><strong>{tip.text}</strong></div>
          <div className="tt-head">{tip.sub}</div>
        </div>
      )}
    </div>
  );
}

export function ActivityLegend() {
  return <>{ACTIVITY.map((a) => <LegendItem key={a.key} color={a.color} label={a.label} line={false} />)}<LegendItem color="var(--band)" label="Night" line={false} /></>;
}
