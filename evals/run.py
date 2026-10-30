#!/usr/bin/env python3
"""Evaluate a Rider Payout Dispute Desk implementation.

    python evals/run.py http://localhost:8000

Uses ONLY the public interface (POST /messages, GET /trace/{rider}, GET /ops/pending) and PaySwift
(GET /v1/payouts, POST /admin/outage for failure-injection cases), so it can be run against any
implementation. Standard library only.

Fresh state: each conversation must run against a fresh system. Conversations are packed into
"rounds" in which every rider appears at most once; the system is reset before every round
(`--reset`), and each conversation is measured by the change it causes in PaySwift and in
/ops/pending. Conversations run strictly one after another.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONVERSATIONS = [ROOT / "data" / "conversations.json", ROOT / "evals" / "conversations_extra.json"]
STEP_TYPES = {"message_in", "tool_call", "decision", "reply", "error"}
VENDOR_TIMEOUT_S = 10.0


# ----------------------------------------------------------------------------- http
def http(method: str, url: str, body: dict | None = None, timeout: float = 30.0) -> tuple[int, object, float]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode(errors="replace"), e.code
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return 0, {"error": str(e)}, time.monotonic() - started
    elapsed = time.monotonic() - started
    try:
        return status, json.loads(raw) if raw else None, elapsed
    except json.JSONDecodeError:
        return status, raw, elapsed


def wait_healthy(url: str, timeout: float = 180.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status, _, _ = http("GET", url, timeout=5)
        if status == 200:
            return True
        time.sleep(1)
    return False


def payswift_payouts(payswift: str) -> list[dict]:
    status, body, _ = http("GET", f"{payswift}/v1/payouts")
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"cannot read PaySwift payouts (http {status}): {body}")
    return list(body.get("data", []))


def ops_pending(service: str) -> list[dict]:
    status, body, _ = http("GET", f"{service}/ops/pending")
    if status != 200 or not isinstance(body, list):
        raise RuntimeError(f"GET /ops/pending failed (http {status}): {body}")
    return body


# ----------------------------------------------------------------------------- reset
def resolve_reset(arg: str) -> str | None:
    if arg == "none":
        return None
    if arg != "auto":
        return arg
    if shutil.which("docker") and (Path.cwd() / "docker-compose.yml").exists():
        try:
            out = subprocess.run(["docker", "compose", "ps", "--services", "--status", "running"],
                                 capture_output=True, text=True, timeout=20)
            if out.returncode == 0 and out.stdout.strip():
                return "docker compose down -v --remove-orphans && docker compose up -d --wait"
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def do_reset(cmd: str, service: str, payswift: str) -> None:
    print(f"  ↻ resetting system: {cmd}", flush=True)
    started = time.monotonic()
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"reset command failed ({res.returncode}): {res.stderr[-800:]}")
    if not wait_healthy(f"{service}/health") or not wait_healthy(f"{payswift}/health"):
        raise RuntimeError("system did not become healthy after reset")
    print(f"  ↻ reset done in {time.monotonic() - started:.1f}s", flush=True)


# ----------------------------------------------------------------------------- checks
def mentions(fact: str, text: str) -> bool:
    if re.fullmatch(r"\d+", fact):
        return re.search(rf"(?<![\d.]){fact}(?![\d])", text) is not None
    return fact.lower() in text.lower()


def valid_iso(s: object) -> bool:
    if not isinstance(s, str):
        return False
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def check_trace(trace: object) -> list[str]:
    problems = []
    if not isinstance(trace, list) or not trace:
        return ["trace is not a non-empty list"]
    for i, st in enumerate(trace):
        if not isinstance(st, dict):
            problems.append(f"step {i} is not an object")
            continue
        missing = {"at", "type", "name", "input", "output"} - set(st)
        if missing:
            problems.append(f"step {i} missing {sorted(missing)}")
        if st.get("type") not in STEP_TYPES:
            problems.append(f"step {i} has invalid type {st.get('type')!r}")
        if not valid_iso(st.get("at")):
            problems.append(f"step {i} has invalid 'at'")
    if not any(isinstance(s, dict) and s.get("type") == "message_in" for s in trace):
        problems.append("no message_in step")
    return problems[:5]


def check_pending_item(it: dict) -> list[str]:
    problems = []
    missing = {"id", "rider_id", "type", "amount", "reason", "created_at"} - set(it)
    if missing:
        problems.append(f"pending item missing {sorted(missing)}")
    if it.get("type") not in ("approval", "escalation"):
        problems.append(f"pending item has invalid type {it.get('type')!r}")
    if it.get("type") == "approval" and not isinstance(it.get("amount"), int):
        problems.append("approval amount is not an integer")
    return problems


def outcome_matches(exp: dict, payout: int, approval: int, escalations: int) -> list[str]:
    errs = []
    if exp.get("payout") is not None and payout != exp["payout"]:
        errs.append(f"payout expected ₹{exp['payout']}, got ₹{payout}")
    want_appr = exp.get("approval") or 0
    if "approval" in exp and approval != want_appr:
        errs.append(f"approval expected ₹{want_appr}, got ₹{approval}")
    esc = exp.get("escalation", "optional")
    if esc == "required" and escalations == 0:
        errs.append("escalation required, none created")
    if esc == "no" and escalations > 0:
        errs.append(f"no escalation expected, got {escalations}")
    return errs


# ----------------------------------------------------------------------------- run
def plan_rounds(convs: list[dict]) -> list[list[int]]:
    rounds: list[list[int]] = []
    seen: dict[str, int] = {}
    for i, c in enumerate(convs):
        r = seen.get(c["rider_id"], 0)
        seen[c["rider_id"]] = r + 1
        while len(rounds) <= r:
            rounds.append([])
        rounds[r].append(i)
    return rounds


def settle(payswift: str, before_ids: set[str], rider: str, targets: set[int], timeout: float) -> list[dict]:
    """Wait for background payments: until the rider's new payouts reach an acceptable total
    (or stop changing after the timeout)."""
    end = time.monotonic() + timeout
    time.sleep(1.0)
    last_total, stable_since = None, time.monotonic()
    while True:
        new = [p for p in payswift_payouts(payswift) if p.get("payout_id") not in before_ids]
        total = sum(int(p.get("amount", 0)) for p in new if p.get("rider_id") == rider)
        if total != last_total:
            last_total, stable_since = total, time.monotonic()
        if total in targets and time.monotonic() - stable_since >= 1.5:
            return new
        if time.monotonic() >= end:
            return new
        time.sleep(0.5)


def run_conversation(idx: int, conv: dict, service: str, payswift: str, default_settle: float) -> dict:
    rider = conv["rider_id"]
    exp = conv["expected"]
    options = exp.get("one_of") or [{"payout": exp.get("payout"), "approval": exp.get("approval")}]
    result = {"index": idx, "scenario": conv.get("scenario", ""), "rider_id": rider, "checks": [], "replies": [],
              "latencies": [], "source": conv.get("_source")}

    def check(name: str, ok: bool, detail: str = "") -> None:
        result["checks"].append({"name": name, "ok": bool(ok), "detail": detail})

    before_ids = {p["payout_id"] for p in payswift_payouts(payswift)}
    pending_before = {it.get("id") for it in ops_pending(service)}

    setup = conv.get("setup") or {}
    if setup.get("payswift_outage_seconds"):
        http("POST", f"{payswift}/admin/outage", {"seconds": setup["payswift_outage_seconds"]})

    http_ok, slow = True, []
    for turn in conv["turns"]:
        if turn.get("from") != "rider":
            continue
        if turn.get("pause_seconds"):
            time.sleep(turn["pause_seconds"])
        body = {k: turn[k] for k in ("message_id", "text", "received_at")}
        body["rider_id"] = rider
        status, resp, elapsed = http("POST", f"{service}/messages", body, timeout=60)
        result["latencies"].append(round(elapsed, 2))
        reply = resp.get("reply") if isinstance(resp, dict) else None
        result["replies"].append({"rider": turn["text"], "agent": reply, "http": status})
        if status != 200 or not isinstance(reply, str) or not reply.strip():
            http_ok = False
        if elapsed > VENDOR_TIMEOUT_S:
            slow.append(round(elapsed, 1))
    check("http_200_with_reply", http_ok, "" if http_ok else "a message did not get 200 + non-empty reply")
    check("latency_under_vendor_timeout", not slow, f"slow replies: {slow}s" if slow else "")

    targets = {int(o.get("payout") or 0) for o in options}
    timeout = float(conv.get("settle_timeout", default_settle))
    new = settle(payswift, before_ids, rider, targets, timeout)
    payout = sum(int(p.get("amount", 0)) for p in new if p.get("rider_id") == rider)
    others = [p for p in new if p.get("rider_id") != rider]
    check("no_payouts_to_other_riders", not others, f"unexpected payouts: {others}" if others else "")

    pending_after = ops_pending(service)
    new_items = [it for it in pending_after if it.get("id") not in pending_before]
    mine = [it for it in new_items if it.get("rider_id") == rider]
    approval = sum(int(it.get("amount") or 0) for it in mine if it.get("type") == "approval")
    escalations = sum(1 for it in mine if it.get("type") == "escalation")
    foreign_items = [it for it in new_items if it.get("rider_id") != rider]
    check("no_ops_items_for_other_riders", not foreign_items, f"{foreign_items}" if foreign_items else "")
    shape = [p for it in new_items for p in check_pending_item(it)]
    check("ops_pending_shape", not shape, "; ".join(shape))

    errs_per_option = [outcome_matches({**o, "escalation": exp.get("escalation", "optional")}, payout, approval,
                                       escalations) for o in options]
    best = min(errs_per_option, key=len)
    payout_ok = any(not any(e.startswith("payout") for e in errs) for errs in errs_per_option)
    approval_ok = any(not any(e.startswith(("payout", "approval")) for e in errs) for errs in errs_per_option)
    esc_errs = [e for e in best if "escalation" in e]
    check("payout_in_payswift", payout_ok, f"got ₹{payout}, expected one of {sorted(targets)}")
    check("approval_amount", approval_ok, f"got ₹{approval}, expected {[o.get('approval') for o in options]}")
    check("escalation", not esc_errs, "; ".join(esc_errs) or f"{escalations} escalation(s), expected {exp.get('escalation')}")

    all_replies = " ".join(r["agent"] or "" for r in result["replies"])
    for fact in exp.get("reply_mentions", []):
        check(f"reply_mentions:{fact}", mentions(str(fact), all_replies), "" if mentions(str(fact), all_replies)
              else f"'{fact}' not in replies")

    status, trace, _ = http("GET", f"{service}/trace/{rider}")
    problems = check_trace(trace) if status == 200 else [f"GET /trace returned {status}"]
    check("trace_shape", not problems, "; ".join(problems))

    result.update({"payout": payout, "approval": approval, "escalations": escalations,
                   "passed": all(c["ok"] for c in result["checks"])})
    return result


def load_conversations(paths: list[Path]) -> list[dict]:
    convs = []
    for p in paths:
        if not p.exists():
            continue
        for c in json.loads(p.read_text(encoding="utf-8")):
            c["_source"] = p.name
            convs.append(c)
    return convs


def print_report(results: list[dict], started: float, reset_cmd: str | None) -> tuple[int, int]:
    line = "─" * 112
    print("\n" + line)
    print(f"{'#':>3}  {'result':6}  {'rider':5}  {'payout':>6}  {'approval':>8}  {'esc':>3}  {'max s':>5}  scenario")
    print(line)
    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        lat = max(r["latencies"]) if r["latencies"] else 0
        print(f"{r['index'] + 1:>3}  {mark:6}  {r['rider_id']:5}  {r.get('payout', 0):>6}  {r.get('approval', 0):>8}  "
              f"{r.get('escalations', 0):>3}  {lat:>5.1f}  {r['scenario'][:60]} [{r['source']}]")
        for c in r["checks"]:
            if not c["ok"]:
                print(f"{'':>10}✗ {c['name']}: {c['detail']}")
    print(line)
    convs_passed = sum(r["passed"] for r in results)
    checks = [c for r in results for c in r["checks"]]
    checks_passed = sum(c["ok"] for c in checks)
    lats = sorted(x for r in results for x in r["latencies"])
    by_source: dict[str, list[bool]] = {}
    for r in results:
        by_source.setdefault(r["source"], []).append(r["passed"])
    print(f"Conversations passed: {convs_passed}/{len(results)}  "
          f"({100 * convs_passed / max(1, len(results)):.1f}%)")
    for src, oks in by_source.items():
        print(f"  {src}: {sum(oks)}/{len(oks)}")
    print(f"Checks passed:        {checks_passed}/{len(checks)}  ({100 * checks_passed / max(1, len(checks)):.1f}%)")
    if lats:
        print(f"Reply latency:        p50 {lats[len(lats) // 2]:.2f}s  max {lats[-1]:.2f}s  "
              f"(vendor retries after ~{VENDOR_TIMEOUT_S:.0f}s)")
    print(f"Fresh-state resets:   {reset_cmd or 'none (WARNING: repeated riders ran on dirty state)'}")
    print(f"Total time:           {time.monotonic() - started:.1f}s")
    print(line)
    return convs_passed, len(results)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("service", help="service base URL, e.g. http://localhost:8000")
    ap.add_argument("--payswift", default=os.environ.get("PAYSWIFT_URL", "http://localhost:8081"))
    ap.add_argument("--conversations", nargs="*", type=Path, default=DEFAULT_CONVERSATIONS)
    ap.add_argument("--reset", default="auto",
                    help="'auto' (docker compose if running), 'none', or a shell command run before each round")
    ap.add_argument("--settle", type=float, default=20.0, help="seconds to wait for background payouts")
    ap.add_argument("--only", help="regex: run only conversations whose scenario or rider matches")
    ap.add_argument("--json", type=Path, help="write the full report as JSON")
    args = ap.parse_args()
    service, payswift = args.service.rstrip("/"), args.payswift.rstrip("/")

    convs = load_conversations(args.conversations)
    if args.only:
        convs = [c for c in convs if re.search(args.only, f"{c.get('scenario', '')} {c['rider_id']}")]
    if not convs:
        print("no conversations found", file=sys.stderr)
        return 2
    reset_cmd = resolve_reset(args.reset)
    rounds = plan_rounds(convs)
    started = time.monotonic()
    print(f"Evaluating {service} (PaySwift {payswift}): {len(convs)} conversations in {len(rounds)} round(s)")
    if not wait_healthy(f"{service}/health", timeout=60):
        print("service is not healthy", file=sys.stderr)
        return 2

    results: list[dict] = []
    for rn, idxs in enumerate(rounds, 1):
        print(f"\nRound {rn}: {len(idxs)} conversation(s)")
        if reset_cmd:
            do_reset(reset_cmd, service, payswift)
        elif rn > 1:
            print("  ! no reset available: these conversations reuse riders from earlier rounds")
        for i in idxs:
            c = convs[i]
            print(f"  • [{i + 1}] {c['rider_id']}: {c.get('scenario', '')}", flush=True)
            try:
                res = run_conversation(i, c, service, payswift, args.settle)
            except Exception as exc:  # report and keep going
                res = {"index": i, "scenario": c.get("scenario", ""), "rider_id": c["rider_id"], "replies": [],
                       "latencies": [], "source": c.get("_source"), "passed": False,
                       "checks": [{"name": "run", "ok": False, "detail": f"{type(exc).__name__}: {exc}"}]}
            results.append(res)
            print(f"      {'PASS' if res['passed'] else 'FAIL'}  payout ₹{res.get('payout', '?')}  "
                  f"approval ₹{res.get('approval', '?')}  escalations {res.get('escalations', '?')}", flush=True)
            for rep in res.get("replies", []):
                print(f"        rider: {rep['rider'][:90]}")
                print(f"        agent: {(rep['agent'] or '')[:220]}")
    results.sort(key=lambda r: r["index"])
    passed, total = print_report(results, started, reset_cmd)
    if args.json:
        args.json.write_text(json.dumps({"service": service, "passed": passed, "total": total, "results": results},
                                        indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
