#!/usr/bin/env python
"""Benchmark video chunk descriptions using Claude Code CLI as judge.

Uses a CheckEval-style binary checklist: 7 yes/no questions that check
whether a description provides enough signal to make highlight selection
decisions. Works on both old schema (freeform description) and new schema
(structured action/shot/energy/setting/quality fields).

Commands:
  score    — evaluate one chunks.json, write scored JSON
  compare  — generate side-by-side HTML from two scored JSONs
  run      — score two chunks.json files and generate comparison HTML in one step

Examples:
  python bench.py score --chunks-json eval/img8462_multi/all_chunks.json \\
      --output /tmp/bench/old_scores.json

  python bench.py run \\
      --before eval/img8462_multi/all_chunks.json \\
      --after  eval/img8462_multi_v2/all_chunks.json \\
      --output /tmp/bench/compare.html
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Checklist definition
# ---------------------------------------------------------------------------

CHECKLIST = [
    ("action_clear",    "Can you tell what is physically happening (subjects + action)?"),
    ("energy_clear",    "Can you judge the energy level (calm / medium / high-energy)?"),
    ("setting_clear",   "Can you identify the location or setting?"),
    ("shot_clear",      "Can you tell the shot type (wide, close-up, tracking, etc.)?"),
    ("quality_ok",      "Can you tell if this clip has any quality issues (blur, shake, etc.)?"),
    ("selection_ready", "Could you decide include/exclude for a highlight reel based only on this?"),
    ("concise",         "Is the signal-to-noise ratio high (not buried in verbose prose)?"),
]
KEYS = [k for k, _ in CHECKLIST]

BATCH_SIZE = 15

SYSTEM_PROMPT = "You are a video description quality evaluator. Answer questions about descriptions concisely and accurately."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_description(chunk: dict) -> str:
    """Format chunk fields into a description string for scoring."""
    if chunk.get("action"):
        parts = [f"action: {chunk['action']}"]
        if chunk.get("shot"):    parts.append(f"shot: {chunk['shot']}")
        if chunk.get("energy"):  parts.append(f"energy: {chunk['energy']}")
        if chunk.get("setting"): parts.append(f"setting: {chunk['setting']}")
        if chunk.get("quality"): parts.append(f"quality: {chunk['quality']}")
        return "\n".join(parts)
    return chunk.get("description", "(no description)")


def score_batch(chunks: list[dict], model: str) -> list[dict]:
    """Call claude CLI to score a batch of chunk descriptions. Returns list of score dicts."""
    items = [{"index": ch["index"], "description": format_description(ch)} for ch in chunks]

    questions = "\n".join(f"{i+1}. {k}: {q}" for i, (k, q) in enumerate(CHECKLIST))

    # --json-schema requires an object type; wrap the array in {scores: [...]}
    schema = {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"index": {"type": "integer"}, **{k: {"type": "boolean"} for k in KEYS}},
                    "required": ["index"] + KEYS,
                },
            }
        },
        "required": ["scores"],
    }

    prompt = (
        "Evaluate these video chunk descriptions for a highlight reel pipeline.\n"
        "For each chunk answer 7 yes/no questions.\n\n"
        f"Questions:\n{questions}\n\n"
        "Chunks:\n"
        + json.dumps(items, indent=2)
        + '\n\nReturn JSON: {"scores": [{...}, ...]} — one object per chunk.'
    )

    result = subprocess.run(
        [
            "claude", "-p", prompt,
            "--system-prompt", SYSTEM_PROMPT,
            "--model", model,
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--no-session-persistence",
            "--dangerously-skip-permissions",
        ],
        capture_output=True, text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed (exit {result.returncode}):\n{result.stderr[:500]}")

    out = json.loads(result.stdout)
    data = out.get("structured_output") or out.get("result") or out
    if isinstance(data, str):
        data = json.loads(data)
    scores = data.get("scores", data) if isinstance(data, dict) else data
    if not isinstance(scores, list):
        raise ValueError(f"expected scores list from claude, got: {type(scores)}: {str(scores)[:200]}")
    return scores


def score_chunks_json(path: str, model: str) -> list[dict]:
    """Score all chunks in a chunks.json. Returns scored list sorted by index."""
    with open(path) as f:
        data = json.load(f)
    chunks = data["chunks"]
    # ensure every chunk has an index (per-video files lack it)
    for i, ch in enumerate(chunks):
        if "index" not in ch:
            ch["index"] = i
    total = len(chunks)
    all_scores: list[dict] = []

    for i in range(0, total, BATCH_SIZE):
        batch = chunks[i:i + BATCH_SIZE]
        end = min(i + BATCH_SIZE, total) - 1
        print(f"  scoring chunks {i}–{end} ...", flush=True)
        scores = score_batch(batch, model=model)
        all_scores.extend(scores)

    # Attach display fields from original chunks
    idx_map = {ch["index"]: ch for ch in chunks}
    for score in all_scores:
        ch = idx_map.get(score["index"], {})
        score["desc_formatted"] = format_description(ch)
        score["thumb"] = ch.get("thumb", "")
        score["start"] = ch.get("start", 0)
        score["end"] = ch.get("end", 0)
        score["total"] = sum(bool(score.get(k)) for k in KEYS)

    return sorted(all_scores, key=lambda s: s["index"])


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def _score_block(sc: dict, label: str) -> str:
    total = sc.get("total", 0)
    colour = "#4a4" if total >= 6 else "#a84" if total >= 4 else "#a44"
    rows = ""
    for k, q in CHECKLIST:
        val = bool(sc.get(k))
        cls = "pass" if val else "fail"
        rows += f'<div class="row"><span class="{cls}">{"✓" if val else "✗"}</span> {q[:52]}</div>\n'
    desc = sc.get("desc_formatted", "").replace("\n", "<br>")
    return (
        f'<div class="sblock">'
        f'<div class="slabel">{label} <span class="tot" style="color:{colour}">{total}/7</span></div>'
        f'<div class="dtext">{desc}</div>'
        f'<div class="checks">{rows}</div>'
        f'</div>'
    )


def generate_html(
    before: list[dict],
    after: list[dict] | None,
    output_path: str,
    label_before: str = "Before",
    label_after: str = "After",
) -> None:
    html_dir = Path(output_path).parent
    after_map = {s["index"]: s for s in after} if after else {}

    # Summary stats
    def pct(scores: list[dict], key: str) -> int:
        return int(100 * sum(bool(s.get(key)) for s in scores) / len(scores)) if scores else 0

    b_avg = sum(s["total"] for s in before) / len(before) if before else 0
    a_avg = sum(s["total"] for s in after) / len(after) if after else 0

    q_rows = ""
    for k, q in CHECKLIST:
        b_p = pct(before, k)
        a_p = pct(after, k) if after else None
        delta = ""
        if a_p is not None:
            diff = a_p - b_p
            cls = "up" if diff >= 0 else "dn"
            delta = f' &rarr; {a_p}% <span class="{cls}">({diff:+d}%)</span>'
        q_rows += f'<tr><td>{q}</td><td>{b_p}%{delta}</td></tr>\n'

    cards = []
    for s in before:
        idx = s["index"]
        a = after_map.get(idx)

        thumb = s.get("thumb", "")
        if thumb and Path(thumb).exists():
            rel = os.path.relpath(thumb, html_dir)
            img_html = f'<img src="{rel}" alt="chunk {idx}">'
        else:
            img_html = '<div class="nothumb">no thumb</div>'

        after_block = _score_block(a, label_after) if a else ""
        cards.append(f'''<div class="card">
  <div class="thumb">{img_html}<div class="idx">[{idx}] {s["start"]:.1f}s–{s["end"]:.1f}s</div></div>
  <div class="scores">{_score_block(s, label_before)}{after_block}</div>
</div>''')

    delta_str = ""
    if after:
        diff = a_avg - b_avg
        cls = "up" if diff >= 0 else "dn"
        delta_str = f' &nbsp;|&nbsp; <span class="{cls}">Δ {diff:+.1f}</span>'

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Bench: {label_before} vs {label_after}</title>
<style>
  body{{font-family:monospace;background:#111;color:#eee;margin:0;padding:16px}}
  h1{{color:#fff;font-size:14px;margin:0 0 10px}}
  .summary{{background:#1a1a1a;border-radius:6px;padding:12px;margin-bottom:14px}}
  .avg{{font-size:13px;font-weight:bold;margin-bottom:8px}}
  table{{border-collapse:collapse;width:100%;font-size:11px}}
  td{{padding:3px 8px;border-bottom:1px solid #2a2a2a}}
  td:first-child{{color:#aaa;width:70%}}
  .up{{color:#4a4}}.dn{{color:#a44}}
  .card{{display:flex;gap:10px;background:#1a1a1a;border-radius:6px;padding:10px;margin-bottom:8px}}
  .thumb{{width:170px;flex-shrink:0}}
  .thumb img{{width:100%;display:block;border-radius:4px}}
  .nothumb{{width:100%;height:96px;background:#2a2a2a;display:flex;align-items:center;justify-content:center;color:#555;font-size:10px;border-radius:4px}}
  .idx{{font-size:10px;color:#666;margin-top:3px;text-align:center}}
  .scores{{display:flex;gap:8px;flex:1;min-width:0}}
  .sblock{{flex:1;background:#222;border-radius:4px;padding:8px;min-width:0}}
  .slabel{{font-size:11px;font-weight:bold;color:#aaa;margin-bottom:3px}}
  .tot{{font-size:13px}}
  .dtext{{font-size:10px;color:#777;margin:4px 0 6px;line-height:1.5}}
  .checks{{font-size:10px}}
  .row{{margin:2px 0;color:#777}}
  .pass{{color:#4a4;font-weight:bold}}.fail{{color:#a44;font-weight:bold}}
</style></head><body>
<h1>Benchmark: {label_before} vs {label_after} &nbsp;·&nbsp; {len(before)} chunks</h1>
<div class="summary">
  <div class="avg">{label_before}: {b_avg:.1f}/7 &nbsp;|&nbsp; {label_after}: {a_avg:.1f}/7{delta_str}</div>
  <table><tr><td colspan="2" style="color:#555;font-weight:bold">Checklist question</td></tr>
  {q_rows}</table>
</div>
{"".join(cards)}
</body></html>"""

    with open(output_path, "w") as f:
        f.write(html)
    print(f"wrote {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="Benchmark chunk descriptions with Claude CLI as judge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd")

    sc = sub.add_parser("score", help="score a chunks.json → scored JSON")
    sc.add_argument("--chunks-json", required=True)
    sc.add_argument("--output", required=True)
    sc.add_argument("--model", default="haiku")

    cm = sub.add_parser("compare", help="generate HTML from two scored JSONs")
    cm.add_argument("--before", required=True)
    cm.add_argument("--after", required=True)
    cm.add_argument("--output", required=True)
    cm.add_argument("--label-before", default="Before")
    cm.add_argument("--label-after", default="After")

    ru = sub.add_parser("run", help="score two chunks.json files + generate HTML")
    ru.add_argument("--before", required=True, help="Old/baseline chunks.json")
    ru.add_argument("--after",  required=True, help="New chunks.json")
    ru.add_argument("--output", required=True, help="Output HTML path")
    ru.add_argument("--label-before", default="Before")
    ru.add_argument("--label-after",  default="After")
    ru.add_argument("--model", default="haiku")

    args = p.parse_args()

    if args.cmd == "score":
        print(f"scoring {args.chunks_json} ...")
        scores = score_chunks_json(args.chunks_json, model=args.model)
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(scores, f, indent=2)
        avg = sum(s["total"] for s in scores) / len(scores)
        print(f"done — {len(scores)} chunks, avg {avg:.1f}/7  →  {args.output}")

    elif args.cmd == "compare":
        before = json.load(open(args.before))
        after  = json.load(open(args.after))
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        generate_html(before, after, args.output, args.label_before, args.label_after)

    elif args.cmd == "run":
        print(f"scoring {args.before} ...")
        before = score_chunks_json(args.before, model=args.model)
        print(f"scoring {args.after} ...")
        after  = score_chunks_json(args.after,  model=args.model)
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        generate_html(before, after, args.output, args.label_before, args.label_after)

    else:
        p.print_help()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
