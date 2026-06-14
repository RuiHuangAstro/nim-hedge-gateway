#!/usr/bin/env python3
"""NIM Proxy daily log health check.

Reads the last 2 days of request logs + response archive,
performs anomaly detection with sample extraction, and prints
a summary report. Designed for Hermes cron (no_agent=True).
"""
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import statistics

LOG_DIR = Path(os.environ.get("NIM_PROXY_LOG_DIR", "/home/huangrui/program/playground/NIM_proxy/logs"))
ARCHIVE_FILE = LOG_DIR / "response_archive.jsonl"
HEALTH_FILE = LOG_DIR.parent / "health_state.json"
NOW = datetime.now(timezone.utc)
TWO_DAYS_AGO = NOW - timedelta(days=2)

# Thresholds
SHORT_RESPONSE_CHARS = 15      # suspicious if winner preview < this
LONG_LATENCY_MS = 300_000      # >5 min
MANY_CANDIDATES = 8            # hedging thrash threshold
MAX_SAMPLES = 5                # samples per anomaly category

REPORT_LINES = []

def p(msg=""):
    REPORT_LINES.append(msg)
    print(msg)

def load_requests(since):
    """Load request logs from current + rotated files, filtering by timestamp."""
    records = []
    for fpath in sorted(LOG_DIR.glob("requests.jsonl*")):
        try:
            with open(fpath) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        ts_str = r.get("ts", "")
                        if ts_str:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            if ts >= since:
                                r["_ts"] = ts
                                records.append(r)
                    except (json.JSONDecodeError, ValueError):
                        continue
        except FileNotFoundError:
            continue
    return records

