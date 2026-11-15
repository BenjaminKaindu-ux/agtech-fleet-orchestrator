"""Scenario library: realistic field-operation scenarios for testing the agent.

Run ``python scenario_library.py`` to (re)write ``scenarios/*.json``.

Machine rates are computed, not guessed, wherever a public reference exists:

    effective field capacity (ac/h) = width_ft × speed_mph × field_efficiency / 8.25
    work fuel (L/h)                 = diesel gal/ac × 3.785 × capacity

Speeds/efficiencies follow the University of Arizona and Iowa State machinery tables
(which draw on ASABE D497); diesel per acre follows the Virginia Tech / Extension
table (planting 0.52, spraying 0.23, combining 1.25, disking 0.74, stalk chopping
0.55 gal/ac). Values marked ``estimate`` had no source in that research and are
illustrative. Weather rules are in ``field_conditions.py``.

The grid is a coordination abstraction: one cell is one road hop between fields,
including folding/transport set-up, and takes one tick (30 min in the new scenarios).
"""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCEN_DIR = HERE / "scenarios"
GAL = 3.785

SOURCES = {
    "capacity": "University of Arizona machinery table (typical speed & field efficiency): https://ag.arizona.edu/crop/equipment/agmgttable3.pdf",
    "capacity_isu": "Iowa State Extension PM 696 Estimating Field Capacity of Farm Machines: https://store.extension.iastate.edu/Product/Estimating-Field-Capacity-of-Farm-Machines-pdf",
    "fuel": "Virginia Tech Farm Management Update – fuel use per acre by operation: https://sites.ext.vt.edu/newsletter-archive/fmu/2004-08/impact.html",
    "fuel_var": "Purdue AE-110 (actual fuel can differ ±50% from table values): https://www.extension.purdue.edu/extmedia/ae/ae-110.html",
    "combine": "Iowa State PM 786 / Michigan State soybean harvest capacity: https://www.canr.msu.edu/news/what_can_be_done_to_expedite_soybean_harvest_operations",
    "drift": "UMN Extension, Too windy to spray? (spray in 3–10 mph wind): https://extension.umn.edu/herbicides/too-windy-to-spray",
    "drift_unl": "UNL CropWatch, Managing pesticide applications to avoid drift: https://cropwatch.unl.edu/managing-pesticide-applications-avoid-drift",
    "compaction": "UW–Madison Extension, Managing soil compaction at planting and harvest (24–72 h after soaking rain): https://fyi.extension.wisc.edu/agemergency/managing-soil-compaction-at-planting-and-harvest",
    "hay": "UW–Madison, Rain damage to forage (22% DM loss; 3→2 day curing): https://fyi.extension.wisc.edu/forage/files/2014/01/Raindam.pdf",
    "hay_moisture": "Manitoba Agriculture, High quality hay management (bale at 18–20% moisture): https://www.gov.mb.ca/agriculture/crops/crop-management/forages/pubs/high_quality_hay_mgmt.pdf",
}


def capacity(width_ft: float, mph: float, eff: float) -> float:
    return round(width_ft * mph * eff / 8.25, 1)


def work_lph(gal_per_ac: float, ac_per_h: float) -> float:
    return round(gal_per_ac * GAL * ac_per_h, 1)


# implement -> (ac/h, L/h, provenance)
def _imp(width, mph, eff, gal, note):
    cap = capacity(width, mph, eff)
    return {"ac_h": cap, "l_h": work_lph(gal, cap), "basis": f"{width} ft × {mph} mph × {eff:.0%} eff; {gal} gal/ac; {note}"}


