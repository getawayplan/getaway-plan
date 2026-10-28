#!/usr/bin/env python3
"""Getaway Plan fare tracker.

Reads config.json (home airport, destinations + price limits, free windows) and asks
SerpApi's Google Flights API for current round-trip fares inside each free window.
Every observation is appended to a history file. When a fare is under your limit the
tracker spends one more search to look up the exact return flight, so the dashboard can
show the full itinerary and the real round-trip total.

    python tracker.py --plan     # show what would be searched (uses no searches)
    python tracker.py            # live run (needs SERPAPI_API_KEY)
    python tracker.py --demo     # synthetic sample data, clearly labeled, no API

Standard library only, so it runs anywhere without installing packages.
Prices are Google Flights totals for one traveler. They include airport taxes; bag fees
and card fees are not included.
"""
import argparse
import json
import os
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
SERP_URL = "https://serpapi.com/search.json"


# --------------------------------------------------------------------------- io
def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def save_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2) + "\n")


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def short(d):
    return f"{d.strftime('%b')} {d.day}"


# ----------------------------------------------------- booking windows & schedule
# Fares tend to be lowest in a band of days before departure. Defaults come from Google's
# published pattern: about 22 to 57 days out for US domestic trips, 50 to 133 for international.
# A window can add `book_earlier_days` (holidays and spring break book a bit earlier).
DEFAULT_BOOKING_DAYS = (22, 57)
DEFAULT_CHECK_EVERY = {"far": 21, "approaching": 7, "prime": 3, "late": 5}
APPROACH_DAYS = 21          # "approaching" = this many days before the best window opens
PHASE_ORDER = {"prime": 0, "late": 1, "approaching": 2, "far": 3}
PHASE_LABEL = {"far": "Too early to expect the best fares", "approaching": "Best booking window opens soon",
               "prime": "In the best booking window", "late": "Past the best booking window"}


INTERNATIONAL_BOOKING_DAYS = (50, 133)
DEFAULT_MAX_LAYOVER_HOURS = {"domestic": 3, "international": 7}


def is_international(dest):
    bd = dest.get("booking_days")
    return bool(dest.get("international")) or bool(bd and bd[1] > 70)


def layover_cap_min(cfg, dest):
    """Longest layover (minutes) allowed for this destination, or None for no limit."""
    table = dict(DEFAULT_MAX_LAYOVER_HOURS)
    table.update(cfg.get("max_layover_hours") or {})
    hours = table["international" if is_international(dest) else "domestic"]
    return int(round(float(hours) * 60)) if hours else None


def booking_range(dest, window):
    lo, hi = dest.get("booking_days") or (INTERNATIONAL_BOOKING_DAYS if is_international(dest)
                                          else DEFAULT_BOOKING_DAYS)
    extra = int(window.get("book_earlier_days") or 0)
    return int(lo) + extra, int(hi) + extra


def phase_for(days_out, lo, hi):
    if days_out > hi + APPROACH_DAYS:
        return "far"
    if days_out > hi:
        return "approaching"
    if days_out >= lo:
        return "prime"
    return "late"


def check_every(cfg, phase):
    table = dict(DEFAULT_CHECK_EVERY)
    table.update(cfg.get("check_every_days") or {})
    return max(1, int(table[phase]))


def rnd(x):
    """Round half up (same as JavaScript), so Python and the website pick the same dates."""
    return int(x + 0.5)


def plan_trips(cfg, today):
    """Turn windows x destinations into concrete (depart, return) searches."""
    trips, skipped = [], []
    for w in cfg["windows"]:
        ws, we = date.fromisoformat(w["start"]), date.fromisoformat(w["end"])
        label = f"{short(ws)} – {short(we)}"
        length = (we - ws).days
        if we <= today:
            skipped.append(f"{label}: window is already over")
            continue
        for d in cfg["destinations"]:
            nights = min(d["nights"], length)
            if nights < d.get("min_nights", 1):
                skipped.append(f"{d['name']} / {label}: window too short "
                               f"({length} nights, needs {d['min_nights']}+)")
                continue
            earliest = max(ws, today + timedelta(days=1))
            latest = we - timedelta(days=nights)
            if latest < earliest:
                skipped.append(f"{d['name']} / {label}: no departure date left")
                continue
            n = max(1, int(w.get("samples", 1)))
            span = (latest - earliest).days
            if n == 1 or span == 0:
                departs = [earliest]
            else:
                departs = sorted({earliest + timedelta(days=rnd(i * span / (n - 1)))
                                  for i in range(n)})
            lo, hi = booking_range(d, w)
            for dep in departs:
                ret = dep + timedelta(days=nights)
                trips.append({
                    "id": f"{d['name']}|{dep}|{ret}",
                    "destination": d["name"],
                    "airports": d["airports"],
                    "window": label,
                    "depart": dep.isoformat(),
                    "return": ret.isoformat(),
                    "nights": nights,
                    "max_price": d["max_price"],
                    "book_lo": lo,
                    "book_hi": hi,
                    "max_layover_min": layover_cap_min(cfg, d),
                })
    return trips, skipped


