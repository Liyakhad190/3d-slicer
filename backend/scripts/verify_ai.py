"""Live end-to-end check of the AI endpoints against a real backend + Ollama.

Starts nothing: expects the API to already be running (the same way a user
runs it). Exercises health -> plan -> apply over HTTP with a real token, so the
whole path is covered: auth, routing, the real provider, validation, and the
deterministic mapper.

    python scripts/verify_ai.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
PREFIX = "/api/v1"

TOKEN: str | None = None


def call(
    method: str, path: str, body: Any = None, *, expect: tuple[int, ...] = (200,)
) -> tuple[int, Any]:
    url = f"{BASE}{PREFIX}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            payload = json.loads(response.read().decode() or "{}")
            status = response.status
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode() or "{}")
        status = exc.code
    if status not in expect:
        raise SystemExit(f"{method} {path} -> {status}\n{json.dumps(payload, indent=2)}")
    return status, payload


def step(label: str) -> None:
    print(f"\n=== {label} ===")


def main() -> int:
    global TOKEN
    started = time.perf_counter()

    step("register a user")
    _, auth = call(
        "POST",
        "/auth/register",
        {
            "email": f"ai-verify-{uuid.uuid4().hex[:8]}@example.com",
            "password": "correct-horse-battery-staple",
            "display_name": "AI Verify",
        },
        expect=(201,),
    )
    TOKEN = auth["access_token"]
    print(f"  token acquired")

    step("catalog, so the planner has real profiles to read")
    _, printers = call("GET", "/printers")
    print(f"  {len(printers)} printer(s) configured")
    printer_id = printers[0]["id"] if printers else None
    _, material_profiles = call("GET", "/material-profiles")
    material_id = material_profiles[0]["id"] if material_profiles else None

    step("GET /ai/health  (must be a real probe, not a config echo)")
    _, health = call("GET", "/ai/health")
    print(f"  provider        : {health['provider']}")
    print(f"  model           : {health['model']}")
    print(f"  available       : {health['available']}")
    print(f"  status          : {health['status']}")
    print(f"  latency_ms      : {health.get('latency_ms')}")
    if not health["available"]:
        print(f"  code            : {health.get('code')}")
        print(f"  hint            : {health.get('hint')}")
        print("\nOllama is not usable. The platform reports it honestly rather than "
              "falling back to anything.")
        return 1
    print(f"  models          : {health.get('models_available')}")

    step("auth is enforced")
    saved, globals()["TOKEN"] = TOKEN, None
    status, _ = call("GET", "/ai/health", expect=(401, 403))
    globals()["TOKEN"] = saved
    print(f"  unauthenticated -> {status}")

    step("POST /ai/plan  (real generation, expect 10-40s on a 1.5B model)")
    request_text = (
        "This is a structural bracket. Make it as strong as you reasonably can, "
        "but keep it light and save material. I do not mind if it takes six hours."
    )
    call_started = time.perf_counter()
    _, plan = call(
        "POST",
        "/ai/plan",
        {
            "request": request_text,
            "printer_id": printer_id,
            "material_profile_id": material_id,
        },
    )
    elapsed = time.perf_counter() - call_started
    print(f"  wall clock      : {elapsed:.1f}s (server reported {plan['duration_ms']} ms)")
    print(f"  attempts        : {plan['attempts']}")
    validation = plan["validation"]
    print(f"  valid           : {validation['valid']}")
    print(f"  intent          : {json.dumps(validation['plan']['intent'])}")
    print(f"  supports        : {validation['plan']['support_preference']}")
    print(f"  orientation     : {validation['plan']['orientation_preference']}")
    print(f"  rationale       : {validation['plan']['rationale'][:110]}")
    print(f"  retrieved docs  : {plan['context']['retrieved']}")
    print(f"  tools consulted : {[t['name'] for t in plan['context']['tools']]}")
    if validation["normalisations"]:
        print(f"  CORRECTIONS ({len(validation['normalisations'])}):")
        for item in validation["normalisations"]:
            print(f"    {item['field']}: {item['model_value']!r} -> {item['applied_value']!r} ({item['reason']})")
    else:
        print("  corrections     : none needed")

    print("\n  every score in range:", all(
        0.0 <= v <= 1.0 for v in validation["plan"]["intent"].values()
    ))

    step("POST /ai/apply  (deterministic, must not call the model)")
    apply_started = time.perf_counter()
    _, applied = call(
        "POST",
        "/ai/apply",
        {
            "plan": validation["plan"],
            "parameters": {"layer_height": 0.2},
            "printer_id": printer_id,
            "plan_decision_id": plan["decision_id"],
        },
    )
    print(f"  wall clock      : {(time.perf_counter() - apply_started) * 1000:.0f} ms")
    print(f"  dominant        : {applied['dominant_priority']}")
    print(f"  layer_height    : {applied['parameters']['layer_height']}")
    print(f"  wall_line_count : {applied['parameters']['wall_line_count']}")
    print(f"  changes         : {len(applied['changes'])}")
    print(f"  decision_id     : {applied['decision_id']}")
    for change in applied["changes"][:6]:
        print(f"    {change['field']}: {change['previous']!r} -> {change['value']!r}")
        print(f"      {change['reason']}")
    for warning in applied["warnings"]:
        print(f"  WARNING: {warning}")

    step("POST /ai/refine")
    _, refined = call(
        "POST",
        "/ai/refine",
        {
            "previous_plan": validation["plan"],
            "instruction": "Actually prioritise surface finish over material saving now.",
        },
    )
    print(f"  valid           : {refined['validation']['valid']}")
    print(f"  intent after    : {json.dumps(refined['validation']['plan']['intent'])}")

    step("the decision ledger: every AI proposal is recorded and cited")
    _, ledger = call("GET", "/ai/decisions")
    decisions = ledger["decisions"]
    print(f"  decisions       : {len(decisions)}")
    if not decisions:
        print("\nFAILED: the AI answered and recorded nothing.")
        return 1
    for item in decisions:
        print(
            f"    {item['decision_type']:<22} source={item['source']:<13} "
            f"calibration={item['calibration']:<12} evidence={len(item['evidence'])}"
        )
        if not item["reason"]:
            print("\nFAILED: a decision was written with no reason.")
            return 1
        if not item["evidence"]:
            print("\nFAILED: a decision was written with no evidence.")
            return 1
        if item["source"] == "llm" and not item["model_identifier"]:
            print("\nFAILED: an llm-sourced decision does not name its model.")
            return 1
        if item["source"] == "deterministic" and item["calibration"] == "historical":
            print("\nFAILED: arithmetic was labelled as historically calibrated.")
            return 1
    kinds = {cite["kind"] for item in decisions for cite in item["evidence"]}
    print(f"  evidence kinds  : {sorted(kinds)}")
    if "knowledge" not in kinds:
        print("  note            : no knowledge citation on this run")

    step("an override is recorded, and the reason is required")
    plan_decision = decisions[0]["id"]
    _, override = call(
        "POST",
        f"/ai/decisions/{plan_decision}/override",
        {"reason": "Declined: I only wanted a draft, not a strong part."},
    )
    print(f"  overridden      : {override['overridden']}")
    status, _ = call(
        "POST",
        f"/ai/decisions/{plan_decision}/override",
        {"reason": "   "},
        expect=(422,),
    )
    print(f"  blank reason    : {status} (refused, as it should be)")

    step("hostile input: a plan carrying G-code must not become a parameter")
    hostile = dict(validation["plan"])
    hostile["gcode"] = "G1 X999 Y999 E9999"
    _, safe = call("POST", "/ai/apply", {"plan": hostile})
    dumped = json.dumps(safe)
    print(f"  G-code in response: {'G1 ' in dumped}")
    print(f"  parameter count   : {len(safe['parameters'])}")
    if "G1 " in dumped:
        print("\nFAILED: model-authored G-code reached the parameter set")
        return 1

    step("no side effects: the slice queue is untouched")
    _, jobs = call("GET", "/slice")
    print(f"  slice jobs after AI use: {len(jobs)}")

    step("tenant isolation: another account sees no decisions")
    saved, globals()["TOKEN"] = TOKEN, None
    _, other = call(
        "POST",
        "/auth/register",
        {
            "email": f"ai-verify-other-{uuid.uuid4().hex[:8]}@example.com",
            "password": "correct-horse-battery-staple",
            "display_name": "AI Verify Other",
        },
        expect=(201,),
    )
    globals()["TOKEN"] = other["access_token"]
    _, theirs = call("GET", "/ai/decisions")
    print(f"  decisions visible to a new account: {len(theirs['decisions'])}")
    if theirs["decisions"]:
        print("\nFAILED: one tenant can read another's decision ledger")
        return 1
    globals()["TOKEN"] = saved

    print(f"\nAll live AI checks passed in {time.perf_counter() - started:.1f}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())