IMPLEMENTS = {
    "planter-16": _imp(40, 5.5, 0.65, 0.52, "16-row 30 in planter"),
    "planter-12": _imp(30, 5.5, 0.65, 0.52, "12-row 30 in planter"),
    "sprayer-pull": _imp(60, 7.0, 0.65, 0.23, "pull-type boom"),
    "sprayer-sp": _imp(90, 12.0, 0.65, 0.23, "self-propelled boom"),
    "cultivator": _imp(30, 7.0, 0.80, 0.74, "field cultivator (disking fuel as proxy)"),
    "mower-conditioner": _imp(16, 7.0, 0.80, 0.60, "disc mower-conditioner; fuel estimate"),
    "rake": _imp(25, 6.0, 0.80, 0.15, "wheel rake; estimate"),
    "baler": {"ac_h": 8.0, "l_h": work_lph(0.45, 8.0), "basis": "large round baler, yield-limited; estimate"},
    "corn-head": {"ac_h": 7.0, "l_h": work_lph(1.25, 7.0), "basis": "~7 ac/h corn (Iowa Farm Bureau benchmark); 1.25 gal/ac"},
    "grain-platform": {"ac_h": 9.4, "l_h": work_lph(1.25, 9.4), "basis": "30 ft platform 3.8 mph 68% (ISU PM 786); 1.25 gal/ac"},
    "stalk-chopper": _imp(20, 6.0, 0.80, 0.55, "stalk chopper"),
}


def machine(mid: str, pos, implements: dict[str, str], tank: float, fuel_pct: float,
            reserve: float, travel_l: float, refuel: float = 400.0) -> dict[str, Any]:
    """``implements`` maps the job's implement name -> IMPLEMENTS key."""
    rates = {name: IMPLEMENTS[key]["ac_h"] for name, key in implements.items()}
    burn = max(IMPLEMENTS[key]["l_h"] for key in implements.values())
    return {"id": mid, "position": list(pos), "implements": list(implements),
            "fuel_liters": round(tank * fuel_pct), "tank_liters": tank, "fuel_reserve_liters": reserve,
            "acres_per_hour": rates, "travel_liters_per_cell": travel_l,
            "work_liters_per_hour": burn, "refuel_liters_per_tick": refuel,
            "_basis": {name: IMPLEMENTS[key]["basis"] for name, key in implements.items()}}


def job(jid, fid, jtype, imp, acres, prio, deadline, order):
    return {"id": jid, "field_id": fid, "job_type": jtype, "required_implement": imp,
            "acres": acres, "priority": prio, "deadline_tick": deadline, "created_order": order}


def weather(wind: list[tuple[float, float]], rain: list[tuple[float, float]] = (),
            tick_minutes: int = 30) -> list[dict[str, Any]]:
    """Merge a wind timeline [(hours_since_start, mph)] and a rain timeline
    [(hours_since_start, mm_per_hr)] into one piecewise-constant tick timeline.
    Later wind rows at the same hour override earlier ones (gusts over the diurnal cycle)."""
    tph = 60 / tick_minutes
    wind_at = {}
    for h, mph in wind:
        wind_at[h] = mph
    rain_at = dict(rain)
    hours = sorted(set(wind_at) | set(rain_at) | {0})
    out, cur_w, cur_r = [], 6.0, 0.0
    for h in hours:
        cur_w = wind_at.get(h, cur_w)
        cur_r = rain_at.get(h, cur_r)
        out.append({"tick": int(round(h * tph)), "wind_mph": cur_w, "rain_mm_hr": cur_r})
    return out


def diurnal_wind(days: int, calm=2.0, morning=6.0, afternoon=14.0, evening=7.0) -> list[tuple]:
    """Typical pattern: calm night (inversions), building breeze, windy afternoon."""
    rows = []
    for d in range(days):
        base = d * 24
        rows += [(base + 0, morning), (base + 4, morning + 2), (base + 6, afternoon),
                 (base + 11, evening), (base + 14, calm)]  # 06,10,12,17,20 o'clock
    return rows


# --------------------------------------------------------------------------- #
def cochise() -> dict[str, Any]:
    scen = json.loads((HERE / "scenario.json").read_text())
    events = json.loads((HERE / "events.json").read_text())["events"]
    return {
        "id": "cochise-field-day",
        "title": "Cochise Valley mixed field day (original)",
        "summary": "The tractor_fleet_sim demo farm and its scripted breakdown, blocked lane and priority change. No weather.",
        "region": "Cochise County, AZ (demo values)",
        "tags": ["baseline", "breakdown", "route-block", "priority-change"],
        "horizon": 60,
        "scenario": scen,
        "events": events,
        "conditions": {},
        "realism": ["Original simulator demo; rates are the simulator's illustrative values."],
        "sources": [],
    }