def trip_phase(t, today):
    days_out = (date.fromisoformat(t["depart"]) - today).days
    return days_out, phase_for(days_out, t["book_lo"], t["book_hi"])


def pick_due(trips, cfg, history, today, now, details=None, force=False):
    """Trips to check now, most valuable first. A trip is due when its last check is older than
    its phase's interval, or when the layover limit has changed since that check."""
    last = {}  # trip id -> (time of last check, layover limit used then)
    for h in history:
        cur = last.get(h["trip_id"])
        if cur is None or h["ts"] > cur[0]:
            last[h["trip_id"]] = (h["ts"], h.get("max_layover_min"))
    for tid, d in (details or {}).items():
        if d.get("checked_at") and (tid not in last or d["checked_at"] > last[tid][0]):
            last[tid] = (d["checked_at"], d.get("max_layover_min"))
    due = []
    for t in trips:
        days_out, ph = trip_phase(t, today)
        ts, cap = last.get(t["id"], (None, None))
        if ts and not force and cap == t.get("max_layover_min"):
            age = (now - datetime.fromisoformat(ts)).total_seconds() / 86400
            if age < check_every(cfg, ph) - 0.5:   # half a day of slack for scheduling drift
                continue
        due.append((PHASE_ORDER[ph], "" if (force or cap != t.get("max_layover_min")) else (ts or ""), t))
    due.sort(key=lambda x: (x[0], x[1]))
    return [x[2] for x in due]


def pace_limit(budget, today, slack):
    """Searches allowed so far this month, so the budget lasts all month, not just the first week."""
    days = (date(today.year + (today.month == 12), today.month % 12 + 1, 1) - date(today.year, today.month, 1)).days
    return min(budget, int(budget * today.day / days) + slack)


def simulate_month(cfg, today, days=30):
    """Rough count of checks over the next `days` days, following the same rules as a live run."""
    total, by_phase = 0, {}
    for t in plan_trips(cfg, today)[0]:
        dep = date.fromisoformat(t["depart"])
        day = 0
        while day < days:
            d = today + timedelta(days=day)
            if dep <= d:
                break
            ph = phase_for((dep - d).days, t["book_lo"], t["book_hi"])
            total += 1
            by_phase[ph] = by_phase.get(ph, 0) + 1
            day += check_every(cfg, ph)
    return total, by_phase


# ------------------------------------------------------------------- live fares
class QuotaError(Exception):
    pass


def serp_search(api_key, home, trip, currency, departure_token=None):
    params = {
        "engine": "google_flights",
        "departure_id": ",".join(home),
        "arrival_id": ",".join(trip["airports"]),
        "outbound_date": trip["depart"],
        "return_date": trip["return"],
        "type": "1",  # round trip
        "adults": "1",
        "currency": currency,
        "hl": "en",
        "api_key": api_key,
    }
    if departure_token:
        params["departure_token"] = departure_token  # asks for the return flights
    return serp_get(params)


def serp_one_way(api_key, origins, destinations, day, currency):
    """One-way fares from any of `origins` to any of `destinations` on `day`."""
    return serp_get({
        "engine": "google_flights",
        "departure_id": ",".join(origins),
        "arrival_id": ",".join(destinations),
        "outbound_date": day,
        "type": "2",  # one way
        "adults": "1",
        "currency": currency,
        "hl": "en",
        "api_key": api_key,
    })


