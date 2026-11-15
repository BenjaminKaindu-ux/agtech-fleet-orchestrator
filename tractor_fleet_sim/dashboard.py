"""Local, dependency-free dashboard for the tractor fleet simulator."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
import argparse
import json

from farm_sim import FarmSimulator, first_come_first_served_plan, priority_nearest_plan


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Run the Fleet | Farm Operations</title>
<style>
:root{--ink:#17231c;--muted:#68756c;--paper:#f2f5ef;--card:#fff;--line:#dce4da;--green:#247348;--lime:#d9edb4;--red:#b83f31;--amber:#b56a16;--blue:#3479a5}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}header{background:#183b2b;color:#f7fbf5;padding:18px 26px;display:flex;justify-content:space-between;align-items:center;gap:16px}h1{font-size:22px;margin:0}header p{margin:3px 0 0;color:#c7d8ca}.wrap{max-width:1440px;margin:auto;padding:18px}.toolbar,.metrics,.layout{display:grid;gap:12px}.toolbar{grid-template-columns:repeat(auto-fit,minmax(130px,1fr));margin-bottom:14px}.metrics{grid-template-columns:repeat(auto-fit,minmax(140px,1fr));margin-bottom:14px}.layout{grid-template-columns:minmax(420px,1.25fr) minmax(350px,1fr)}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:15px;box-shadow:0 2px 8px #183b2b0c}.metric{padding:12px 14px}.metric b{display:block;font-size:22px}.metric span,.subtle{color:var(--muted);font-size:12px}.card h2{font-size:16px;margin:0 0 12px}.btnrow{display:flex;flex-wrap:wrap;gap:8px}button,select,input{font:inherit}button{border:0;background:var(--green);color:white;border-radius:8px;padding:9px 12px;cursor:pointer}button.secondary{background:#e7eee7;color:#20362a}button.warn{background:#a63e31}button:hover{filter:brightness(.96)}.map{display:grid;gap:4px;margin:10px 0 16px;overflow:auto}.cell{min-width:38px;height:38px;border:1px solid #e3e9e1;border-radius:6px;background:#f8faf7;position:relative;display:flex;align-items:center;justify-content:center;font-weight:650;font-size:11px}.cell.blocked{background:#414a45;color:white}.cell.base{background:#d7ead8}.cell.field{background:#e6f0d7;color:#365b31}.tractor{position:absolute;right:1px;bottom:0;border-radius:5px;padding:0 3px;color:#fff;background:#286b48;font-size:10px}.tractor.broken{background:var(--red)}.tractor.traveling,.tractor.to_refuel{background:var(--blue)}.tractor.working{background:#b36d12}table{border-collapse:collapse;width:100%;font-size:12px}th,td{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:650}.bar{height:7px;background:#e8eee7;border-radius:7px;overflow:hidden;min-width:70px}.bar i{height:100%;display:block;background:#5c9d63}.scroll{max-height:275px;overflow:auto}.event{padding:7px 2px;border-bottom:1px solid var(--line);font-size:12px}.event time{color:var(--muted);margin-right:7px}.event.breakdown,.event.deadline_missed,.event.plan_rejected{color:var(--red)}.event.job_completed,.event.plan_accepted{color:var(--green)}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}.control{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}.control select,.control input,textarea{border:1px solid var(--line);border-radius:7px;padding:8px;background:#fff;color:var(--ink)}textarea{width:100%;min-height:90px;font:12px ui-monospace,monospace}.message{margin-top:8px;min-height:20px;color:var(--muted);font-size:12px}.pill{border-radius:99px;padding:2px 7px;background:#edf1ed;font-size:11px}.status-broken,.status-stranded,.status-blocked{color:var(--red);font-weight:700}.status-working{color:var(--amber);font-weight:700}.status-idle{color:var(--green)}@media(max-width:920px){.layout{grid-template-columns:1fr}}@media(max-width:520px){.wrap{padding:10px}.grid2{grid-template-columns:1fr}header{padding:14px}.cell{min-width:30px;height:32px}}
</style></head><body>
<header><div><h1>Run the Fleet</h1><p id="subtitle">Offline farm fleet coordination simulator</p></div><div id="clock"></div></header>
<main class="wrap"><section class="toolbar">
<button onclick="tick(1)">Advance 1 tick</button><button class="secondary" onclick="tick(4)">Advance 1 hour</button><button onclick="plan('priority-nearest')">Demo dispatch</button><button class="secondary" onclick="plan('fcfs')">FCFS baseline</button><button class="warn" onclick="resetSim()">Reset scenario</button>
</section><section id="metrics" class="metrics"></section>
<section class="layout"><div>
<article class="card"><h2>Farm map <span class="subtle">· each cell is one travel step</span></h2><div id="map" class="map"></div><div class="subtle">Base = green · field = pale green · blocked route = charcoal · tractor labels match table</div></article>
<article class="card" style="margin-top:12px"><h2>Fleet status</h2><div class="scroll"><table><thead><tr><th>Unit</th><th>Status / assignment</th><th>Fuel</th><th>Position</th><th>Implements</th></tr></thead><tbody id="tractors"></tbody></table></div></article>
<article class="card" style="margin-top:12px"><h2>Jobs and field progress</h2><div class="scroll"><table><thead><tr><th>Job / field</th><th>Work</th><th>Progress</th><th>Priority</th><th>Deadline</th><th>Status</th></tr></thead><tbody id="jobs"></tbody></table></div></article>
</div><div>
<article class="card"><h2>Trigger an event</h2><div class="control"><select id="tractorPick"></select><button class="warn" onclick="eventBreakdown()">Break down tractor</button><button class="secondary" onclick="eventRepair()">Repair now</button></div><div class="control"><input id="bx" type="number" min="0" placeholder="x" style="width:64px"><input id="by" type="number" min="0" placeholder="y" style="width:64px"><button class="secondary" onclick="eventBlock()">Block cell</button><button class="secondary" onclick="eventUnblock()">Reopen cell</button></div><div class="control"><select id="jobPick"></select><select id="priorityPick"><option value="1">1 · urgent</option><option value="2">2</option><option value="3" selected>3</option><option value="4">4</option><option value="5">5 · low</option></select><button class="secondary" onclick="eventPriority()">Change priority</button></div><div id="message" class="message"></div></article>
<article class="card" style="margin-top:12px"><h2>Dispatch plan (Smolagents can call the same API)</h2><p class="subtle">A plan is checked for implement, fuel, route, deadline, field conflicts, and current state version before it is committed.</p><textarea id="planJson">[{"tractor_id":"T1","job_id":"J1"}]</textarea><div class="control"><button class="secondary" onclick="validatePlan()">Validate only</button><button onclick="commitPlan()">Validate and dispatch</button></div></article>
<article class="card" style="margin-top:12px"><h2>Event log</h2><div id="events" class="scroll"></div></article>
</div></section></main>
<script>
let lastState=null;const $=id=>document.getElementById(id);async function api(path,body){const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});const d=await r.json();if(!r.ok)throw Error(d.error||r.statusText);return d}function say(s){$('message').textContent=s}
async function refresh(){try{lastState=await api('/api/state');render(lastState)}catch(e){say(e.message)}}function render(s){$('subtitle').textContent=`${s.scenario_name} · tick ${s.tick} (${s.time_minutes} simulated minutes) · state version ${s.version}`;$('clock').textContent=`${s.tick_minutes} min / tick`;
const m=s.metrics;$('metrics').innerHTML=[['Completed jobs',`${m.completed_jobs} / ${s.jobs.length}`],['Acres completed',`${m.acres_completed} / ${m.total_job_acres}`],['On-time jobs',m.on_time_jobs],['Travel distance',`${m.travel_cells} cells`],['Fuel used',`${m.fuel_used_liters} L`],['Idle time',`${m.idle_tractor_ticks} tractor-ticks`],['Replans',m.replans]].map(x=>`<div class="card metric"><b>${x[1]}</b><span>${x[0]}</span></div>`).join('');
const w=s.grid.width,h=s.grid.height;$('map').style.gridTemplateColumns=`repeat(${w},minmax(30px,1fr))`;let cells=[];const fields=Object.fromEntries(s.fields.map(f=>[f.position.join(','),f]));const tractors=Object.fromEntries(s.tractors.map(t=>[t.position.join(','),t]));const blocks=new Set(s.grid.blocked_cells.map(p=>p.join(',')));for(let y=h-1;y>=0;y--)for(let x=0;x<w;x++){let k=`${x},${y}`,f=fields[k],t=tractors[k],cls=blocks.has(k)?'blocked':k===s.grid.base.join(',')?'base':f?'field':'';cells.push(`<div class="cell ${cls}" title="${f?`${f.id}: ${f.name}`:k}">${blocks.has(k)?'×':f?f.id:k===s.grid.base.join(',')?'BASE':''}${t?`<span class="tractor ${t.status}" title="${t.id} ${t.status}">${t.id}</span>`:''}</div>`)}$('map').innerHTML=cells.join('');
$('tractors').innerHTML=s.tractors.map(t=>`<tr><td><b>${t.id}</b></td><td><span class="status-${t.status}">${t.status}</span><br>${t.job_id||'—'}${t.waiting_ticks?` · waiting ${t.waiting_ticks}`:''}</td><td>${t.fuel_liters} / ${t.tank_liters} L<div class="bar"><i style="width:${t.fuel_pct}%"></i></div></td><td>${t.position.join(', ')}</td><td>${t.implements.join(', ')}</td></tr>`).join('');$('jobs').innerHTML=s.jobs.map(j=>`<tr><td><b>${j.id}</b> / ${j.field_id}<br><span class="subtle">${j.job_type} · ${j.required_implement}</span></td><td>${j.completed_acres}/${j.acres} ac</td><td><div class="bar"><i style="width:${j.progress_pct}%"></i></div>${j.progress_pct}%</td><td>${j.priority}</td><td>t${j.deadline_tick}</td><td>${j.status}${j.assigned_tractor_id?` · ${j.assigned_tractor_id}`:''}</td></tr>`).join('');
$('tractorPick').innerHTML=s.tractors.map(t=>`<option value="${t.id}">${t.id} · ${t.status}</option>`).join('');$('jobPick').innerHTML=s.jobs.map(j=>`<option value="${j.id}">${j.id} · ${j.status}</option>`).join('');$('events').innerHTML=s.recent_events.slice().reverse().map(e=>`<div class="event ${e.type}"><time>t${e.tick}</time><b>${e.type.replaceAll('_',' ')}</b> — ${e.message}</div>`).join('')||'<div class="subtle">No events yet.</div>'}
async function tick(n){try{await api('/api/tick',{steps:n});await refresh()}catch(e){say(e.message)}}async function plan(policy){try{const r=await api('/api/plan',{policy});say(r.message);await refresh()}catch(e){say(e.message)}}async function resetSim(){if(!confirm('Reset to the original scenario? Current progress and events will be cleared.'))return;await api('/api/reset',{});await refresh();say('Scenario reset.')}async function eventCall(name,data){try{const r=await api('/api/event',{name,...data});say(r.ok?`${name.replaceAll('_',' ')} recorded.`:(r.reason||'Event rejected'));await refresh()}catch(e){say(e.message)}}function eventBreakdown(){eventCall('breakdown',{tractor_id:$('tractorPick').value,repair_after_ticks:5})}function eventRepair(){eventCall('repair',{tractor_id:$('tractorPick').value})}function eventBlock(){eventCall('block_cell',{position:[Number($('bx').value),Number($('by').value)]})}function eventUnblock(){eventCall('unblock_cell',{position:[Number($('bx').value),Number($('by').value)]})}function eventPriority(){eventCall('change_priority',{job_id:$('jobPick').value,priority:Number($('priorityPick').value)})}async function validatePlan(){try{const r=await api('/api/dispatch',{assignments:JSON.parse($('planJson').value),expected_version:lastState.version,commit:false});say(r.valid?`Valid plan · ${r.estimates.length} assignment(s)`:r.errors.join(' | '))}catch(e){say(e.message)}}async function commitPlan(){try{const r=await api('/api/dispatch',{assignments:JSON.parse($('planJson').value),expected_version:lastState.version,commit:true});say(r.accepted?`Plan committed at state version ${r.state_version}`:r.errors.join(' | '));await refresh()}catch(e){say(e.message)}}refresh();setInterval(refresh,2500);
</script></body></html>'''


def make_handler(sim: FarmSimulator):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: object, content_type: str = "application/json") -> None:
            raw = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1_000_000:
                raise ValueError("Request body too large")
            raw = self.rfile.read(length)
            return json.loads(raw or b"{}")

        def do_GET(self):
            route = urlparse(self.path).path
            if route == "/":
                self._send(200, PAGE, "text/html")
            elif route == "/api/state":
                state = sim.fleet_state()
                state["scenario_name"] = sim.scenario.get("name", "Farm scenario")
                self._send(200, state)
            else:
                self._send(404, {"error": "Not found"})

        def do_POST(self):
            try:
                body = self._body()
                route = urlparse(self.path).path
                if route == "/api/tick":
                    steps = int(body.get("steps", 1))
                    if not 1 <= steps <= 500:
                        raise ValueError("steps must be between 1 and 500")
                    self._send(200, sim.advance(steps))
                elif route == "/api/reset":
                    sim.__init__(sim.scenario)
                    self._send(200, {"ok": True})
                elif route == "/api/plan":
                    policy = body.get("policy")
                    planner = {"fcfs": first_come_first_served_plan,
                               "priority-nearest": priority_nearest_plan}.get(policy)
                    if planner is None:
                        raise ValueError("policy must be fcfs or priority-nearest")
                    assignments = planner(sim)
                    result = sim.commit_plan(assignments, expected_version=sim.version) if assignments else {"accepted": False, "errors": ["No feasible assignments right now"]}
                    result["message"] = (f"{policy} assigned {len(assignments)} job(s)." if result.get("accepted")
                                          else "; ".join(result.get("errors", [])))
                    self._send(200, result)
                elif route == "/api/dispatch":
                    assignments = body.get("assignments", [])
                    version = body.get("expected_version")
                    result = (sim.commit_plan(assignments, version) if body.get("commit")
                              else sim.validate_plan(assignments, version))
                    self._send(200, result)
                elif route == "/api/event":
                    name = str(body.pop("name", ""))
                    result = sim.event(name, **body)
                    self._send(200, result)
                else:
                    self._send(404, {"error": "Not found"})
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                self._send(400, {"error": str(exc)})

        def log_message(self, fmt, *args):
            return

    return Handler


def serve(scenario_path: str | Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    sim = FarmSimulator.from_file(scenario_path)
    server = ThreadingHTTPServer((host, port), make_handler(sim))
    print(f"Dashboard running at http://{host}:{port} · scenario: {sim.scenario.get('name', 'Farm')}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default=str(Path(__file__).with_name("scenario.json")))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serve(args.scenario, args.host, args.port)