def spring_planting() -> dict[str, Any]:
    fields = [
        {"id": "F1", "name": "Home 80 (sandy loam)", "position": [2, 1], "acres": 80},
        {"id": "F2", "name": "Creek Bottom (clay)", "position": [5, 3], "acres": 120},
        {"id": "F3", "name": "North 160 (loam)", "position": [3, 6], "acres": 160},
        {"id": "F4", "name": "Ridge (sand)", "position": [8, 2], "acres": 90},
        {"id": "F5", "name": "Tile Flat (clay)", "position": [9, 6], "acres": 110},
    ]
    tractors = [
        machine("P1", (0, 0), {"planter": "planter-16"}, 700, 0.9, 60, 3.0),
        machine("P2", (1, 0), {"planter": "planter-12", "cultivator": "cultivator"}, 600, 0.8, 50, 2.8),
        machine("S1", (0, 1), {"sprayer": "sprayer-sp"}, 500, 0.7, 40, 2.0),
    ]
    jobs = [
        job("J1", "F2", "plant corn", "planter", 120, 1, 140, 1),
        job("J2", "F5", "field cultivate", "cultivator", 110, 2, 124, 2),
        job("J3", "F5", "plant corn", "planter", 110, 1, 142, 3),
        job("J4", "F3", "plant soybeans", "planter", 160, 2, 130, 4),
        job("J5", "F1", "plant corn", "planter", 80, 2, 100, 5),
        job("J6", "F4", "plant soybeans", "planter", 90, 3, 120, 6),
        job("J7", "F2", "pre-emerge herbicide", "sprayer", 120, 2, 142, 7),
        job("J8", "F1", "pre-emerge herbicide", "sprayer", 80, 3, 142, 8),
    ]
    # Day 1 storm 13:00–17:00 (~20 mm), day 2 gusty afternoon.
    wind = diurnal_wind(3, calm=2.0, morning=7.0, afternoon=9.0, evening=6.0) + [(7, 11), (9, 12), (30, 13), (35, 7)]
    rain = [(7, 4.0), (9, 6.0), (11, 0.0)]
    return {
        "id": "spring-planting-rain",
        "title": "Spring planting: rain front on clay ground",
        "summary": "Over three days, two planters and a self-propelled sprayer race a ~20 mm afternoon storm. Clay fields stay too wet for ~2 days, sand for ~half a day. Pre-emerge herbicide follows planting.",
        "region": "Central Iowa (corn/soybean)",
        "tags": ["planting", "rain", "soil-wet", "dependencies", "spray-wind"],
        "horizon": 144,
        "scenario": {"name": "Spring planting – rain front", "tick_minutes": 30, "base": [0, 0],
                     "grid": {"width": 12, "height": 8, "blocked_cells": [[4, 2], [4, 3], [7, 4]]},
                     "fields": fields, "tractors": tractors, "jobs": jobs},
        "events": [{"tick": 60, "name": "breakdown", "tractor_id": "P2", "repair_after_ticks": 6,
                    "note": "Planter drive chain snaps (3 h repair)"}],
        "conditions": {
            "start_hour": 6, "sunrise_hour": 5.75, "sunset_hour": 20.5,
            "weather": weather(wind, rain),
            "soil_traffic_implements": ["planter", "cultivator", "sprayer"],
            "field_soil": {"F1": "sand", "F2": "clay", "F3": "loam", "F4": "sand", "F5": "clay"},
            "rules": {"planter": {"hours": [6, 22]}, "cultivator": {"hours": [6, 22]}},
            "dependencies": {
                "J3": {"after": "J2", "min_hours": 0.5, "why": "plant after tillage"},
                "J7": {"after": "J1", "min_hours": 0, "why": "spray after planting"},
                "J8": {"after": "J5", "min_hours": 0, "why": "spray after planting"},
            },
        },
        "realism": [
            "Planter and sprayer capacities from width × speed × efficiency (UA/ISU tables).",
            "Soil drying: sand 12 h, loam 24 h, clay 48 h per soaking rain (extension: 24–72 h compaction window).",
            "Spraying only in 3–10 mph wind, daylight, no rain; afternoon gusts close the window on day 2.",
        ],
        "sources": [SOURCES[k] for k in ("capacity", "capacity_isu", "fuel", "compaction", "drift", "drift_unl")],
    }