def serp_get(params):
    url = f"{SERP_URL}?{urlencode(params)}"
    last_err = None
    for attempt in range(2):
        try:
            with urlopen(url, timeout=60) as r:
                data = json.loads(r.read().decode())
            if "error" in data:
                msg = str(data["error"])
                if "no results" in msg.lower():
                    return {}
                raise RuntimeError(msg)
            return data
        except HTTPError as e:
            if e.code in (401, 403, 429):
                raise QuotaError(f"SerpApi returned HTTP {e.code}")
            last_err = f"HTTP {e.code}"
        except (URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as e:
            last_err = type(e).__name__ if not isinstance(e, RuntimeError) else str(e)
        time.sleep(2)
    raise RuntimeError(last_err or "unknown error")


def itineraries(resp, max_layover_min=None):
    """Priced itineraries, skipping any with a layover longer than the limit (minutes)."""
    offers = (resp.get("best_flights") or []) + (resp.get("other_flights") or [])
    offers = [o for o in offers if isinstance(o.get("price"), (int, float))]
    if max_layover_min:
        offers = [o for o in offers
                  if all((l.get("duration") or 0) <= max_layover_min for l in (o.get("layovers") or []))]
    return offers


def leg_detail(itin):
    """Exact flights for one direction: segments, layovers, total minutes."""
    segs = []
    for s in itin.get("flights") or []:
        dep, arr = s.get("departure_airport") or {}, s.get("arrival_airport") or {}
        segs.append({
            "airline": s.get("airline"),
            "flight": s.get("flight_number"),
            "from": dep.get("id"),
            "depart": dep.get("time"),
            "to": arr.get("id"),
            "arrive": arr.get("time"),
            "minutes": s.get("duration"),
            "overnight": bool(s.get("overnight")),
        })
    lays = [{"name": l.get("name"), "code": l.get("id"),
             "minutes": l.get("duration"), "overnight": bool(l.get("overnight"))}
            for l in (itin.get("layovers") or [])]
    return {"segments": segs, "layovers": lays, "minutes": itin.get("total_duration")}


def best_offer(resp, max_layover_min=None):
    """Cheapest itinerary in a Google Flights response (price = round-trip total)."""
    offers = itineraries(resp, max_layover_min)
    if not offers:
        return None
    o = min(offers, key=lambda x: x["price"])
    detail = leg_detail(o)
    airlines = []
    for s in detail["segments"]:
        if s["airline"] and s["airline"] not in airlines:
            airlines.append(s["airline"])
    stops = len(o["layovers"]) if "layovers" in o else max(0, len(detail["segments"]) - 1)
    insights = resp.get("price_insights") or {}
    return {
        "price": o["price"],
        "airlines": airlines,
        "stops": stops,
        "duration_min": o.get("total_duration"),
        "price_level": insights.get("price_level"),
        "typical_range": insights.get("typical_price_range"),
        "link": (resp.get("search_metadata") or {}).get("google_flights_url"),
        "outbound": detail,
        "departure_token": o.get("departure_token"),
    }


def cheapest_return(resp, max_layover_min=None):
    """Cheapest return flight from a departure_token lookup (price = round-trip total)."""
    offers = itineraries(resp, max_layover_min)
    if not offers:
        return None
    o = min(offers, key=lambda x: x["price"])
    return {"price": o["price"], "detail": leg_detail(o)}


# ------------------------------------------------- two one-way tickets vs round trip
OW_DEFAULTS = {"enabled": True, "max_trips_per_run": 2, "within_pct_of_limit": 25, "min_days_between": 7}
OW_MAX_AGE_DAYS = 7  # a saved comparison older than this is no longer shown


def ow_settings(cfg):
    o = dict(OW_DEFAULTS)
    o.update(cfg.get("one_way_compare") or {})
    return o


def one_way_leg(resp, max_layover_min=None):
    """Cheapest one-way itinerary in a response: price plus the exact flights."""
    offers = itineraries(resp, max_layover_min)
    if not offers:
        return None
    o = min(offers, key=lambda x: x["price"])
    detail = leg_detail(o)
    airlines = []
    for sg in detail["segments"]:
        if sg["airline"] and sg["airline"] not in airlines:
            airlines.append(sg["airline"])
    return {"price": o["price"], "airlines": airlines, "detail": detail}


def one_way_candidates(trips_searched, details, cfg, today, now):
    """Trips worth a one-way comparison today: in their best window, near or under the limit,
    and not compared recently. Closest to the limit first."""
    ow = ow_settings(cfg)
    out = []
    for t in trips_searched:
        det = details.get(t["id"]) or {}
        if det.get("price") is None or trip_phase(t, today)[1] != "prime":
            continue
        if det["price"] > t["max_price"] * (1 + float(ow["within_pct_of_limit"]) / 100):
            continue
        prev = det.get("oneway")
        if prev and prev.get("checked_at"):
            age = (now - datetime.fromisoformat(prev["checked_at"])).total_seconds() / 86400
            if age < float(ow["min_days_between"]) - 0.5:
                continue
        out.append((det["price"] / t["max_price"], t))
    out.sort(key=lambda x: x[0])
    return [t for _, t in out][: int(ow["max_trips_per_run"])]


# -------------------------------------------------------------------- demo mode
DEMO_BASE = {"Boston": 370, "New York City": 720, "Washington, DC": 370,
             "London": 1230, "Delhi": 3400}
DEMO_AIRLINES = {
    "Boston": ["American", "Southwest", "Delta"],
    "New York City": ["United", "American", "Delta"],
    "Washington, DC": ["Southwest", "United", "American"],
    "London": ["British Airways", "American", "Aer Lingus"],
    "Delhi": ["Air India", "Emirates", "Lufthansa"],
}
AIRLINE_CODE = {"American": "AA", "Southwest": "WN", "Delta": "DL", "United": "UA",
                "British Airways": "BA", "Aer Lingus": "EI", "Air India": "AI",
                "Emirates": "EK", "Lufthansa": "LH"}
# minutes: nonstop MCI->dest, hub->dest after a stop (all demo hubs are on Central time)
DEMO_FLIGHT = {"Boston": (175, 140), "New York City": (170, 135), "Washington, DC": (155, 110),
               "London": (None, 470), "Delhi": (None, 880)}
DEMO_HUBS = [("ORD", "Chicago O'Hare"), ("DFW", "Dallas/Fort Worth")]


def season(dep):
    if (dep.month == 12 and dep.day >= 18) or (dep.month == 1 and dep.day <= 3):
        return 1.30
    if dep.month == 1:
        return 0.90
    if dep.month in (3, 4):
        return 1.05
    return 0.95


def demo_history(trips, cfg, today):
    """Plausible fake price history: ~14 observations, one every 4 days."""
    rng = random.Random(2026)
    history = []
    steps = 14
    for t in trips:
        dep = date.fromisoformat(t["depart"])
        base = DEMO_BASE.get(t["destination"], 500) * season(dep)
        start = rng.uniform(0.98, 1.22)
        end = rng.uniform(0.72, 1.18)
        airlines = DEMO_AIRLINES.get(t["destination"], ["Demo Air"])
        stops = 1 if t["destination"] in ("London", "Delhi") else rng.choice([0, 0, 1])
        for k in range(steps):
            frac = k / (steps - 1)
            factor = start + (end - start) * frac + rng.uniform(-0.04, 0.04)
            ts = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc) \
                - timedelta(days=4 * (steps - 1 - k)) + timedelta(hours=14)
            history.append({
                "ts": ts.replace(microsecond=0).isoformat(),
                "trip_id": t["id"],
                "price": int(round(base * factor)),
                "airlines": [rng.choice(airlines)],
                "stops": stops,
                "price_level": "low" if factor < 0.88 else ("high" if factor > 1.10 else "typical"),
                "typical_range": [int(round(base * 0.93)), int(round(base * 1.10))],
            })
    return history


