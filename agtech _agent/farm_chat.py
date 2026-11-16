"""Farmer chat: plain-language questions about the live fleet, answered from the
simulator's current state (authoritative) and the learning memory.

Works fully offline with a grounded rule-based answerer. If HF_TOKEN is set (and
FARM_CHAT_LLM != "0") the grounded draft plus a compact state summary is handed
to an LLM to phrase a friendlier answer; on any failure we fall back to rules.

The chat never changes the farm by itself. When a farmer reports something
("T2 broke down", "rush the F3 planting") it returns a *proposed* operator event
that the UI shows as a button; the farmer confirms and the UI calls /events.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

SPRAY_MIN_MPH, SPRAY_MAX_MPH = 3.0, 10.0
QUESTION_START = re.compile(r"^\s*(why|what|when|where|how|is|are|was|were|did|does|do|can|could|should|will|would|which|who)\b", re.I)

HELP = ("I can answer questions about the farm right now. Try:\n"
        "• \"How are we doing?\"  • \"What is the sprayer doing?\"  • \"Can I spray today?\"\n"
        "• \"What's late?\"  • \"Why did the agent send P1 to F4?\"  • \"What have you learned?\"\n"
        "You can also tell me what's happening in the field, e.g. \"T2 broke down\", "
        "\"rush J3\" or \"T1 needs fuel\", and I'll offer to update the plan.")

SUGGESTIONS = ["How are we doing?", "Can I spray today?", "What's late?", "What have you learned?"]


# --------------------------------------------------------------------------- helpers
def _clock(state: dict[str, Any], tick: int | None) -> str:
    if tick is None:
        return "—"
    cond = state.get("conditions") or {}
    now = int(state.get("tick", 0))
    if cond.get("enabled") and "hour" in cond:
        total = (int(cond.get("day", 1)) - 1) * 24 + float(cond["hour"]) + (tick - now) * state.get("tick_minutes", 30) / 60
        day, hour = int(total // 24) + 1, total % 24
        return f"day {day} {int(hour):02d}:{int(round((hour % 1) * 60)) % 60:02d}"
    return f"t{tick}"


def _hours(state: dict[str, Any], ticks: float) -> str:
    h = ticks * state.get("tick_minutes", 30) / 60
    return f"{h:.0f} h" if h >= 3 else f"{h * 60:.0f} min"


def _find_ids(text: str, ids: list[str]) -> list[str]:
    toks = set(re.findall(r"[a-z]+\d+", text.lower()))
    return [i for i in ids if i.lower() in toks]


def _has(text: str, *words: str) -> bool:
    return any(re.search(rf"\b{w}", text) for w in words)


def _tractor_line(state: dict[str, Any], t: dict[str, Any]) -> str:
    jobs = {j["id"]: j for j in state.get("jobs", [])}
    blocks = (state.get("conditions") or {}).get("job_blocks") or {}
    imp = "/".join(t.get("implements") or []) or "no implement"
    fuel = f"fuel {t.get('fuel_pct', 0):.0f}%"
    st, jid = t.get("status"), t.get("job_id")
    if st == "broken":
        when = f", back about {_clock(state, t['repair_at_tick'])}" if t.get("repair_at_tick") else ", waiting for repair"
        return f"{t['id']} ({imp}) is broken down at {tuple(t['position'])}{when}."
    if st == "working" and jid in jobs:
        j = jobs[jid]
        return (f"{t['id']} ({imp}) is working: {j['job_type']} on {j['field_id']} ({jid}), "
                f"{j['progress_pct']:.0f}% done, {j['remaining_acres']:.0f} ac left · {fuel}.")
    if st in ("traveling", "moving") or t.get("route"):
        dest = f" to {jobs[jid]['field_id']} for {jid} ({jobs[jid]['job_type']})" if jid in jobs else ""
        return f"{t['id']} ({imp}) is driving{dest}, {len(t.get('route') or [])} cells to go · {fuel}."
    if st == "refueling":
        return f"{t['id']} ({imp}) is refueling at the yard · {fuel}."
    why = ""
    if jid in blocks:
        why = f" — {jid} is on hold: {blocks[jid]}"
    elif jid in jobs:
        why = f", assigned to {jid} ({jobs[jid]['job_type']} on {jobs[jid]['field_id']})"
    else:
        waiting = [f"{j['id']} ({b})" for j in state.get("jobs", []) for jj, b in blocks.items()
                   if jj == j["id"] and j["required_implement"] in (t.get("implements") or [])]
        why = f" — its work is on hold: {', '.join(waiting[:2])}" if waiting else " with nothing it can do right now"
    return f"{t['id']} ({imp}) is idle at {tuple(t['position'])}{why} · {fuel}."


def _job_line(state: dict[str, Any], j: dict[str, Any]) -> str:
    now = int(state.get("tick", 0))
    blocks = (state.get("conditions") or {}).get("job_blocks") or {}
    head = f"{j['id']} {j['job_type']} on {j['field_id']} ({j['acres']:.0f} ac, priority {j['priority']})"
    if j["status"] == "completed":
        late = j["completed_tick"] > j["deadline_tick"]
        return f"{head}: finished {_clock(state, j['completed_tick'])}{' — LATE' if late else ', on time'}."
    who = f", {j['assigned_tractor_id']} on it" if j.get("assigned_tractor_id") else ", no machine assigned yet"
    hold = f" On hold: {blocks[j['id']]}." if j["id"] in blocks else ""
    left = j["deadline_tick"] - now
    due = (f"due {_clock(state, j['deadline_tick'])} ({_hours(state, left)} left)" if left >= 0
           else f"was due {_clock(state, j['deadline_tick'])} — overdue")
    return f"{head}: {j['progress_pct']:.0f}% done{who}; {due}.{hold}"


def _spray_window(state: dict[str, Any]) -> tuple[str, list[str]]:
    cond = state.get("conditions") or {}
    if not cond.get("enabled"):
        return "This scenario doesn't model weather, so spraying is never held up here.", []
    now_ok = cond.get("daylight") and SPRAY_MIN_MPH <= cond.get("wind_mph", 0) <= SPRAY_MAX_MPH and not cond.get("rain_mm_hr")
    facts = [f"now {cond.get('clock')}: wind {cond.get('wind_mph')} mph, rain {cond.get('rain_mm_hr')} mm/h, "
             f"{'daylight' if cond.get('daylight') else 'dark'}"]
    if now_ok:
        msg = f"Yes — conditions are sprayable right now ({cond['wind_mph']:.0f} mph wind, dry, daylight)."
    else:
        why = ("it's dark" if not cond.get("daylight") else "it's raining" if cond.get("rain_mm_hr")
               else f"wind is {cond.get('wind_mph', 0):.0f} mph (need {SPRAY_MIN_MPH:.0f}–{SPRAY_MAX_MPH:.0f} mph"
                    + (" — too calm, inversion/drift risk)" if cond.get("wind_mph", 0) < SPRAY_MIN_MPH else ")"))
        msg = f"Not right now — {why}."
    window, start = [], None
    for f in cond.get("forecast") or []:
        ok = f.get("daylight") and SPRAY_MIN_MPH <= f.get("wind_mph", 0) <= SPRAY_MAX_MPH and not f.get("rain_mm_hr")
        if ok and start is None:
            start = f["tick"]
        if not ok and start is not None:
            window.append((start, f["tick"]))
            start = None
    if start is not None:
        window.append((start, None))
    future = [w for w in window if w[0] > state.get("tick", 0) or not now_ok]
    if future and not now_ok:
        a, b = future[0]
        msg += f" Next spray window in the forecast: {_clock(state, a)}" + (f" to {_clock(state, b)}." if b else " onward.")
    elif now_ok and window and window[0][1]:
        msg += f" The window closes around {_clock(state, window[0][1])}."
    elif not future and not now_ok:
        msg += " No sprayable window in the forecast I can see."
    return msg, facts


# --------------------------------------------------------------------------- intents
def _status(state: dict[str, Any]) -> str:
    m = state.get("metrics", {})
    trs = state.get("tractors", [])
    counts: dict[str, int] = {}
    for t in trs:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    cond = state.get("conditions") or {}
    when = f"It's {_clock(state, state.get('tick'))}" if cond.get("enabled") else f"Tick {state.get('tick')}"
    s = (f"{when} on \"{(state.get('scenario') or {}).get('title') or state.get('scenario_name')}\". "
         f"{m.get('completed_jobs', 0)} of {len(state.get('jobs', []))} jobs done "
         f"({m.get('acres_completed', 0):.0f} of {m.get('total_job_acres', 0):.0f} acres), "
         f"{m.get('late_completed_jobs', 0)} finished late. Machines: "
         + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) + ".")
    holds = cond.get("job_blocks") or {}
    if holds:
        reasons: dict[str, list[str]] = {}
        for jid, r in holds.items():
            reasons.setdefault(r, []).append(jid)
        s += " On hold: " + "; ".join(f"{', '.join(v)} ({r})" for r, v in list(reasons.items())[:3]) + "."
    if state.get("done"):
        s += " The scenario has finished."
    return s


def _late(state: dict[str, Any]) -> str:
    now = int(state.get("tick", 0))
    out = []
    for j in state.get("jobs", []):
        if j["status"] == "completed":
            if j["completed_tick"] > j["deadline_tick"]:
                out.append((0, f"• {j['id']} {j['job_type']} on {j['field_id']} finished late."))
            continue
        left = j["deadline_tick"] - now
        need_ticks = None
        if j.get("assigned_tractor_id"):
            for d in reversed(state.get("decisions", [])):
                a = next((a for a in d.get("assignments", []) if a.get("job_id") == j["id"]), None)
                if a and a.get("estimated_completion_tick"):
                    need_ticks = a["estimated_completion_tick"]
                    break
        if left < 0:
            out.append((1, f"• {j['id']} {j['job_type']} on {j['field_id']} is overdue ({j['progress_pct']:.0f}% done)."))
        elif need_ticks and need_ticks > j["deadline_tick"]:
            out.append((2, f"• {j['id']} {j['job_type']} on {j['field_id']}: planned finish {_clock(state, need_ticks)} "
                           f"is after the deadline {_clock(state, j['deadline_tick'])}."))
        elif left < 24 and j["progress_pct"] < 50:
            out.append((3, f"• {j['id']} {j['job_type']} on {j['field_id']}: {_hours(state, left)} left and only "
                           f"{j['progress_pct']:.0f}% done."))
    if not out:
        return "Nothing is late or at risk right now — every open job's plan finishes before its deadline."
    return "Here's what's late or at risk:\n" + "\n".join(x for _, x in sorted(out)[:6])


def _why(state: dict[str, Any], ids: list[str]) -> str:
    decs = state.get("decisions") or []
    if not decs:
        return "The planner hasn't made a dispatch decision yet in this run — step the simulation forward first."
    pick = None
    for d in reversed(decs):
        if not ids or any(a.get("tractor_id") in ids or a.get("job_id") in ids for a in d.get("assignments", [])):
            pick = d
            break
    if pick is None:
        return f"I don't see a recent decision involving {', '.join(ids)}."
    lines = [f"Decision at {_clock(state, pick['tick'])} (trigger: {pick.get('trigger')}):"]
    for a in pick.get("assignments", [])[:4]:
        if ids and a.get("tractor_id") not in ids and a.get("job_id") not in ids:
            continue
        lines.append(f"• {a['tractor_id']} → {a['job_id']}: {a.get('travel_cells', 0)} cells to drive, "
                     f"~{a.get('work_hours', 0):.1f} h of work, est. done {_clock(state, a.get('estimated_completion_tick'))}, "
                     f"needs {a.get('fuel_needed_liters', 0):.0f} L of {a.get('fuel_available_liters', 0):.0f} L.")
    expl = (pick.get("explanation") or "").strip()
    if expl:
        lines.append("Planner's note: " + re.sub(r"\s+", " ", expl)[:400])
    return "\n".join(lines)


def _learned(learning: dict[str, Any], state: dict[str, Any]) -> str:
    sid = (state.get("scenario") or {}).get("id")
    lines = []
    sc = ((learning.get("facts") or {}).get("scenarios") or {}).get(sid)
    if sc:
        best = max(sc["by_policy"].items(), key=lambda kv: kv[1]["mean_score"])
        lines.append(f"From {sc['runs']} past runs of this scenario, \"{best[0]}\" scores best on average "
                     f"({best[1]['mean_score']:.0f}, {best[1]['mean_jobs']:.1f} jobs done).")
    skills = [s for s in learning.get("skills", []) if s.get("scenario_id") == sid and s.get("status") == "proven"]
    if skills:
        s = skills[-1]
        lines.append(f"Proven skill for it: {s['name']} — {s.get('description', '')}")
    ins = [i for i in learning.get("insights", []) if i.get("scenario_id") == sid] or learning.get("insights", [])
    for i in ins[:3]:
        lines.append(f"• {(i.get('interpretation') or {}).get('lesson', '')} (candidate, {i.get('observations', 1)} obs.)")
    if not lines:
        return "I haven't learned anything about this scenario yet. Run it a few times in the Simulation Lab and I'll start keeping notes."
    lines.append(f"Overall I've recorded {learning.get('episode_count', 0)} runs. Lessons stay 'candidate' until a farmer reviews them.")
    return "\n".join(lines)


def _fuel(state: dict[str, Any]) -> str:
    trs = sorted(state.get("tractors", []), key=lambda t: t.get("fuel_pct", 100))
    low = [t for t in trs if t.get("fuel_pct", 100) < 30]
    s = "Fuel: " + ", ".join(f"{t['id']} {t['fuel_pct']:.0f}%" for t in trs) + "."
    s += (" Low: " + ", ".join(t["id"] for t in low) + " — say \"refuel " + low[0]["id"] + "\" to send it to the yard."
          if low else " Nobody is low.")
    s += f" Fleet has burned {state.get('metrics', {}).get('fuel_used_liters', 0):.0f} L so far."
    return s


def _weather(state: dict[str, Any]) -> str:
    cond = state.get("conditions") or {}
    if not cond.get("enabled"):
        return "This scenario doesn't model weather."
    s = (f"Now ({cond.get('clock')}, day {cond.get('day')}): wind {cond.get('wind_mph', 0):.0f} mph, "
         f"rain {cond.get('rain_mm_hr', 0)} mm/h, {'daylight' if cond.get('daylight') else 'dark'}.")
    rain = [f for f in cond.get("forecast") or [] if f.get("rain_mm_hr")]
    s += (f" Rain in the forecast from {_clock(state, rain[0]['tick'])}." if rain else " No rain in the forecast window.")
    wet = {f: v["wet_until"] for f, v in (cond.get("soils") or {}).items() if v.get("wet_until")}
    if wet:
        s += " Too wet to work: " + ", ".join(f"{f} until {_clock(state, t)}" for f, t in wet.items()) + "."
    return s


def _breakdowns(state: dict[str, Any]) -> str:
    broken = [t for t in state.get("tractors", []) if t["status"] == "broken"]
    ev = [e for e in state.get("recent_events", []) if "break" in json.dumps(e).lower()]
    if not broken and not ev:
        return "No machines are broken down."
    s = " ".join(_tractor_line(state, t) for t in broken) or "Everything is running right now."
    s += f" Breakdowns this run: {state.get('metrics', {}).get('breakdowns', 0)}."
    return s


# --------------------------------------------------------------------------- actions
def _propose_action(text: str, state: dict[str, Any], tractors: list[str], jobs: list[str]) -> dict[str, Any] | None:
    if QUESTION_START.match(text) or text.strip().endswith("?"):
        return None
    t = text.lower()
    by_id = {x["id"]: x for x in state.get("tractors", [])}
    if tractors and _has(t, "broke", "broken", "break", "down\\b", "quit", "won't start", "flat", "hydraulic"):
        tid = tractors[0]
        if by_id.get(tid, {}).get("status") == "broken":
            return None
        return {"name": "breakdown", "payload": {"tractor_id": tid, "repair_after_ticks": 8},
                "label": f"Mark {tid} broken (repair ~{_hours(state, 8)}) and replan"}
    if tractors and _has(t, "fixed", "repaired", "running again", "back up", "repair"):
        return {"name": "repair", "payload": {"tractor_id": tractors[0]}, "label": f"Mark {tractors[0]} repaired"}
    if tractors and _has(t, "refuel", "fuel", "gas", "diesel", "top off"):
        return {"name": "refuel", "payload": {"tractor_id": tractors[0], "target_pct": 90},
                "label": f"Send {tractors[0]} to refuel to 90%"}
    if jobs and _has(t, "priorit", "rush", "urgent", "first", "asap", "hurry", "lower", "later", "wait"):
        m = re.search(r"priority\s*(\d)", t)
        pr = int(m.group(1)) if m else (4 if _has(t, "lower", "later", "wait", "not urgent") else 1)
        pr = min(5, max(1, pr))
        return {"name": "change_priority", "payload": {"job_id": jobs[0], "priority": pr},
                "label": f"Set {jobs[0]} to priority {pr} ({'highest' if pr == 1 else 'lower'}) and replan"}
    m = re.search(r"\(?\s*(\d+)\s*,\s*(\d+)\s*\)?", t)
    if m and _has(t, "block", "closed", "flood", "washed", "washout", "mud", "gate"):
        pos = [int(m.group(1)), int(m.group(2))]
        if _has(t, "open", "unblock", "clear"):
            return {"name": "unblock_cell", "payload": {"position": pos}, "label": f"Reopen cell {tuple(pos)}"}
        return {"name": "block_cell", "payload": {"position": pos}, "label": f"Block cell {tuple(pos)} and reroute"}
    return None


# --------------------------------------------------------------------------- main
def answer(message: str, state: dict[str, Any], learning: dict[str, Any]) -> dict[str, Any]:
    text = (message or "").strip()[:500]
    t = text.lower()
    tr_ids = [x["id"] for x in state.get("tractors", [])]
    job_ids = [x["id"] for x in state.get("jobs", [])]
    field_ids = [x["id"] for x in state.get("fields", [])]
    tractors, jobs, fields = _find_ids(t, tr_ids), _find_ids(t, job_ids), _find_ids(t, field_ids)
    by_type = not tractors
    if not tractors:  # "the sprayer", "balers", "combine" → machines carrying that implement
        tractors = [x["id"] for x in state.get("tractors", [])
                    if any(re.search(rf"\b{re.escape(re.split(r'[-/ ]', i.lower())[0])}s?\b", t)
                           for i in x.get("implements") or [])]
    # "the F3 planting" → open job on F3 matching the operation word, if any.
    if fields and not jobs:
        cand = [j for j in state.get("jobs", []) if j["field_id"] in fields and j["status"] != "completed"]
        cand = [j for j in cand if any(w in t for w in j["job_type"].lower().split())] or cand
        jobs = [j["id"] for j in cand[:1]]

    known = {i.lower() for i in tr_ids + job_ids + field_ids}
    unknown = [x.upper() for x in re.findall(r"\b[a-z]{1,2}\d+\b", t) if x not in known]
    action = _propose_action(text, state, tractors, jobs)
    if action and by_type and len(tractors) > 1 and "tractor_id" in action["payload"]:
        return {"reply": f"Which one — {' or '.join(tractors)}? Tell me e.g. \"{tractors[0]} broke down\".",
                "intent": "clarify", "source": "rules", "action": None, "tick": state.get("tick"),
                "suggestions": [f"What is {x} doing?" for x in tractors][:4]}
    intent, reply = "status", ""
    if unknown and not (tractors or jobs or fields):
        intent = "unknown"
        reply = (f"I don't see {', '.join(unknown)} on this farm. Machines: {', '.join(tr_ids)}; "
                 f"fields: {', '.join(field_ids)}; jobs: {', '.join(job_ids)}.")
    elif not text or _has(t, "help", "what can you", "hello", "hi\\b", "hey"):
        intent, reply = "help", HELP
    elif action:
        intent = "action"
        reply = (f"Got it. I can update the plan: {action['label']}. Press the button to confirm — "
                 "the agent will replan around it.")
    elif _has(t, "why", "reason", "decid", "chose", "explain"):
        intent, reply = "why", _why(state, tractors or jobs)
    elif tractors:
        intent = "tractor"
        reply = "\n".join(_tractor_line(state, x) for x in state["tractors"] if x["id"] in tractors)
    elif _has(t, "spray", "drift", "wind"):
        intent, reply = "spray", _spray_window(state)[0]
    elif jobs or fields:
        intent = "job"
        js = [j for j in state.get("jobs", []) if j["id"] in jobs or j["field_id"] in fields]
        reply = "\n".join(_job_line(state, j) for j in js) or "No jobs on that field."
        if fields:
            soil = ((state.get("conditions") or {}).get("soils") or {})
            for f in fields:
                info = soil.get(f)
                fname = next((x["name"] for x in state["fields"] if x["id"] == f), f)
                if info:
                    wet = f", too wet until {_clock(state, info['wet_until'])}" if info.get("wet_until") else ", workable"
                    reply = f"{f} — {fname}: {info['soil']} soil{wet}.\n" + reply
    elif _has(t, "late", "behind", "deadline", "risk", "overdue", "on time", "miss"):
        intent, reply = "late", _late(state)
    elif _has(t, "rain", "weather", "forecast", "wet", "mud", "soil", "dry"):
        intent, reply = "weather", _weather(state)
    elif _has(t, "fuel", "diesel", "gas"):
        intent, reply = "fuel", _fuel(state)
    elif _has(t, "broke", "breakdown", "repair", "mechanic"):
        intent, reply = "breakdowns", _breakdowns(state)
    elif _has(t, "learn", "lesson", "memory", "remember", "best", "strategy", "skill", "improv", "history", "past"):
        intent, reply = "learned", _learned(learning, state)
    elif _has(t, "plant", "harvest", "combine", "bale", "cut", "hay", "till", "cultivat", "corn", "soy", "wheat", "alfalfa"):
        intent = "operation"
        js = [j for j in state.get("jobs", []) if any(w[:4] in t for w in re.findall(r"[a-z]{4,}", j["job_type"].lower()))]
        reply = "\n".join(_job_line(state, j) for j in js) or _status(state)
    else:
        intent, reply = "status", _status(state)
        if not _has(t, "how", "status", "doing", "going", "progress", "update", "summary", "overview"):
            reply = "Here's where things stand. " + reply + "\n(Ask \"help\" for things I can answer.)"

    reply = re.sub(r"\bt(\d+)\b", lambda m: _clock(state, int(m.group(1))), reply)
    out = {"reply": reply, "intent": intent, "source": "rules", "action": action,
           "tick": state.get("tick"), "suggestions": _suggest(intent, state)}
    if intent not in ("help", "action", "unknown"):
        llm = _llm_rephrase(text, reply, state)
        if llm:
            out.update(reply=llm, source="llm", grounded_draft=reply)
    return out


def _suggest(intent: str, state: dict[str, Any]) -> list[str]:
    trs = state.get("tractors", [])
    busy = next((t["id"] for t in trs if t["status"] == "working"), trs[0]["id"] if trs else "T1")
    pool = ["How are we doing?", f"What is {busy} doing?", "What's late?", "Can I spray today?",
            f"Why did the agent pick {busy}?", "What have you learned?", "Weather?", "Fuel?"]
    return [p for p in pool if not p.lower().startswith(intent[:4])][:4]


# --------------------------------------------------------------------------- optional LLM
def _compact_state(state: dict[str, Any]) -> dict[str, Any]:
    cond = state.get("conditions") or {}
    return {
        "time": _clock(state, state.get("tick")), "scenario": (state.get("scenario") or {}).get("title"),
        "tractors": [{k: t.get(k) for k in ("id", "status", "implements", "fuel_pct", "job_id", "job_progress_pct")}
                     for t in state.get("tractors", [])],
        "jobs": [{k: j.get(k) for k in ("id", "field_id", "job_type", "progress_pct", "priority", "status",
                                        "assigned_tractor_id")} | {"due": _clock(state, j.get("deadline_tick"))}
                 for j in state.get("jobs", [])],
        "weather": {k: cond.get(k) for k in ("wind_mph", "rain_mm_hr", "daylight")},
        "on_hold": cond.get("job_blocks"), "metrics": state.get("metrics"),
    }


def hf_token() -> str | None:
    """HF_TOKEN env var, else the token cached by `hf auth login` / `huggingface-cli login`."""
    tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if tok:
        return tok
    try:
        from huggingface_hub import get_token
        return get_token()
    except Exception:  # noqa: BLE001
        return None


def _llm_rephrase(question: str, draft: str, state: dict[str, Any]) -> str | None:
    token = hf_token()
    if not token or os.environ.get("FARM_CHAT_LLM", "1") == "0":
        return None
    try:
        from huggingface_hub import InferenceClient
        client = InferenceClient(model=os.environ.get("FARM_CHAT_MODEL", "Qwen/Qwen2.5-72B-Instruct"),
                                 token=token, timeout=20)
        sys_prompt = ("You are a helpful farm operations assistant talking to a farmer. Answer in 2-5 short "
                      "sentences of plain language. Use ONLY the facts provided (the live simulator state is "
                      "authoritative); never invent numbers, machines or fields. You cannot move machines yourself; "
                      "if the farmer wants a change, tell them to describe it (e.g. 'T2 broke down') so the app "
                      "can offer a confirm button.")
        msgs = [{"role": "system", "content": sys_prompt},
                {"role": "user", "content": f"Farmer asked: {question}\n\nGrounded answer draft:\n{draft}\n\n"
                                            f"Live state:\n{json.dumps(_compact_state(state), default=str)[:6000]}"}]
        r = client.chat_completion(messages=msgs, max_tokens=300, temperature=0.2)
        text = (r.choices[0].message.content or "").strip()
        return text or None
    except Exception:  # noqa: BLE001 — offline / quota / model errors fall back to rules
        return None