def hay() -> dict[str, Any]:
    fields = [
        {"id": "F1", "name": "Alfalfa A (60 ac)", "position": [2, 2], "acres": 60},
        {"id": "F2", "name": "Alfalfa B (45 ac)", "position": [6, 1], "acres": 45},
        {"id": "F3", "name": "Alfalfa C (70 ac)", "position": [4, 5], "acres": 70},
        {"id": "F4", "name": "Grass hay D (40 ac)", "position": [9, 4], "acres": 40},
    ]
    tractors = [
        machine("M1", (0, 0), {"mower-conditioner": "mower-conditioner"}, 450, 0.85, 35, 2.4),
        machine("M2", (1, 0), {"mower-conditioner": "mower-conditioner", "rake": "rake"}, 400, 0.8, 30, 2.2),
        machine("R1", (0, 2), {"rake": "rake"}, 250, 0.9, 20, 1.6),
        machine("B1", (0, 3), {"baler": "baler"}, 450, 0.75, 35, 2.4),
        machine("B2", (1, 3), {"baler": "baler"}, 450, 0.9, 35, 2.4),
    ]
    jobs, chains, deps, order = [], [], {}, 1
    deadline_bale = 140
    for i, f in enumerate(fields):
        n = i * 3
        mow, rake, bale = f"J{n + 1}", f"J{n + 2}", f"J{n + 3}"
        jobs += [job(mow, f["id"], "mow & condition", "mower-conditioner", f["acres"], 2, 60, order),
                 job(rake, f["id"], "rake windrows", "rake", f["acres"], 2, 130, order + 1),
                 job(bale, f["id"], "bale (18–20% moisture)", "baler", f["acres"], 1, deadline_bale, order + 2)]
        order += 3
        deps[rake] = {"after": mow, "min_hours": 24, "why": "cure to ~40% moisture before raking"}
        deps[bale] = {"after": rake, "min_hours": 4, "why": "dry to 18–20% before baling"}
        chains.append({"field": f["id"], "mow": mow, "rake": rake, "bale": bale})
    # Day-3 storm 14:00–21:00 (~36 mm).
    wind = diurnal_wind(3, calm=3.0, morning=6.0, afternoon=12.0, evening=8.0) + [(56, 10), (60, 9)]
    rain = [(56, 3.0), (60, 8.0), (63, 0.0)]
    return {
        "id": "hay-cut-cure-bale",
        "title": "Alfalfa first cutting: race the rain",
        "summary": "Two mower-conditioners, a rake and two balers: mow → cure 24 h → rake → dry 4 h → bale on four fields before a storm on the afternoon of day 3. Hay that is cut but not baled when it rains is counted as rain-damaged.",
        "region": "Upper Midwest / irrigated West (alfalfa)",
        "tags": ["hay", "rain", "dependencies", "rain-damage"],
        "horizon": 144,
        "scenario": {"name": "Alfalfa first cutting", "tick_minutes": 30, "base": [0, 0],
                     "grid": {"width": 11, "height": 7, "blocked_cells": [[4, 2], [5, 3], [7, 3]]},
                     "fields": fields, "tractors": tractors, "jobs": jobs},
        "events": [{"tick": 70, "name": "breakdown", "tractor_id": "B1", "repair_after_ticks": 4,
                    "note": "Baler knotter / net-wrap jam (2 h)"}],
        "conditions": {
            "start_hour": 6, "sunrise_hour": 5.5, "sunset_hour": 21.0,
            "weather": weather(wind, rain),
            "soil_traffic_implements": ["mower-conditioner", "rake", "baler"],
            "field_soil": {"F1": "loam", "F2": "loam", "F3": "clay", "F4": "sand"},
            "rules": {"mower-conditioner": {"daylight_only": True}, "rake": {"daylight_only": True},
                      "baler": {"hours": [10, 22]}},
            "dependencies": deps,
            "hay_chains": chains,
        },
        "realism": [
            "Curing 24 h before raking and 4 h more before baling (rake near 40% moisture, bale at 18–20%).",
            "Rain on cut, unbaled hay is tallied as damaged acres (1 in of rain on day-1 hay cost ~22% DM in Wisconsin trials).",
            "Baling starts mid-morning after dew; mowing/raking in daylight.",
            "Baler throughput is yield-limited and estimated; mower-conditioner fuel is an estimate.",
        ],
        "sources": [SOURCES[k] for k in ("capacity", "capacity_isu", "hay", "hay_moisture", "fuel")],
    }