def tz_shift(dest, day):
    """Hours the destination clock is ahead of Kansas City, for plausible sample times."""
    if dest == "Delhi":
        return 11.5 if (day.month in (11, 12, 1, 2) or (day.month == 3 and day.day < 14)) else 10.5
    return {"London": 6}.get(dest, 1)


def build_leg(rng, day, nodes, durs, lays, code):
    """nodes: [(airport, hours-ahead-of-Central)], durs per hop, lays per connection."""
    h, m = rng.choice([(6, 10), (7, 5), (9, 40), (12, 15), (14, 30), (17, 55)])
    cur = datetime(day.year, day.month, day.day, h, m)
    segs, layovers = [], []
    for i in range(len(nodes) - 1):
        dep_t = cur
        arr_t = dep_t + timedelta(minutes=durs[i]) + timedelta(hours=nodes[i + 1][1] - nodes[i][1])
        segs.append({
            "airline": code[0], "flight": f"{code[1]} {rng.randint(100, 2999)}",
            "from": nodes[i][0], "depart": dep_t.strftime("%Y-%m-%d %H:%M"),
            "to": nodes[i + 1][0], "arrive": arr_t.strftime("%Y-%m-%d %H:%M"),
            "minutes": durs[i], "overnight": arr_t.date() != dep_t.date(),
        })
        if i < len(lays):
            layovers.append({"name": nodes[i + 1][2], "code": nodes[i + 1][0],
                             "minutes": lays[i], "overnight": False})
            cur = arr_t + timedelta(minutes=lays[i])
    total = sum(durs) + sum(lays)
    return {"segments": segs, "layovers": layovers, "minutes": total}


def demo_details(trips, history):
    """Fake flight details that match each trip's latest sample price and stops."""
    rng = random.Random(7)
    latest = {}
    for h in history:
        latest[h["trip_id"]] = h
    details = {}
    for t in trips:
        obs = latest[t["id"]]
        dest, airline = t["destination"], obs["airlines"][0]
        code = (airline, AIRLINE_CODE.get(airline, "XX"))
        nonstop_min, hub_min = DEMO_FLIGHT.get(dest, (170, 140))
        dep, ret = date.fromisoformat(t["depart"]), date.fromisoformat(t["return"])
        dst = t["airports"][0]
        off = tz_shift(dest, dep)
        if obs["stops"] == 0 and nonstop_min:
            out_nodes = [("MCI", 0, "Kansas City"), (dst, off, dest)]
            out_durs, out_lays = [nonstop_min], []
            back_nodes = [(dst, off, dest), ("MCI", 0, "Kansas City")]
            back_durs, back_lays = [nonstop_min + 10], []
        else:
            hub = rng.choice(DEMO_HUBS)
            out_nodes = [("MCI", 0, "Kansas City"), (hub[0], 0, hub[1]), (dst, off, dest)]
            out_durs, out_lays = [85, hub_min], [rng.randint(70, 150)]
            back_nodes = [(dst, off, dest), (hub[0], 0, hub[1]), ("MCI", 0, "Kansas City")]
            back_durs, back_lays = [hub_min + 40, 85], [rng.randint(70, 150)]
        outbound = build_leg(rng, dep, out_nodes, out_durs, out_lays, code)
        row = {"price": obs["price"], "checked_at": obs["ts"], "outbound": outbound, "return": None}
        if obs["price"] <= t["max_price"]:  # like a live run: return flight looked up for deals
            row["return"] = build_leg(rng, ret, back_nodes, back_durs, back_lays, code)
        details[t["id"]] = row
    # a few sample "two one-way tickets" comparisons, some cheaper and some not
    today = date.today()
    eligible = sorted(((details[t["id"]]["price"] / t["max_price"], t) for t in trips
                       if trip_phase(t, today)[1] == "prime"
                       and details[t["id"]]["price"] <= t["max_price"] * 1.25),
                      key=lambda x: (x[1]["destination"] in ("London", "Delhi"), x[0]))
    for k, (_, t) in enumerate(eligible[:6]):
        row = details[t["id"]]
        dest, airline = t["destination"], latest[t["id"]]["airlines"][0]
        code = (airline, AIRLINE_CODE.get(airline, "XX"))
        dep, ret = date.fromisoformat(t["depart"]), date.fromisoformat(t["return"])
        dst, off = t["airports"][0], tz_shift(dest, dep)
        nonstop_min = DEMO_FLIGHT.get(dest, (170, 140))[0] or 170
        total = row["price"] * [0.86, 0.93, 1.07, 0.90, 1.12, 0.95][k]
        out_p = int(round(total * 0.52)); back_p = int(round(total)) - out_p
        out = build_leg(rng, dep, [("MCI", 0, "Kansas City"), (dst, off, dest)], [nonstop_min], [], code)
        back = build_leg(rng, ret, [(dst, off, dest), ("MCI", 0, "Kansas City")], [nonstop_min + 10], [], code)
        row["oneway"] = {"checked_at": row["checked_at"],
                         "out": {"price": out_p, "airlines": [airline], "detail": out},
                         "back": {"price": back_p, "airlines": [airline], "detail": back}}
    return details