def load_archive(since):
    """Load response archive entries within time window."""
    records = []
    try:
        with open(ARCHIVE_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    ts_str = r.get("ts", "")
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts >= since:
                            r["_ts"] = ts
                            records.append(r)
                except (json.JSONDecodeError, ValueError):
                    continue
    except FileNotFoundError:
        pass
    return records

# ── Anomaly detectors ──────────────────────────────────────────

def detect_short_responses(records):
    """Find suspiciously short successful responses (potential garbage/no-op)."""
    samples = []
    for r in records:
        if not r.get("success"):
            continue
        preview = (r.get("winner_content_preview") or "").strip()
        # Skip known benign patterns
        benign = {"HEARTBEAT_OK", "NO_REPLY"}
        if preview in benign:
            continue
        if 0 < len(preview) < SHORT_RESPONSE_CHARS:
            samples.append({
                "ts": r.get("ts", "")[:16],
                "model": r.get("winner", "?"),
                "vm": r.get("virtual_model", "?"),
                "tokens": (r.get("usage") or {}).get("completion_tokens", 0),
                "preview": preview[:80],
                "latency_ms": r.get("latency_ms", 0),
            })
    return samples

def detect_long_latency(records):
    """Find requests with extremely long latency (>5 min)."""
    samples = []
    for r in records:
        lat = r.get("latency_ms") or 0
        if lat > LONG_LATENCY_MS:
            samples.append({
                "ts": r.get("ts", "")[:16],
                "model": r.get("winner", "?"),
                "vm": r.get("virtual_model", "?"),
                "latency_s": round(lat / 1000),
                "n_candidates": len(r.get("candidates_tried", [])),
                "success": r.get("success"),
                "tokens": (r.get("usage") or {}).get("completion_tokens", 0),
                "preview": (r.get("winner_content_preview") or "")[:60],
            })
    return sorted(samples, key=lambda x: -x["latency_s"])

def detect_hedging_thrash(records):
    """Find requests that tried many candidates (hedging thrashing)."""
    samples = []
    for r in records:
        n = len(r.get("candidates_tried", []))
        if n >= MANY_CANDIDATES:
            failed_names = [c["name"] for c in r.get("candidates_tried", [])
                           if not c.get("ok", True)]
            samples.append({
                "ts": r.get("ts", "")[:16],
                "vm": r.get("virtual_model", "?"),
                "n_candidates": n,
                "success": r.get("success"),
                "winner": r.get("winner", "?"),
                "failed": ", ".join(sorted(set(failed_names)))[:80],
            })
    return sorted(samples, key=lambda x: -x["n_candidates"])

def detect_model_failure_bursts(records):
    """Detect time windows where a model fails many times in a row."""
    # Group by model, find consecutive failure streaks
    model_events = defaultdict(list)  # model -> [(ts, success)]
    for r in records:
        for c in r.get("candidates_tried", []):
            name = c.get("name", "?")
            model_events[name].append((r["_ts"], c.get("ok", True)))

    bursts = []
    for model, events in model_events.items():
        events.sort()
        streak = 0
        streak_start = None
        for ts, ok in events:
            if not ok:
                if streak == 0:
                    streak_start = ts
                streak += 1
            else:
                if streak >= 5:
                    bursts.append({
                        "model": model,
                        "start": streak_start.strftime("%m-%d %H:%M"),
                        "count": streak,
                        "duration_min": round((ts - streak_start).total_seconds() / 60),
                    })
                streak = 0
                streak_start = None
        # Tail streak
        if streak >= 5:
            bursts.append({
                "model": model,
                "start": streak_start.strftime("%m-%d %H:%M"),
                "count": streak,
                "duration_min": round((events[-1][0] - streak_start).total_seconds() / 60) if events else 0,
            })

    return sorted(bursts, key=lambda x: -x["count"])[:10]

def detect_repetition_in_wins(records):
    """Find winning responses with repeated content blocks."""
    samples = []
    for r in records:
        if not r.get("success"):
            continue
        preview = r.get("winner_content_preview") or ""
        if len(preview) < 60:
            continue
        # Check for ngram repetition (10-30 char blocks, 3+ times)
        found = False
        checked = set()
        for i in range(0, min(len(preview) - 40, 200), 5):
            block = preview[i:i+10]
            if block in checked or not block.strip() or block[0] == ' ' or block[-1] == ' ':
                continue
            checked.add(block)
            cnt = preview.count(block)
            if cnt >= 3:
                # Skip tool call markers (benign for kimi)
                if "<|tool_cal" in block:
                    continue
                samples.append({
                    "ts": r.get("ts", "")[:16],
                    "model": r.get("winner", "?"),
                    "pattern": block[:25],
                    "count": cnt,
                    "preview": preview[:80],
                })
                found = True
                break
        if found and len(samples) >= MAX_SAMPLES * 2:
            break
    return samples

def detect_archive_anomalies(archive_records):
    """Analyze response archive for suspicious patterns."""
    results = {
        "short_nontruncated": [],   # very short content, not finish_reason=length
        "empty_no_toolcall": [],    # empty content + no tool calls
        "garbled": [],              # low alnum/CJK ratio
        "repetition_archive": [],   # repetition in archived content
    }

    for r in archive_records:
        content = r.get("content") or ""
        category = r.get("category", "")
        fr = r.get("finish_reason") or ""
        extra_reason = (r.get("extra") or {}).get("reason", "")
        model = r.get("candidate_name", "?")

        # Short non-truncated
        if content and 0 < len(content.strip()) < 15 and "length" not in fr and "length" not in extra_reason:
            results["short_nontruncated"].append({
                "ts": r.get("ts", "")[:16],
                "model": model,
                "category": category,
                "content": repr(content[:50]),
                "finish_reason": fr,
            })

        # Empty + no tool calls
        if (not content or not content.strip()) and not r.get("tool_calls"):
            results["empty_no_toolcall"].append({
                "ts": r.get("ts", "")[:16],
                "model": model,
                "category": category,
                "finish_reason": fr or extra_reason[:40],
            })

        # Garbled: >60% non-meaningful chars
        if content and len(content) > 100:
            meaningful = sum(1 for c in content
                          if c.isalnum() or '\u4e00' <= c <= '\u9fff'
                          or '\u3000' <= c <= '\u303f' or c in '.,;:!?\'"()[]{}')
            if meaningful / len(content) < 0.4:
                results["garbled"].append({
                    "ts": r.get("ts", "")[:16],
                    "model": model,
                    "category": category,
                    "ratio": round(meaningful / len(content), 2),
                    "preview": repr(content[:60]),
                })

        # Repetition in archived content (longer check)
        if content and len(content) > 100:
            for i in range(0, min(len(content) - 80, 300), 10):
                block = content[i:i+20]
                if not block.strip() or block[0] == ' ' or block[-1] == ' ':
                    continue
                if content.count(block) >= 4:
                    results["repetition_archive"].append({
                        "ts": r.get("ts", "")[:16],
                        "model": model,
                        "category": category,
                        "pattern": repr(block[:30]),
                        "count": content.count(block),
                    })
                    break

    return results

# ── Report sections ─────────────────────────────────────────────

def report_requests_overview(records):
    total = len(records)
    if total == 0:
        p("⚠️  No request logs found in the last 2 days.")
        return

    failures = [r for r in records if not r.get("success", True)]
    fail_rate = len(failures) / total * 100

    p(f"## 📊 Request Summary (last 2 days)")
    p(f"Total: {total} | Failed: {len(failures)} ({fail_rate:.1f}%)")
    p()

    daily = defaultdict(lambda: {"total": 0, "fail": 0})
    for r in records:
        day = r["_ts"].strftime("%Y-%m-%d")
        daily[day]["total"] += 1
        if not r.get("success", True):
            daily[day]["fail"] += 1

    p("| Date | Total | Fail | Rate |")
    p("|------|-------|------|------|")
    for day in sorted(daily):
        d = daily[day]
        rate = d["fail"] / d["total"] * 100 if d["total"] else 0
        marker = " 🔴" if rate > 5 else (" 🟡" if rate > 2 else "")
        p(f"| {day} | {d['total']} | {d['fail']} | {rate:.1f}%{marker} |")
    p()

def report_model_performance(records):
    model_wins = defaultdict(int)
    model_attempts = defaultdict(int)
    model_fails_detail = defaultdict(lambda: defaultdict(int))
    model_latencies = defaultdict(list)

    for r in records:
        winner = r.get("winner")
        if winner:
            model_wins[winner] += 1
            lat = r.get("latency_ms") or 0
            if lat:
                model_latencies[winner].append(lat)
        for c in r.get("candidates_tried", []):
            name = c.get("name", "?")
            model_attempts[name] += 1
            if not c.get("ok", True):
                status = c.get("status") or "unknown"
                model_fails_detail[name][status] += 1

    p("## 🤖 Model Performance")
    p("| Model | Wins | Attempts | Win% | p50(ms) | p90(ms) |")
    p("|-------|------|----------|------|---------|---------|")
    for m in sorted(model_wins, key=lambda x: -model_wins[x]):
        wins = model_wins[m]
        att = model_attempts[m]
        wr = wins / att * 100 if att else 0
        lats = sorted(model_latencies[m])
        p50 = lats[len(lats)//2] if lats else 0
        p90 = lats[int(len(lats)*0.9)] if len(lats) > 1 else p50
        marker = " 🔴" if wr < 5 else (" 🟡" if wr < 20 else "")
        p(f"| {m} | {wins} | {att} | {wr:.1f}%{marker} | {p50:.0f} | {p90:.0f} |")
    p()

    if model_fails_detail:
        p("<details><summary>❌ Candidate Failure Details</summary>")
        p()
        for m in sorted(model_fails_detail):
            p(f"**{m}**:")
            for status, cnt in sorted(model_fails_detail[m].items(), key=lambda x: -x[1]):
                p(f"  - {cnt}× {status}")
            p()
        p("</details>")
        p()

def report_http_errors(records):
    special_errors = defaultdict(lambda: defaultdict(int))
    for r in records:
        for ce in r.get("candidate_errors", []):
            err_str = str(ce.get("error", ""))
            candidate = ce.get("candidate", "?")
            for code in ["429", "504", "503", "404", "500"]:
                if code in err_str:
                    special_errors[code][candidate] += 1
    if special_errors:
        p("## ⚠️ HTTP Error Codes")
        for code in sorted(special_errors):
            p(f"**{code}**:")
            for m, cnt in sorted(special_errors[code].items(), key=lambda x: -x[1]):
                p(f"  - {m}: {cnt}×")
            p()

def report_anomaly_samples(records):
    """The core new section: sample-based anomaly reports."""

    # ── Anomaly short ──
    short = detect_short_responses(records)
    if short:
        p(f"## 📏 Anomaly: Suspiciously Short Responses ({len(short)})")
        p("Successful responses with <15 chars content (excluding HEARTBEAT/NO_REPLY):")
        p()
        for s in short[:MAX_SAMPLES]:
            p(f"- `{s['ts']}` {s['model']}: \"{s['preview']}\" ({s['tokens']}tok, {s['latency_ms']}ms)")
        if len(short) > MAX_SAMPLES:
            p(f"- ... and {len(short) - MAX_SAMPLES} more")
        p()

    # ── Anomaly long ──
    long = detect_long_latency(records)
    if long:
        p(f"## 🐌 Anomaly: Very Long Latency >5min ({len(long)})")
        for s in long[:MAX_SAMPLES]:
            ok = "✅" if s["success"] else "❌"
            p(f"- `{s['ts']}` {s['model']}: {s['latency_s']}s {ok} ({s['n_candidates']} candidates, {s['tokens']}tok)")
            if s["preview"]:
                p(f"  preview: \"{s['preview'][:60]}\"")
        if len(long) > MAX_SAMPLES:
            p(f"- ... and {len(long) - MAX_SAMPLES} more")
        p()

    # ── Hedging thrash ──
    thrash = detect_hedging_thrash(records)
    if thrash:
        p(f"## 🔄 Anomaly: Hedging Thrash ≥{MANY_CANDIDATES} candidates ({len(thrash)})")
        for s in thrash[:MAX_SAMPLES]:
            ok = "✅" if s["success"] else "❌"
            p(f"- `{s['ts']}` {s['vm']}: {s['n_candidates']} candidates {ok} winner={s['winner']}")
            p(f"  failed: {s['failed']}")
        if len(thrash) > MAX_SAMPLES:
            p(f"- ... and {len(thrash) - MAX_SAMPLES} more")
        p()

    # ── Model failure bursts ──
    bursts = detect_model_failure_bursts(records)
    if bursts:
        p(f"## 💥 Anomaly: Model Failure Bursts (≥5 consecutive)")
        for b in bursts[:MAX_SAMPLES]:
            p(f"- {b['model']}: {b['count']} consecutive failures starting {b['start']} ({b['duration_min']}min)")
        p()

    # ── Repetition in winning responses ──
    rep = detect_repetition_in_wins(records)
    if rep:
        p(f"## 🔁 Anomaly: Repetition in Winning Responses ({len(rep)})")
        for s in rep[:MAX_SAMPLES]:
            p(f"- `{s['ts']}` {s['model']}: `{s['pattern']}` ×{s['count']}")
            p(f"  preview: \"{s['preview'][:60]}\"")
        if len(rep) > MAX_SAMPLES:
            p(f"- ... and {len(rep) - MAX_SAMPLES} more")
        p()

def report_archive_anomalies(archive_records):
    if not archive_records:
        return

    p("## 🗄️ Response Archive Anomalies")

    # Category breakdown
    cats = Counter(r.get("category", "unknown") for r in archive_records)
    p("| Category | Count |")
    p("|----------|-------|")
    for c, n in cats.most_common():
        p(f"| {c} | {n} |")
    p()

    anomalies = detect_archive_anomalies(archive_records)

    if anomalies["short_nontruncated"]:
        p(f"### 📏 Short Non-Truncated ({len(anomalies['short_nontruncated'])})")
        for s in anomalies["short_nontruncated"][:MAX_SAMPLES]:
            p(f"- `{s['ts']}` {s['model']}: {s['content']} (fr={s['finish_reason']})")
        if len(anomalies["short_nontruncated"]) > MAX_SAMPLES:
            p(f"- ... {len(anomalies['short_nontruncated']) - MAX_SAMPLES} more")
        p()

    if anomalies["empty_no_toolcall"]:
        p(f"### 🫥 Empty + No Tool Calls ({len(anomalies['empty_no_toolcall'])})")
        for s in anomalies["empty_no_toolcall"][:MAX_SAMPLES]:
            p(f"- `{s['ts']}` {s['model']}: fr={s['finish_reason']}")
        if len(anomalies["empty_no_toolcall"]) > MAX_SAMPLES:
            p(f"- ... {len(anomalies['empty_no_toolcall']) - MAX_SAMPLES} more")
        p()

    if anomalies["garbled"]:
        p(f"### 🗑️ Garbled Content ({len(anomalies['garbled'])})")
        for s in anomalies["garbled"][:MAX_SAMPLES]:
            p(f"- `{s['ts']}` {s['model']}: ratio={s['ratio']} {s['preview']}")
        p()

    if anomalies["repetition_archive"]:
        p(f"### 🔁 Archive Repetition ({len(anomalies['repetition_archive'])})")
        for s in anomalies["repetition_archive"][:MAX_SAMPLES]:
            p(f"- `{s['ts']}` {s['model']}: {s['pattern']} ×{s['count']} ({s['category']})")
        if len(anomalies["repetition_archive"]) > MAX_SAMPLES:
            p(f"- ... {len(anomalies['repetition_archive']) - MAX_SAMPLES} more")
        p()

def report_health_state():
    try:
        with open(HEALTH_FILE) as f:
            hs = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return

    candidates = hs.get("candidates", {})
    if not candidates:
        return

    p("## 💓 Health Scores (current)")
    by_vm = defaultdict(list)
    for k, v in candidates.items():
        vm = k.split("/")[0] if "/" in k else k
        by_vm[vm].append((k, v))

    for vm in sorted(by_vm):
        items = sorted(by_vm[vm], key=lambda x: x[1].get("score", 0), reverse=True)
        line_parts = []
        for k, v in items:
            name = k.split("/")[-1]
            score = v.get("score", 0)
            marker = "🔴" if score < 0 else ("🟡" if score < 0.3 else "")
            line_parts.append(f"{name}={score:.2f}{marker}")
        p(f"**{vm}**: {', '.join(line_parts)}")
    p()

def main():
    p(f"# 🔍 NIM Proxy Daily Health Report")
    p(f"Generated: {NOW.strftime('%Y-%m-%d %H:%M UTC')}")
    p(f"Window: {TWO_DAYS_AGO.strftime('%m-%d %H:%M')} → now")
    p()

    records = load_requests(TWO_DAYS_AGO)
    archive_records = load_archive(TWO_DAYS_AGO)

    report_requests_overview(records)
    report_model_performance(records)
    report_http_errors(records)
    report_anomaly_samples(records)
    report_archive_anomalies(archive_records)
    report_health_state()

    # Final verdict
    total = len(records)
    failures = sum(1 for r in records if not r.get("success", True))
    fail_rate = failures / total * 100 if total else 0

    p("---")
    if fail_rate > 5:
        p(f"🔴 **ALERT**: Failure rate {fail_rate:.1f}% exceeds 5% threshold!")
    elif fail_rate > 2:
        p(f"🟡 **WARNING**: Failure rate {fail_rate:.1f}% slightly elevated.")
    else:
        p(f"🟢 **OK**: Failure rate {fail_rate:.1f}% within normal range.")

if __name__ == "__main__":
    main()