def spray_window() -> dict[str, Any]:
    fields = [
        {"id": "F1", "name": "Cotton 1", "position": [2, 1], "acres": 150},
        {"id": "F2", "name": "Cotton 2", "position": [5, 1], "acres": 120},
        {"id": "F3", "name": "Alfalfa pivot", "position": [3, 4], "acres": 125},
        {"id": "F4", "name": "Sorghum", "position": [7, 3], "acres": 180},
        {"id": "F5", "name": "Corn (silage)", "position": [9, 5], "acres": 140},
        {"id": "F6", "name": "Durum", "position": [6, 6], "acres": 100},
    ]
    tractors = [
        machine("S1", (0, 0), {"sprayer": "sprayer-sp"}, 500, 0.8, 40, 2.0),
        machine("S2", (1, 0), {"sprayer": "sprayer-pull"}, 450, 0.75, 35, 2.4),
        machine("T3", (0, 1), {"stalk-chopper": "stalk-chopper", "rotary mower": "mower-conditioner"}, 400, 0.85, 30, 2.2),
    ]
    jobs = [
        job("J1", "F1", "insecticide (lygus)", "sprayer", 150, 1, 30, 1),
        job("J2", "F2", "insecticide (lygus)", "sprayer", 120, 1, 34, 2),
        job("J3", "F3", "weevil insecticide", "sprayer", 125, 2, 60, 3),
        job("J4", "F4", "post-emerge herbicide", "sprayer", 180, 2, 80, 4),
        job("J5", "F5", "fungicide", "sprayer", 140, 3, 92, 5),
        job("J6", "F6", "fungicide (rust)", "sprayer", 100, 2, 70, 6),
        job("J7", "F6", "mow field borders", "rotary mower", 20, 4, 90, 7),
        job("J8", "F5", "shred stalks (prev. crop strip)", "stalk-chopper", 40, 4, 94, 8),
    ]
    # Windy-morning gust front on day 2.
    wind = diurnal_wind(2, calm=2.0, morning=6.0, afternoon=16.0, evening=8.0) + [(26, 11), (28, 7)]
    return {
        "id": "spray-wind-window",
        "title": "Spray season: narrow wind windows",
        "summary": "Two sprayers must cover six crops while afternoon winds hit 16 mph and nights fall below 3 mph (inversion risk). Spraying is only legal in 3–10 mph daylight; the utility tractor can work in any wind.",
        "region": "Southern Arizona / Southern Plains",
        "tags": ["spray-wind", "night-restriction", "tight-deadlines"],
        "horizon": 96,
        "scenario": {"name": "Spray season – wind windows", "tick_minutes": 30, "base": [0, 0],
                     "grid": {"width": 11, "height": 8, "blocked_cells": [[4, 2], [4, 3], [8, 4]]},
                     "fields": fields, "tractors": tractors, "jobs": jobs},
        "events": [{"tick": 30, "name": "breakdown", "tractor_id": "S1", "repair_after_ticks": 5,
                    "note": "Boom section valve failure (2.5 h)"}],
        "conditions": {
            "start_hour": 6, "sunrise_hour": 5.5, "sunset_hour": 19.5,
            "weather": weather(wind),
            "soil_traffic_implements": [],
            "rules": {"rotary mower": {"daylight_only": True}, "stalk-chopper": {"daylight_only": True}},
        },
        "realism": [
            "Spray only in 3–10 mph wind, daylight, no rain (drift & inversion guidance; many labels also ban night spraying).",
            "Self-propelled 90 ft boom ~85 ac/h, 60 ft pull-type ~33 ac/h from width × speed × efficiency.",
            "Diurnal wind: calm nights, windy afternoons; leaves roughly 06:00–12:00 and 17:00–20:00 as legal windows.",
        ],
        "sources": [SOURCES[k] for k in ("drift", "drift_unl", "capacity", "fuel")],
    }