# ---------------------------------------------------------------------- summary
def clock(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M").strftime("%I:%M %p").lstrip("0")
    except (TypeError, ValueError):
        return str(s or "")


def flight_summary(leg):
    if not leg or not leg.get("segments"):
        return ""
    s = leg["segments"]
    nums = ", ".join(f"{x.get('flight') or ''}".strip() for x in s if x.get("flight"))
    return (f"{nums or (s[0].get('airline') or 'flight')} {clock(s[0]['depart'])} {s[0]['from']}"
            f" → {clock(s[-1]['arrive'])} {s[-1]['to']}")


LOWEST_MIN_OBS = 3      # need a few earlier checks before "lowest yet" means anything
LOWEST_MIN_DROP = 0.03  # and the new price must beat the old low by at least 3%


def is_lowest_yet(obs):
    """True when the newest price beats every earlier price for this trip by a clear margin."""
    if len(obs) <= LOWEST_MIN_OBS:
        return False
    prior = min(h["price"] for h in obs[:-1])
    return obs[-1]["price"] <= prior * (1 - LOWEST_MIN_DROP)


LEVEL_TEXT = {"low": "Google rates this fare LOW for this route",
              "typical": "Google rates this fare typical for this route",
              "high": "Google rates this fare HIGH for this route"}


def build_latest(cfg, trips, skipped, history, details, mode, used, budget, today=None):
    today = today or date.today()
    by_trip = {}
    for h in history:
        by_trip.setdefault(h["trip_id"], []).append(h)
    out_trips = []
    for t in trips:
        obs = sorted(by_trip.get(t["id"], []), key=lambda h: h["ts"])
        row = dict(t)
        days_out, ph = trip_phase(t, today)
        dep = date.fromisoformat(t["depart"])
        row.update(days_out=days_out, phase=ph,
                   best_from=(dep - timedelta(days=t["book_hi"])).isoformat(),
                   best_until=(dep - timedelta(days=t["book_lo"])).isoformat(),
                   check_every_days=check_every(cfg, ph))
        mark = details.get(t["id"]) or {}
        if mark.get("none") and (not obs or mark.get("checked_at", "") >= obs[-1]["ts"]):
            row.update(status="no_data", price=None, no_data_reason="no_flights_within_layover_limit")
        elif not obs:
            row.update(status="no_data", price=None)
        else:
            cur = obs[-1]
            prev = obs[-2]["price"] if len(obs) > 1 else None
            det = details.get(t["id"]) or {}
            ow = None
            o = det.get("oneway")
            if o and o.get("out") and o.get("back") and o.get("checked_at"):
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(o["checked_at"])).total_seconds() / 86400
                if age <= OW_MAX_AGE_DAYS:
                    total = o["out"]["price"] + o["back"]["price"]
                    ow = {"total": total, "out_price": o["out"]["price"], "back_price": o["back"]["price"],
                          "out_airlines": o["out"].get("airlines", []), "back_airlines": o["back"].get("airlines", []),
                          "out_flight": o["out"]["detail"], "back_flight": o["back"]["detail"],
                          "checked_at": o["checked_at"], "better": total < cur["price"],
                          "saves": cur["price"] - total}
            effective = min(cur["price"], ow["total"]) if ow else cur["price"]
            row.update(
                status="ok",
                price=cur["price"],
                airlines=cur.get("airlines", []),
                stops=cur.get("stops"),
                duration_min=cur.get("duration_min"),
                link=cur.get("link"),
                price_level=cur.get("price_level"),
                typical_range=cur.get("typical_range"),
                observed_at=cur["ts"],
                prev_price=prev,
                delta=(cur["price"] - prev) if prev is not None else None,
                low_price=min(h["price"] for h in obs),
                observations=len(obs),
                effective_price=effective,
                oneway=ow,
                under_ceiling=effective <= t["max_price"],
                lowest_yet=is_lowest_yet(obs),
                outbound_flight=det.get("outbound"),
                return_flight=det.get("return"),
            )
        out_trips.append(row)

    dests = []
    for d in cfg["destinations"]:
        rows = [r for r in out_trips if r["destination"] == d["name"] and r["status"] == "ok"]
        best = min(rows, key=lambda r: r["effective_price"]) if rows else None
        dests.append({
            "name": d["name"],
            "airports": d["airports"],
            "max_price": d["max_price"],
            "best_trip_id": best["id"] if best else None,
            "best_price": best["effective_price"] if best else None,
            "best_kind": ("two_one_ways" if best and best.get("oneway") and best["oneway"]["better"] else "round_trip"),
            "under_ceiling": bool(best and best["effective_price"] <= d["max_price"]),
        })
    return {
        "generated_at": now_iso(),
        "mode": mode,
        "home_airports": cfg["home_airports"],
        "currency": cfg.get("currency", "USD"),
        "searches_used_this_month": used,
        "monthly_search_budget": budget,
        "estimated_searches_next_30_days": simulate_month(cfg, today)[0],
        "config": cfg,
        "destinations": dests,
        "trips": out_trips,
        "skipped": skipped,
    }


