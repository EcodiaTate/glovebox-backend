#!/usr/bin/env python3
"""
Guide regression harness.

Posts a battery of representative scenarios against /guide/turn and grades the
response on two axes:

  1. STRUCTURAL  - did the model emit the right tool_call for "find me X"
                   queries instead of bluffing from the NEARBY block?
  2. DEPTH       - does the prose actually reference specific Australian
                   features / does it admit uncertainty when it should?

Run locally during iteration:
    python scripts/guide_test.py --target local         # hits :8000
    python scripts/guide_test.py --target prod          # hits Cloud Run
    python scripts/guide_test.py --target local --case servo_nearest_kawana

Exit code is 0 iff every case PASSES the structural assertion. Depth lines are
informational - judge by reading them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

import urllib.request
import urllib.error

PROD_URL = "https://roam-backend-176723812810.australia-southeast1.run.app"
LOCAL_URL = "http://127.0.0.1:8000"

# ──────────────────────────────────────────────────────────────────────
# Scenario building blocks
# ──────────────────────────────────────────────────────────────────────

KAWANA = {"lat": -26.6800, "lng": 153.1370}  # Sunshine Coast, beach side
LANDSBOROUGH = {"lat": -26.8059, "lng": 152.9650}  # ~22km inland, on Bruce Hwy
BRISBANE = {"lat": -27.4705, "lng": 153.0260}
CALOUNDRA = {"lat": -26.8000, "lng": 153.1330}
GLASS_HOUSE = {"lat": -26.9000, "lng": 152.9500}


def base_progress(lat: float, lng: float, km_from_start: float = 30.0) -> dict:
    return {
        "user_lat": lat,
        "user_lng": lng,
        "user_accuracy_m": 8.0,
        "user_heading": 0.0,
        "user_speed_mps": 0.0,
        "current_stop_idx": 1,
        "current_leg_idx": 0,
        "visited_stop_ids": [],
        "km_from_start": km_from_start,
        "km_remaining": 60.0,
        "total_km": 90.0,
        "local_time_iso": "2026-05-29T14:00:00",
        "timezone": "Australia/Brisbane",
        "updated_at": "2026-05-29T14:00:00",
    }


def wire_place(
    pid: str,
    name: str,
    lat: float,
    lng: float,
    category: str,
    dist_km: float,
    locality: str | None = None,
) -> dict:
    return {
        "id": pid,
        "name": name,
        "lat": lat,
        "lng": lng,
        "category": category,
        "dist_km": dist_km,
        "ahead": True,
        "locality": locality,
        "hours": None,
        "phone": None,
        "website": None,
        "quality_score": 0.5,
    }


def stops_brisbane_to_caloundra() -> list[dict]:
    return [
        {
            "id": "s0",
            "type": "start",
            "name": "Brisbane",
            "lat": BRISBANE["lat"],
            "lng": BRISBANE["lng"],
        },
        {
            "id": "s1",
            "type": "end",
            "name": "Caloundra",
            "lat": CALOUNDRA["lat"],
            "lng": CALOUNDRA["lng"],
        },
    ]


# ──────────────────────────────────────────────────────────────────────
# Scenarios
# ──────────────────────────────────────────────────────────────────────


@dataclass
class Scenario:
    name: str
    description: str
    payload: dict
    structural_check: Callable[[dict], tuple[bool, str]]
    depth_questions: list[str] = field(default_factory=list)


def _msg(role: str, text: str) -> dict:
    return {"role": role, "content": text}


def _ctx(
    progress: dict, stops: list[dict], label: str = "Brisbane → Sunshine Coast"
) -> dict:
    return {
        "plan_id": "test-plan",
        "label": label,
        "profile": "drive",
        "stops": stops,
        "total_distance_m": 90_000,
        "total_duration_s": 5400,
        "progress": progress,
        "traffic_summary": {"total": 0, "sample": []},
        "hazards_summary": {"total": 0, "sample": []},
    }


# ── Case 1 ── Tate's actual bug: in Kawana, only Landsborough in NEARBY
def case_servo_nearest_kawana() -> Scenario:
    progress = base_progress(KAWANA["lat"], KAWANA["lng"], km_from_start=85.0)
    payload = {
        "context": _ctx(progress, stops_brisbane_to_caloundra()),
        "thread": [_msg("user", "where's the nearest servo?")],
        "relevant_places": [
            wire_place(
                "ldb-bp",
                "BP Landsborough",
                LANDSBOROUGH["lat"],
                LANDSBOROUGH["lng"],
                "fuel",
                dist_km=21.5,
                locality="Landsborough",
            ),
        ],
        "tool_results": [],
    }

    def check(resp: dict) -> tuple[bool, str]:
        tcs = resp.get("tool_calls", [])
        if any(tc.get("tool") == "places_search" for tc in tcs):
            req = next((tc["req"] for tc in tcs if tc["tool"] == "places_search"), {})
            ctr = req.get("center") or {}
            radius = req.get("radius_m", 0)
            cats = req.get("categories", [])
            # Reasonable proximity search: center near user, radius <= 15km, fuel category
            close_to_user = (
                abs(ctr.get("lat", 0) - KAWANA["lat"]) < 0.05
                and abs(ctr.get("lng", 0) - KAWANA["lng"]) < 0.05
            )
            if not close_to_user:
                return False, f"places_search center not near user GPS ({ctr})"
            if radius > 20_000:
                return (
                    False,
                    f"places_search radius too wide ({radius}m) - should be ~5-15km",
                )
            if "fuel" not in [c.lower() for c in cats]:
                return False, f"places_search missing fuel category ({cats})"
            return True, "emitted tight places_search around user"
        # No tool_call: did the model at least flag the gap?
        text = resp.get("assistant", "").lower()
        if "landsborough" in text and "21" in text and "kawana" not in text:
            return False, "bluffed Landsborough as nearest without searching for Kawana"
        if "don't know" in text or "let me search" in text or "checking" in text:
            return False, "promised to check but no tool_call emitted"
        return False, "no places_search and unclear response"

    return Scenario(
        name="servo_nearest_kawana",
        description="User in Kawana, NEARBY only has Landsborough (21km) - asks 'nearest servo'. Should trigger places_search.",
        payload=payload,
        structural_check=check,
    )


# ── Case 2 ── Control: nearest servo when NEARBY has a 500m result
def case_servo_nearest_control() -> Scenario:
    progress = base_progress(KAWANA["lat"], KAWANA["lng"], km_from_start=85.0)
    payload = {
        "context": _ctx(progress, stops_brisbane_to_caloundra()),
        "thread": [_msg("user", "where's the nearest servo?")],
        "relevant_places": [
            wire_place(
                "kw-bp",
                "BP Kawana Waters",
                KAWANA["lat"] + 0.005,
                KAWANA["lng"] + 0.005,
                "fuel",
                dist_km=0.7,
                locality="Kawana Waters",
            ),
            wire_place(
                "ldb-bp",
                "BP Landsborough",
                LANDSBOROUGH["lat"],
                LANDSBOROUGH["lng"],
                "fuel",
                dist_km=21.5,
                locality="Landsborough",
            ),
        ],
        "tool_results": [],
    }

    def check(resp: dict) -> tuple[bool, str]:
        text = resp.get("assistant", "").lower()
        if "kawana" in text or "bp kawana" in text:
            return True, "correctly named Kawana fuel from NEARBY"
        if "landsborough" in text and "kawana" not in text:
            return False, "ignored close Kawana fuel and named Landsborough"
        return False, f"unclear: {text[:120]}"

    return Scenario(
        name="servo_nearest_control",
        description="Control: NEARBY has Kawana fuel at 0.7km - should be named directly without tool_call.",
        payload=payload,
        structural_check=check,
    )


# ── Case 3 ── Promise-without-delivery: "best fish and chips around here"
def case_best_fish_and_chips() -> Scenario:
    progress = base_progress(CALOUNDRA["lat"], CALOUNDRA["lng"], km_from_start=89.0)
    payload = {
        "context": _ctx(progress, stops_brisbane_to_caloundra()),
        "thread": [_msg("user", "what's the best fish and chips around here?")],
        "relevant_places": [],
        "tool_results": [],
    }

    def check(resp: dict) -> tuple[bool, str]:
        text = resp.get("assistant", "").lower()
        tcs = resp.get("tool_calls", [])
        has_search = any(
            tc.get("tool") in ("places_search", "web_search") for tc in tcs
        )
        promise_phrases = [
            "let me check",
            "i'll look",
            "i'll search",
            "looking that up",
            "give me a sec",
            "one moment",
            "checking",
            "let me find",
        ]
        if any(p in text for p in promise_phrases) and not has_search:
            return (
                False,
                f"promised lookup without emitting tool_call (text='{text[:100]}', done={resp.get('done')})",
            )
        if not has_search and not any(
            c in text
            for c in ["caloundra", "sunshine coast", "kings beach", "moffat", "shelly"]
        ):
            return False, "no AU-specific recommendation and no search"
        return True, (
            "ran search" if has_search else "named specific spots from knowledge"
        )

    return Scenario(
        name="best_fish_and_chips",
        description="Open-ended local food query - should either name specific spots OR run search, never bluff a promise.",
        payload=payload,
        structural_check=check,
        depth_questions=[
            "Did it name actual Caloundra/Sunshine Coast spots?",
            "Did it suggest Bluewater Bistro, Tides Waterfront, Suttons, etc?",
        ],
    )


# ── Case 4 ── AU regional knowledge depth: Glass House Mountains
def case_glass_house_depth() -> Scenario:
    progress = base_progress(GLASS_HOUSE["lat"], GLASS_HOUSE["lng"], km_from_start=70.0)
    payload = {
        "context": _ctx(progress, stops_brisbane_to_caloundra()),
        "thread": [
            _msg("user", "tell me about the Glass House Mountains, what's the story?")
        ],
        "relevant_places": [],
        "tool_results": [],
    }

    def check(resp: dict) -> tuple[bool, str]:
        text = resp.get("assistant", "").lower()
        au_anchors = [
            "cook",
            "captain cook",
            "dreamtime",
            "jinibara",
            "kabi kabi",
            "volcanic",
            "trachyte",
            "plug",
            "tibrogargan",
            "beerwah",
            "ngungun",
            "coonowrin",
        ]
        hits = [a for a in au_anchors if a in text]
        if len(hits) >= 3:
            return True, f"deep knowledge ({len(hits)} anchors: {hits[:5]})"
        if "i don't know" in text or "let me search" in text:
            tcs = resp.get("tool_calls", [])
            if any(tc.get("tool") == "web_search" for tc in tcs):
                return True, "admitted gap and searched"
            return False, "admitted gap but didn't search"
        return False, f"shallow ({len(hits)} AU anchors found: {hits})"

    return Scenario(
        name="glass_house_depth",
        description="Knowledge depth test: rich place with deep Aboriginal + colonial + geological history.",
        payload=payload,
        structural_check=check,
        depth_questions=[
            "Did it mention the Dreamtime story (Tibrogargan as the father)?",
            "Did it mention Cook naming them in 1770 (looked like glass furnaces)?",
            "Did it mention which peaks are climbable (Ngungun yes, Beerwah closed)?",
        ],
    )


# ── Case 5 ── Hard-knowledge gap: small obscure thing
def case_obscure_query() -> Scenario:
    progress = base_progress(
        LANDSBOROUGH["lat"], LANDSBOROUGH["lng"], km_from_start=60.0
    )
    payload = {
        "context": _ctx(progress, stops_brisbane_to_caloundra()),
        "thread": [
            _msg(
                "user",
                "is the Mary Cairncross Scenic Reserve cafe open today, and do they take cards?",
            )
        ],
        "relevant_places": [],
        "tool_results": [],
    }

    def check(resp: dict) -> tuple[bool, str]:
        tcs = resp.get("tool_calls", [])
        text = resp.get("assistant", "").lower()
        if resp.get("web_searched"):
            n = len(resp.get("sources", []))
            return True, f"ran inline web_search (grounded, {n} sources)"
        if any(tc.get("tool") == "web_search" for tc in tcs):
            return True, "emitted web_search for current info"
        if (
            "i don't have current" in text
            or "i can't confirm" in text
            or "you'll need to" in text
            or "ring ahead" in text
            or "give them a" in text
            or "check their" in text
        ):
            if re.search(r"\d{1,2}(:\d{2})?\s*(am|pm)", text):
                return False, "admitted uncertainty but still fabricated specific hours"
            return True, "admitted uncertainty (no fabricated specifics)"
        if re.search(r"\d{1,2}(:\d{2})?\s*(am|pm)", text):
            return False, "fabricated specific opening hours without searching"
        return False, f"unclear behaviour: {text[:120]}"

    return Scenario(
        name="obscure_current_info",
        description="Current-info query (hours, EFTPOS) - must search or admit uncertainty, not bluff.",
        payload=payload,
        structural_check=check,
    )


ALL_CASES = [
    case_servo_nearest_kawana,
    case_servo_nearest_control,
    case_best_fish_and_chips,
    case_glass_house_depth,
    case_obscure_query,
]


# ──────────────────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────────────────


def post_turn(base_url: str, payload: dict, timeout: int = 90) -> dict:
    req = urllib.request.Request(
        f"{base_url}/guide/turn",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode(errors="replace")[:500]}
    except Exception as e:
        return {"_error": str(e)}


def run(target_url: str, case_filter: str | None = None) -> int:
    cases = [c() for c in ALL_CASES if not case_filter or c().name == case_filter]
    if not cases:
        print(f"no matching case: {case_filter}", file=sys.stderr)
        return 2

    all_pass = True
    for sc in cases:
        print(f"\n{'=' * 70}")
        print(f"CASE: {sc.name}")
        print(f"  {sc.description}")
        print(f"  user: {sc.payload['thread'][-1]['content']!r}")

        t0 = time.time()
        resp = post_turn(target_url, sc.payload)
        dt = time.time() - t0

        if "_http_error" in resp or "_error" in resp:
            print(f"  ERROR ({dt:.1f}s): {resp}")
            all_pass = False
            continue

        text = resp.get("assistant", "")
        tcs = resp.get("tool_calls", [])
        done = resp.get("done")
        ok, why = sc.structural_check(resp)
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False

        print(f"  [{status}] {why}  ({dt:.1f}s)")
        print(f"  done={done}  tool_calls={[tc.get('tool') for tc in tcs]}")
        print(f"  assistant ({len(text)} chars):")
        for line in (text or "").split("\n"):
            print(f"    > {line}")
        if tcs:
            for tc in tcs:
                rk = tc.get("req", {})
                rkk = {
                    k: v
                    for k, v in rk.items()
                    if k in ("center", "radius_m", "categories", "query", "limit")
                }
                print(
                    f"    tool: {tc.get('tool')} req={json.dumps(rkk, separators=(',', ':'))}"
                )
        if sc.depth_questions:
            print("  depth questions:")
            for q in sc.depth_questions:
                print(f"    ? {q}")

    return 0 if all_pass else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["prod", "local"], default="prod")
    ap.add_argument("--case", default=None, help="run only this case name")
    ap.add_argument("--url", default=None, help="override base URL")
    args = ap.parse_args()

    if args.url:
        base = args.url.rstrip("/")
    else:
        base = LOCAL_URL if args.target == "local" else PROD_URL
    print(f"target: {base}")
    sys.exit(run(base, args.case))