def harvest() -> dict[str, Any]:
    fields = [
        {"id": "F1", "name": "Corn 1", "position": [2, 2], "acres": 90},
        {"id": "F2", "name": "Soybeans 1", "position": [5, 1], "acres": 110},
        {"id": "F3", "name": "Corn 2", "position": [3, 5], "acres": 75},
        {"id": "F4", "name": "Soybeans 2", "position": [8, 3], "acres": 85},
        {"id": "F5", "name": "Corn 3", "position": [9, 6], "acres": 60},
    ]
    tractors = [
        machine("C1", (0, 0), {"corn-head": "corn-head", "grain-platform": "grain-platform"}, 1000, 0.85, 80, 4.0),
        machine("C2", (1, 0), {"grain-platform": "grain-platform"}, 900, 0.7, 70, 3.8),
        machine("T3", (0, 1), {"stalk-chopper": "stalk-chopper"}, 450, 0.8, 35, 2.4),
    ]
    jobs = [
        job("J1", "F2", "harvest soybeans", "grain-platform", 110, 1, 80, 1),
        job("J2", "F4", "harvest soybeans", "grain-platform", 85, 1, 104, 2),
        job("J3", "F1", "harvest corn", "corn-head", 90, 2, 110, 3),
        job("J4", "F3", "harvest corn", "corn-head", 75, 2, 132, 4),
        job("J5", "F5", "harvest corn", "corn-head", 60, 3, 140, 5),
        job("J6", "F1", "chop stalks", "stalk-chopper", 90, 4, 142, 6),
        job("J7", "F3", "chop stalks", "stalk-chopper", 75, 4, 142, 7),
    ]
    # Day-2 18:00 shower (~4.5 mm).
    wind = diurnal_wind(3, calm=4.0, morning=8.0, afternoon=15.0, evening=9.0) + [(36, 12), (39, 8)]
    rain = [(36, 3.0), (39, 0.0)]
    return {
        "id": "fall-harvest-breakdowns",
        "title": "Fall harvest: dew, showers & combine breakdowns",
        "summary": "Two combines (one with a corn head) and a stalk chopper. Soybeans can't be cut until the dew burns off; an evening shower stops harvest; both combines break down once.",
        "region": "Corn Belt",
        "tags": ["harvest", "breakdown", "time-window", "rain", "dependencies"],
        "horizon": 144,
        "scenario": {"name": "Fall harvest", "tick_minutes": 30, "base": [0, 0],
                     "grid": {"width": 11, "height": 8, "blocked_cells": [[4, 1], [4, 2], [6, 4]]},
                     "fields": fields, "tractors": tractors, "jobs": jobs},
        "events": [
            {"tick": 14, "name": "breakdown", "tractor_id": "C1", "repair_after_ticks": 8, "note": "Feeder-house chain (4 h)"},
            {"tick": 40, "name": "breakdown", "tractor_id": "C2", "repair_after_ticks": 6, "note": "Rotor belt (3 h)"},
            {"tick": 22, "name": "block_cell", "position": [6, 2], "note": "Grain truck stuck on the lane"},
            {"tick": 30, "name": "unblock_cell", "position": [6, 2], "note": "Lane cleared"},
        ],
        "conditions": {
            "start_hour": 6, "sunrise_hour": 7.0, "sunset_hour": 18.5,
            "weather": weather(wind, rain),
            "soil_traffic_implements": ["corn-head", "grain-platform", "stalk-chopper"],
            "rain_stops_work_mm_hr": 1.0,
            "rules": {"grain-platform": {"hours": [11, 22]}, "corn-head": {"hours": [8, 23]},
                      "stalk-chopper": {"daylight_only": True}},
            "dependencies": {"J6": {"after": "J3", "min_hours": 0, "why": "chop after harvest"},
                             "J7": {"after": "J4", "min_hours": 0, "why": "chop after harvest"}},
        },
        "realism": [
            "Combine capacity: soybeans ~9.4 ac/h (30 ft platform), corn ~7 ac/h; 1.25 gal/ac diesel.",
            "Soybean cutting starts late morning after dew (illustrative 11:00); corn tolerates earlier starts.",
            "Showers stop harvest while it rains; ~4.5 mm is below the soaking threshold so soil stays workable.",
        ],
        "sources": [SOURCES[k] for k in ("combine", "capacity_isu", "fuel")],
    }