def reasons(r):
    """Plain-language reasons a fare is worth a look."""
    why = []
    eff = r.get("effective_price", r["price"])
    if r.get("under_ceiling"):
        why.append(f"${r['max_price'] - eff:,} under your limit")
    ow = r.get("oneway")
    if ow and ow.get("better"):
        why.append(f"two one-way tickets save ${ow['saves']:,} vs the ${r['price']:,} round trip")
    if r.get("lowest_yet"):
        why.append(f"lowest price yet (was ${r['low_before']:,})" if r.get("low_before") else "lowest price yet")
    if r.get("price_level") in LEVEL_TEXT:
        why.append(LEVEL_TEXT[r["price_level"]])
    return why


def deal_note(r):
    airline = ", ".join(r.get("airlines") or []) or "airline n/a"
    stops = "nonstop" if r.get("stops") == 0 else f"{r.get('stops')} stop(s)"
    out = flight_summary(r.get("outbound_flight"))
    back = flight_summary(r.get("return_flight"))
    ow = r.get("oneway")
    if ow and ow.get("better"):
        lines = [f"- **{r['destination']}** {r['depart']} → {r['return']}: "
                 f"${ow['total']:,} for TWO ONE-WAY tickets (${ow['out_price']:,} out + ${ow['back_price']:,} back), "
                 f"taxes included, bought separately; round trip is ${r['price']:,} (your limit ${r['max_price']:,})"]
        out, back = flight_summary(ow["out_flight"]), flight_summary(ow["back_flight"])
        why = reasons(r)
        if why:
            lines.append(f"  - {', '.join(why)}")
        if out:
            lines.append(f"  - Out (ticket 1): {out}")
        if back:
            lines.append(f"  - Back (ticket 2): {back}")
        lines.append("  - Two separate tickets: no protection if one flight disrupts the other, bags paid per ticket.")
        return "\n".join(lines)
    lines = [f"- **{r['destination']}** {r['depart']} → {r['return']}: "
             f"${r['price']:,} round trip, taxes included (your limit ${r['max_price']:,}), {airline}, {stops}"]
    why = reasons(r)
    if why:
        lines.append(f"  - {', '.join(why)}")
    if out:
        lines.append(f"  - Out: {out}")
    if back:
        lines.append(f"  - Back: {back}")
    if r.get("link"):
        lines.append(f"  - Google Flights: {r['link']}")
    return "\n".join(lines)


# ------------------------------------------------------------------------ alerts
def push(topic, title, body, click=None, priority="default", tags="airplane"):
    """Send a phone notification through ntfy.sh. Never raises: alerts are best-effort."""
    if not topic:
        return False
    headers = {"Title": title.encode("ascii", "ignore").decode(), "Priority": priority, "Tags": tags}
    if click and click.startswith("http"):
        headers["Click"] = click
    req = Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"  (phone alert not sent: {type(e).__name__})")
        return False


def alert_text(rows):
    """Title and body for one phone notification covering all alert-worthy trips."""
    if len(rows) == 1:
        r = rows[0]
        title = f"{r['destination']} ${r.get('effective_price', r['price']):,}"
    else:
        title = f"{len(rows)} fares worth a look"
    body = "\n".join(
        f"{r['destination']} {r['depart']} to {r['return']}: ${r.get('effective_price', r['price']):,} ({'; '.join(reasons(r)) or 'price drop'})"
        for r in rows[:6])
    body += "\nTaxes included, bags extra. Confirm the price when you book."
    return title, body