BUILDERS = [cochise, spring_planting, hay, spray_window, harvest]


def build_all() -> list[Path]:
    SCEN_DIR.mkdir(exist_ok=True)
    out = []
    for b in BUILDERS:
        spec = b()
        spec["machines"] = {t["id"]: t.pop("_basis", {}) for t in spec["scenario"]["tractors"]}
        path = SCEN_DIR / f"{spec['id']}.json"
        path.write_text(json.dumps(spec, indent=2) + "\n")
        out.append(path)
    return out


# --------------------------------------------------------------------------- #
# Loading & seeded variants
# --------------------------------------------------------------------------- #
def load_library() -> dict[str, dict[str, Any]]:
    if not SCEN_DIR.is_dir() or not any(SCEN_DIR.glob("*.json")):
        build_all()
    specs = {}
    for p in sorted(SCEN_DIR.glob("*.json")):
        spec = json.loads(p.read_text())
        specs[spec["id"]] = spec
    return specs


def make_variant(spec: dict[str, Any], seed: int) -> dict[str, Any]:
    """Seed 0 is the canonical scenario. Other seeds jitter weather, breakdown timing,
    starting fuel, and may add an unscripted breakdown, so learned strategies are
    tested on conditions they were not tuned on."""
    v = copy.deepcopy(spec)
    v["seed"] = seed
    v["base_title"] = spec["title"]
    if seed == 0:
        return v
    rng = random.Random(f"{spec['id']}:{seed}")
    horizon = int(v["horizon"])
    for t in v["scenario"]["tractors"]:
        t["fuel_liters"] = round(min(t["tank_liters"], max(t["fuel_reserve_liters"] * 2,
                                                            t["fuel_liters"] * rng.uniform(0.85, 1.1))), 1)
    for ev in v["events"]:
        ev["tick"] = max(1, min(horizon - 2, ev["tick"] + rng.randint(-3, 3)))
        if ev["name"] == "breakdown" and ev.get("repair_after_ticks") is not None:
            ev["repair_after_ticks"] = max(1, ev["repair_after_ticks"] + rng.randint(-2, 2))
    if rng.random() < 0.6:
        tid = rng.choice([t["id"] for t in v["scenario"]["tractors"]])
        v["events"].append({"tick": rng.randint(5, max(6, horizon * 2 // 3)), "name": "breakdown",
                            "tractor_id": tid, "repair_after_ticks": rng.randint(2, 8),
                            "note": "unscripted failure (variant)"})
    # keep unblock after block
    v["events"].sort(key=lambda e: e["tick"])
    cond = v.get("conditions") or {}
    if cond.get("weather"):
        shift = rng.randint(-2, 2)
        wscale, rscale = rng.uniform(0.85, 1.2), rng.uniform(0.7, 1.4)
        for w in cond["weather"]:
            if w["tick"] > 0:
                w["tick"] = max(1, w["tick"] + shift)
            w["wind_mph"] = round(w["wind_mph"] * wscale, 1)
            w["rain_mm_hr"] = round(w["rain_mm_hr"] * rscale, 1)
    v["title"] = f"{spec['title']} · variant {seed}"
    return v


if __name__ == "__main__":
    for path in build_all():
        print("wrote", path.relative_to(HERE))
    for k, v in IMPLEMENTS.items():
        print(f"  {k:18s} {v['ac_h']:6.1f} ac/h {v['l_h']:6.1f} L/h   {v['basis']}")