# ------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--out", default=None, help="output folder (default: docs/data, or docs/demo-data with --demo)")
    ap.add_argument("--demo", action="store_true", help="generate labeled sample data, no API calls")
    ap.add_argument("--plan", action="store_true", help="print the search plan and exit")
    ap.add_argument("--recheck-all", action="store_true", help="check every trip now, ignoring the schedule")
    ap.add_argument("--test-notify", action="store_true", help="send a test phone notification and exit")
    args = ap.parse_args()

    topic = (os.environ.get("NTFY_TOPIC") or "").strip().strip("/")
    if args.test_notify:
        if not topic:
            sys.exit("Set NTFY_TOPIC to your ntfy topic name first.")
        ok = push(topic, "Getaway Plan test", "If you can read this, phone alerts work.")
        sys.exit(0 if ok else "Could not send. Check the topic name and your connection.")

    cfg = load_json(args.config, None)
    if cfg is None:
        sys.exit(f"Config not found: {args.config}")
    today = date.today()
    trips, skipped = plan_trips(cfg, today)

    out = Path(args.out or (ROOT / "docs" / ("demo-data" if args.demo else "data")))
    budget = int(cfg.get("monthly_search_budget", 240))
    lookups_per_run = int(cfg.get("max_detail_lookups_per_run", 8))

    if args.plan:
        print(f"{len(trips)} trips are tracked. How often each is checked depends on how close it is to "
              f"its best booking window:")
        for t in sorted(trips, key=lambda t: (PHASE_ORDER[trip_phase(t, today)[1]], t["depart"])):
            days_out, ph = trip_phase(t, today)
            print(f"  {t['destination']:<15} {t['depart']} -> {t['return']} ({t['nights']} nights, "
                  f"limit ${t['max_price']:,}) | {days_out} days out, {ph}, every {check_every(cfg, ph)} days "
                  f"(best window {t['book_hi']}-{t['book_lo']} days out)")
        for x in skipped:
            print(f"  skipped: {x}")
        total, by_phase = simulate_month(cfg, today)
        print(f"Next 30 days: about {total} checks {dict(sorted(by_phase.items()))}, plus up to "
              f"{lookups_per_run} return-flight lookups per run for fares under a limit. "
              f"Budget: {budget}/month.")
        return

    if args.demo:
        history = demo_history(trips, cfg, today)
        details = demo_details(trips, history)
        save_json(out / "history.json", history)
        save_json(out / "details.json", details)
        latest = build_latest(cfg, trips, skipped, history, details, "demo", 0, budget)
        save_json(out / "latest.json", latest)
        print(f"Demo data written to {out} ({len(trips)} trips, {len(history)} observations). "
              "Sample data only: not real fares or flights.")
        return

    api_key = os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        sys.exit("Set SERPAPI_API_KEY (free key at serpapi.com), or use --demo.")

    usage = load_json(out / "usage.json", {})
    month = today.strftime("%Y-%m")
    used = usage.get("used", 0) if usage.get("month") == month else 0
    history = load_json(out / "history.json", [])
    details = load_json(out / "details.json", {})
    previous = build_latest(cfg, trips, skipped, history, details, "live", used, budget)
    was_under = {r["id"]: r.get("under_ceiling") for r in previous["trips"]}
    prior_low = {r["id"]: r.get("low_price") for r in previous["trips"]}
    currency = cfg.get("currency", "USD")

    done = failed = 0
    lookups_left = lookups_per_run
    halted = False
    now = datetime.now(timezone.utc)
    due = pick_due(trips, cfg, history, today, now, details, force=args.recheck_all)
    allowed = pace_limit(budget, today, int(cfg.get("pacing_slack", 40)))
    print(f"{len(due)} of {len(trips)} trips are due for a check today "
          f"({used}/{budget} searches used this month, pace limit {allowed}).")
    searched = set()
    for t in due:
        if used >= budget:
            print(f"Monthly budget of {budget} searches reached; stopping early.")
            break
        if used >= allowed:
            print(f"Pacing: {used} searches used is already the pace for day {today.day}; "
                  f"the rest wait until tomorrow so the budget lasts all month.")
            break
        try:
            resp = serp_search(api_key, cfg["home_airports"], t, currency)
            used += 1
        except QuotaError as e:
            print(f"Stopping: {e} (check your SerpApi key and quota).")
            break
        except Exception as e:  # keep going; one bad route shouldn't sink the run
            used += 1
            failed += 1
            print(f"  {t['destination']} {t['depart']}: search failed ({e})")
            continue
        searched.add(t["id"])
        offer = best_offer(resp, t.get("max_layover_min"))
        if not offer:
            print(f"  {t['destination']} {t['depart']}: no fares returned "
                  f"(or none with layovers under your limit)")
            details[t["id"]] = {"none": True, "checked_at": now_iso(), "max_layover_min": t.get("max_layover_min")}
            continue
        done += 1

        quoted = offer["price"]  # the "from" price in the first search
        det = {"price": offer["price"], "quoted": quoted, "checked_at": now_iso(),
               "max_layover_min": t.get("max_layover_min"),
               "outbound": offer["outbound"], "return": None}
        old_ow = (details.get(t["id"]) or {}).get("oneway")
        if old_ow:
            det["oneway"] = old_ow  # kept until it is refreshed or gets too old to show
        if offer["price"] <= t["max_price"] and offer.get("departure_token"):
            prev = details.get(t["id"]) or {}
            if prev.get("return") and prev.get("quoted") == quoted:
                det["return"] = prev["return"]  # same fare as last time: no new search needed
                det["price"] = offer["price"] = prev.get("price", offer["price"])
            elif lookups_left > 0 and used < budget:
                try:
                    back = cheapest_return(serp_search(api_key, cfg["home_airports"], t, currency,
                                                       departure_token=offer["departure_token"]),
                                           t.get("max_layover_min"))
                    used += 1
                    lookups_left -= 1
                    if back:
                        det["return"] = back["detail"]
                        offer["price"] = back["price"]  # a real bookable round-trip total
                        det["price"] = back["price"]
                except QuotaError as e:
                    print(f"Stopping after this fare: {e}")
                    halted = True
                except Exception as e:
                    used += 1
                    print(f"  {t['destination']} {t['depart']}: return-flight lookup failed ({e})")
        details[t["id"]] = det

        history.append({
            "ts": now_iso(),
            "trip_id": t["id"],
            "price": offer["price"],
            "airlines": offer["airlines"],
            "stops": offer["stops"],
            "duration_min": offer["duration_min"],
            "price_level": offer["price_level"],
            "typical_range": offer["typical_range"],
            "link": offer["link"],
            "max_layover_min": t.get("max_layover_min"),
        })
        print(f"  {t['destination']:<15} {t['depart']}: ${offer['price']:,} "
              f"(limit ${t['max_price']:,}, {trip_phase(t, today)[1]}){' + return flight' if det['return'] else ''}")
        if halted:
            break

    # Two one-way tickets vs the round trip. Lowest priority: only runs when the month's pace has
    # room, only for a few trips in their best booking window, and at most once a week per trip.
    if ow_settings(cfg)["enabled"] and not halted:
        for t in one_way_candidates([x for x in due if x["id"] in searched], details, cfg, today, now):
            if used + 2 > min(budget, allowed):
                print("One-way comparison skipped: no room in this month's search pace.")
                break
            try:
                r1 = serp_one_way(api_key, cfg["home_airports"], t["airports"], t["depart"], currency)
                used += 1
                r2 = serp_one_way(api_key, t["airports"], cfg["home_airports"], t["return"], currency)
                used += 1
            except QuotaError as e:
                print(f"Stopping one-way comparisons: {e}")
                break
            except Exception as e:
                used += 1
                print(f"  {t['destination']} {t['depart']}: one-way comparison failed ({e})")
                continue
            a, b = one_way_leg(r1, t.get("max_layover_min")), one_way_leg(r2, t.get("max_layover_min"))
            if a and b:
                details[t["id"]]["oneway"] = {"checked_at": now_iso(), "out": a, "back": b}
                rt = details[t["id"]]["price"]
                verdict = f"saves ${rt - a['price'] - b['price']:,}" if a["price"] + b["price"] < rt else "no saving"
                print(f"  {t['destination']:<15} {t['depart']}: one-ways ${a['price'] + b['price']:,} "
                      f"vs round trip ${rt:,} ({verdict})")
            else:
                print(f"  {t['destination']} {t['depart']}: no one-way fares returned for one direction")

    keep = {t["id"] for t in trips}
    details = {k: v for k, v in details.items() if k in keep}
    save_json(out / "history.json", history)
    save_json(out / "details.json", details)
    save_json(out / "usage.json", {"month": month, "used": used})
    latest = build_latest(cfg, trips, skipped, history, details, "live", used, budget)
    save_json(out / "latest.json", latest)

    # Worth an alert: newly under the limit, or the lowest price this trip has had so far.
    # (Fares already under the limit stay quiet unless they hit a new low.)
    new_deals = []
    for r in latest["trips"]:
        if r.get("status") != "ok" or r["id"] not in searched:
            continue
        r["low_before"] = prior_low.get(r["id"])
        if (r.get("under_ceiling") and not was_under.get(r["id"])) or r.get("lowest_yet"):
            new_deals.append(r)
    note = out / "new_deals.md"
    if new_deals:
        note.write_text("Fares worth a look: newly under your price limit, or the lowest yet "
                        "(prices include taxes; bags extra, "
                        "and fares can change before you book):\n\n"
                        + "\n".join(deal_note(r) for r in new_deals) + "\n")
        print(f"{len(new_deals)} fare(s) worth an alert.")
        if topic:
            title, body = alert_text(new_deals)
            top = min(new_deals, key=lambda r: r["price"])
            sent = push(topic, title, body, click=top.get("link"),
                        priority="high" if any(r.get("under_ceiling") for r in new_deals) else "default")
            print("Phone alert sent." if sent else "Phone alert not sent.")
    elif note.exists():
        note.unlink()
    print(f"Done: {done} fares recorded, {failed} errors, {used}/{budget} searches used this month.")


if __name__ == "__main__":
    main()
