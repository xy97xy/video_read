#!/usr/bin/env python
"""Video highlight pipeline.

Two modes:
  describe  — TransNetV2 scene detection + Qwen descriptions → chunks.json
  cut       — ffmpeg cut + concat from chunks.json + selected indices
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import torch
from transformers import AutoProcessor, BitsAndBytesConfig, Qwen2_5_VLForConditionalGeneration
from transnetv2_pytorch import TransNetV2
from qwen_vl_utils import process_vision_info

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pipeline")

QWEN_MODEL = "Qwen/Qwen2.5-VL-7B-Instruct"
MAX_CHUNK_SECONDS = 10
SCENE_FPS = 25


# ---------------------------------------------------------------------------
# Stage 1: scene detection
# ---------------------------------------------------------------------------

def detect_scenes(video_path: str, threshold: float = 0.5, duration: float | None = None) -> list[dict]:
    log.info("detecting scenes with TransNetV2...")
    model = TransNetV2()
    model.eval()
    _, predictions, _ = model.predict_video(video_path)
    preds = predictions.numpy()

    cut_frames = [i for i, p in enumerate(preds) if p > threshold]
    boundaries = [0] + cut_frames + [len(preds) - 1]

    scenes = []
    for i in range(len(boundaries) - 1):
        s = round(boundaries[i] / SCENE_FPS, 2)
        e = round(boundaries[i + 1] / SCENE_FPS, 2)
        if duration:
            s = min(s, duration)
            e = min(e, duration)
        if e - s > 1.0:
            scenes.append({"start": s, "end": e})

    log.info(f"found {len(scenes)} scene(s)")
    return scenes


def split_long_scenes(scenes: list[dict], max_chunk: int = MAX_CHUNK_SECONDS) -> list[dict]:
    chunks = []
    for scene in scenes:
        duration = scene["end"] - scene["start"]
        if duration <= max_chunk:
            chunks.append(scene)
        else:
            t = scene["start"]
            while t < scene["end"]:
                end = min(t + max_chunk, scene["end"])
                chunks.append({"start": round(t, 2), "end": round(end, 2)})
                t = end
    return chunks


# ---------------------------------------------------------------------------
# Stage 2: Qwen descriptions
# ---------------------------------------------------------------------------

def load_qwen():
    log.info(f"loading {QWEN_MODEL} (4-bit)...")
    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
    )
    processor = AutoProcessor.from_pretrained(QWEN_MODEL)
    model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
        QWEN_MODEL, quantization_config=bnb, device_map="cuda:0"
    )
    model.eval()
    log.info("qwen loaded")
    return model, processor


_QWEN_PROMPT = (
    'Analyze this video clip. Reply with ONLY this JSON — no markdown, no extra text, all values must be plain strings:\n'
    '{"action": "one sentence: who is present and what they are doing",\n'
    ' "shot": "shot type only, e.g. close-up, wide shot, tracking shot, aerial",\n'
    ' "energy": "one word: low, medium, or high",\n'
    ' "setting": "brief location context, e.g. beach at sunset, indoor market, hotel room",\n'
    ' "quality": "good, or note any issues: blur, shaky, overexposed, obstructed"}'
)


def _flatten(value) -> str | None:
    """Convert any JSON value to a readable string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        return " ".join(str(v) for v in value).strip() or None
    if isinstance(value, dict):
        parts = []
        for v in value.values():
            if isinstance(v, (str, list)):
                parts.append(_flatten(v))
        return " ".join(p for p in parts if p) or str(value)
    return str(value).strip() or None


def _parse_qwen_json(raw: str) -> dict:
    """Try to extract a JSON object from Qwen output; fall back gracefully."""
    # Strip markdown code fences (```json ... ```)
    text = re.sub(r"```(?:json)?\s*", "", raw).strip()

    # Try the whole cleaned text first, then scan for first { ... last }
    candidates = [text]
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidates.append(text[first_brace:last_brace + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and "action" in parsed:
                return {
                    "action": _flatten(parsed.get("action")) or raw.strip(),
                    "shot": _flatten(parsed.get("shot")),
                    "energy": _flatten(parsed.get("energy")),
                    "setting": _flatten(parsed.get("setting")),
                    "quality": _flatten(parsed.get("quality")),
                }
        except json.JSONDecodeError:
            pass

    log.warning("qwen JSON parse failed — storing raw text in action field")
    return {"action": raw.strip(), "shot": None, "energy": None, "setting": None, "quality": None}


def describe_chunk(model, processor, seg_path: str, start: float, end: float) -> dict:
    messages = [{
        "role": "user",
        "content": [
            {"type": "video", "video": seg_path, "fps": 1.0, "max_pixels": 360 * 420},
            {"type": "text", "text": _QWEN_PROMPT},
        ],
    }]
    text_input = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    image_inputs, video_inputs, video_kwargs = process_vision_info(messages, return_video_kwargs=True)
    if isinstance(video_kwargs.get("fps"), list):
        video_kwargs["fps"] = video_kwargs["fps"][0]
    inputs = processor(
        text=[text_input], images=image_inputs, videos=video_inputs,
        return_tensors="pt", **video_kwargs,
    ).to(model.device)
    with torch.no_grad():
        outputs = model.generate(**inputs, max_new_tokens=300, do_sample=False)
    decoded = processor.decode(outputs[0], skip_special_tokens=True)
    for marker in ("assistant\n", "Assistant: ", "assistant: "):
        if marker in decoded:
            decoded = decoded.split(marker, 1)[1].strip()
            break
    else:
        decoded = decoded.strip()
    return _parse_qwen_json(decoded)


def cut_segment(video_path: str, start: float, end: float, out_path: str) -> None:
    cmd = ["ffmpeg", "-y", "-i", video_path,
           "-ss", str(start), "-t", str(end - start),
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", out_path]
    subprocess.run(cmd, check=True, capture_output=True)


def extract_thumb(video_path: str, out_path: str, seek_time: float) -> None:
    cmd = ["ffmpeg", "-y", "-ss", str(seek_time), "-i", video_path,
           "-frames:v", "1", "-q:v", "3", out_path]
    subprocess.run(cmd, check=True, capture_output=True)


def describe_all(model, processor, video_path: str, chunks: list[dict], thumbs_dir: str | None = None) -> list[dict]:
    seg_dir = tempfile.mkdtemp(prefix="pipeline_seg_")
    try:
        for i, chunk in enumerate(chunks):
            seg_path = os.path.join(seg_dir, f"seg_{i}.mp4")
            cut_segment(video_path, chunk["start"], chunk["end"], seg_path)
            if thumbs_dir:
                thumb_path = os.path.join(thumbs_dir, f"chunk_{i}.jpg")
                try:
                    extract_thumb(seg_path, thumb_path, (chunk["end"] - chunk["start"]) / 2)
                    chunk["thumb"] = thumb_path
                except subprocess.CalledProcessError:
                    log.warning(f"  thumb extraction failed for chunk {i} (segment too short?)")
            log.info(f"[{i+1}/{len(chunks)}] describing {chunk['start']:.1f}s-{chunk['end']:.1f}s ...")
            result = describe_chunk(model, processor, seg_path, chunk["start"], chunk["end"])
            chunk["action"] = result["action"]
            chunk["shot"] = result["shot"]
            chunk["energy"] = result["energy"]
            chunk["setting"] = result["setting"]
            chunk["quality"] = result["quality"]
            chunk["description"] = result["action"]  # backward compat
            log.info(f"  action:  {result['action'][:80]}")
            if result["setting"]:
                log.info(f"  setting: {result['setting']}")
            if result["energy"]:
                log.info(f"  energy:  {result['energy']}  shot: {result['shot'] or '?'}  quality: {result['quality'] or '?'}")
    finally:
        shutil.rmtree(seg_dir, ignore_errors=True)
    return chunks


# ---------------------------------------------------------------------------
# Stage 2b: speech transcription (optional, faster-whisper)
# ---------------------------------------------------------------------------

def transcribe_audio(video_path: str, model_size: str = "base", language: str | None = None) -> list[dict]:
    """Return list of {start, end, text, words} speech segments."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log.warning("faster-whisper not installed — skipping transcription (pip install faster-whisper)")
        return []

    log.info(f"transcribing audio with whisper-{model_size}...")

    # Try CUDA; fall back to CPU if the CUDA libraries aren't available
    whisper_model = None
    for device, ctype in [("cuda", "float16"), ("cpu", "int8")]:
        try:
            whisper_model = WhisperModel(model_size, device=device, compute_type=ctype)
            log.info(f"whisper loaded on {device}")
            break
        except Exception as e:
            log.warning(f"whisper on {device} failed ({e}), trying next...")

    if whisper_model is None:
        log.error("could not load whisper model — skipping transcription")
        return []

    segments_iter, info = whisper_model.transcribe(video_path, word_timestamps=True, language=language)
    log.info(f"detected language: {info.language} (prob={info.language_probability:.2f})")

    speech = []
    for seg in segments_iter:
        words = [{"word": w.word, "start": w.start, "end": w.end} for w in (seg.words or [])]
        speech.append({
            "start": round(seg.start, 2),
            "end": round(seg.end, 2),
            "text": seg.text.strip(),
            "words": words,
        })

    log.info(f"found {len(speech)} speech segment(s)")
    return speech


def _is_garbage(text: str) -> bool:
    if not text or len(text.strip('., …\t\n')) <= 1:
        return True
    if 'ʔ' in text:  # whisper noise token
        return True
    alpha = sum(c.isalpha() for c in text)
    return alpha / len(text) < 0.4


def speech_in_range(speech: list[dict], start: float, end: float) -> list[str]:
    """Return text of speech segments that overlap with [start, end], filtering noise."""
    texts = []
    for seg in speech:
        if seg["end"] > start and seg["start"] < end and not _is_garbage(seg["text"]):
            texts.append(seg["text"])
    return texts


def write_thumbs_html(
    chunks: list[dict],
    speech_data: list[dict] | dict,
    html_path: str,
    title: str = "Chunks",
) -> None:
    html_dir = Path(html_path).parent
    unique_sources = list(dict.fromkeys(ch.get("source_video") for ch in chunks if ch.get("source_video")))
    multi_source = len(unique_sources) > 1

    cards: list[str] = []
    current_src = None
    for ch in chunks:
        src = ch.get("source_video")
        idx = ch.get("index", chunks.index(ch))

        if multi_source and src != current_src:
            current_src = src
            cards.append(f'<div class="source-header">{Path(src).name if src else "unknown"}</div>')

        thumb_abs = ch.get("thumb")
        if thumb_abs and Path(thumb_abs).exists():
            rel = os.path.relpath(thumb_abs, html_dir)
            img_html = f'<img src="{rel}" alt="chunk {idx}">'
        else:
            img_html = '<div class="no-thumb">no thumbnail</div>'

        src_speech = speech_data.get(src, []) if isinstance(speech_data, dict) else speech_data
        spoken = speech_in_range(src_speech, ch["start"], ch["end"])
        speech_html = f'<div class="speech">"{" / ".join(spoken)[:70]}"</div>' if spoken else ""

        if ch.get("action"):
            meta_parts = []
            if ch.get("shot"):
                meta_parts.append(ch["shot"])
            if ch.get("energy"):
                meta_parts.append(ch["energy"] + " energy")
            if ch.get("quality") and ch["quality"].lower() != "good":
                meta_parts.append(f'<span class="warn">{ch["quality"]}</span>')
            meta_html = f'<div class="meta">{" · ".join(meta_parts)}</div>' if meta_parts else ""
            setting_html = f'<div class="setting">{ch["setting"]}</div>' if ch.get("setting") else ""
            desc_html = f'<div class="desc">{ch["action"][:120]}…</div>{setting_html}{meta_html}'
        else:
            desc = (ch.get("description") or "")[:100]
            desc_html = f'<div class="desc">{desc}…</div>'

        cards.append(f'''<div class="chunk">
  {img_html}
  <div class="info">
    <span class="index">[{idx}]</span>
    <span class="time">{ch["start"]:.1f}s – {ch["end"]:.1f}s</span>
    {speech_html}
    {desc_html}
  </div>
</div>''')

    body = "\n".join(cards)
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font-family: monospace; background: #111; color: #eee; margin: 0; padding: 16px; }}
  h1 {{ color: #fff; font-size: 14px; margin: 0 0 16px; }}
  .grid {{ display: flex; flex-wrap: wrap; gap: 10px; }}
  .source-header {{ width: 100%; padding: 8px 0 4px; font-size: 13px; font-weight: bold;
                    color: #4af; border-bottom: 1px solid #333; margin-top: 12px; }}
  .chunk {{ width: 260px; background: #1e1e1e; border-radius: 6px; overflow: hidden; }}
  .chunk img {{ width: 100%; display: block; }}
  .no-thumb {{ width: 100%; height: 146px; background: #2a2a2a; display: flex;
               align-items: center; justify-content: center; color: #555; font-size: 11px; }}
  .info {{ padding: 8px; }}
  .index {{ font-size: 18px; font-weight: bold; color: #4af; margin-right: 6px; }}
  .time {{ color: #888; font-size: 11px; }}
  .speech {{ color: #fa4; font-size: 11px; margin: 4px 0; }}
  .desc {{ font-size: 11px; color: #aaa; line-height: 1.4; margin-top: 4px; }}
  .setting {{ font-size: 10px; color: #8d8; margin-top: 3px; }}
  .meta {{ font-size: 10px; color: #88f; margin-top: 2px; font-style: italic; }}
  .warn {{ color: #f84; font-style: normal; }}
</style>
</head>
<body>
<h1>{title}</h1>
<div class="grid">
{body}
</div>
</body>
</html>"""
    with open(html_path, "w") as f:
        f.write(html)


# ---------------------------------------------------------------------------
# Stage 3: ffmpeg cut (with optional speech-aware boundary snapping)
# ---------------------------------------------------------------------------

def snap_to_silence(t: float, speech: list[dict], video_duration: float, margin: float = 0.3) -> float:
    """If t falls inside a speech segment, snap to the nearest sentence boundary."""
    for seg in speech:
        seg_start = seg["start"] - margin
        seg_end = seg["end"] + margin
        if seg_start <= t <= seg_end:
            before = seg["start"] - margin
            after = seg["end"] + margin
            snapped = before if abs(t - before) <= abs(t - after) else after
            snapped = max(0.0, min(snapped, video_duration))
            log.info(f"  snapped {t:.2f}s → {snapped:.2f}s (speech: '{seg['text'][:50]}')")
            return round(snapped, 2)
    return t


def concat_chunks(
    video_path: str | None,
    chunks: list[dict],
    selected: list[int],
    output_path: str,
    speech: list[dict] | dict | None = None,
    video_duration: float | dict = 0.0,
    normalize_audio: bool = True,
    scale_height: int | None = None,
) -> None:
    selected_chunks = [chunks[i] for i in selected]
    if not selected_chunks:
        log.warning("no chunks selected — nothing to cut")
        return

    has_source = all("source_video" in ch for ch in selected_chunks)
    if has_source:
        seen = []
        for ch in selected_chunks:
            if ch["source_video"] not in seen:
                seen.append(ch["source_video"])
        src_index = {src: i for i, src in enumerate(seen)}
    else:
        if video_path is None:
            raise ValueError("video_path is required when chunks lack source_video field")
        seen = [video_path]
        src_index = {video_path: 0}

    parts, inputs = [], []
    actual_duration = 0.0
    for i, ch in enumerate(selected_chunks):
        src = ch.get("source_video", video_path)
        idx = src_index[src]

        if isinstance(speech, dict):
            src_speech = speech.get(src, [])
        elif isinstance(speech, list):
            src_speech = speech
        else:
            src_speech = []

        dur = video_duration.get(src, 0.0) if isinstance(video_duration, dict) else video_duration
        s = snap_to_silence(ch["start"], src_speech, dur) if src_speech else ch["start"]
        e = snap_to_silence(ch["end"], src_speech, dur) if src_speech else ch["end"]
        actual_duration += e - s

        vf = f"trim=start={s}:end={e},setpts=PTS-STARTPTS"
        if scale_height:
            vf += f",scale=-2:{scale_height}"
        af = f"atrim=start={s}:end={e},asetpts=PTS-STARTPTS"
        if normalize_audio:
            af += ",loudnorm"
        parts.append(f"[{idx}:v]{vf}[v{i}];")
        parts.append(f"[{idx}:a]{af}[a{i}];")
        inputs.append(f"[v{i}][a{i}]")

    filter_complex = "".join(parts) + f"{''.join(inputs)}concat=n={len(selected_chunks)}:v=1:a=1[outv][outa]"
    input_args = []
    for src in seen:
        input_args += ["-i", src]

    cmd = ["ffmpeg", "-y"] + input_args + [
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-c:a", "aac", output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    log.info(f"wrote {output_path} ({actual_duration:.1f}s from {len(selected_chunks)} chunks)")


def merge_chunks_json(chunk_files: list[str]) -> dict:
    """Merge per-video chunk files into one combined dict sorted by shot_at."""
    sources_data = []
    for path in chunk_files:
        with open(path) as f:
            data = json.load(f)
        sources_data.append(data)

    def src_sort_key(d):
        return (0, d["shot_at"]) if d.get("shot_at") else (1, d["video"])
    sources_data.sort(key=src_sort_key)

    sources = [{"video": d["video"], "shot_at": d.get("shot_at"), "duration": d["duration"]}
               for d in sources_data]

    all_chunks = []
    for d in sources_data:
        all_chunks.extend(d["chunks"])

    for idx, chunk in enumerate(all_chunks):
        chunk["index"] = idx

    speech = {d["video"]: d.get("speech", []) for d in sources_data}

    return {"sources": sources, "chunks": all_chunks, "speech": speech}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def get_duration(video_path: str) -> float:
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", video_path]
    info = json.loads(subprocess.run(cmd, capture_output=True, text=True).stdout)
    return float(info["format"]["duration"])


def get_shot_at(video_path: str) -> str | None:
    """Return ISO 8601 shot timestamp from video metadata, or None."""
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", video_path]
    info = json.loads(subprocess.run(cmd, capture_output=True, text=True).stdout)
    tags = info.get("format", {}).get("tags", {})
    return (
        tags.get("com.apple.quicktime.creationdate")
        or tags.get("creation_time")
        or None
    )


VIDEO_EXTS = {".mov", ".mp4", ".MOV", ".MP4"}

def collect_videos(videos: list[str], video_dir: str | None) -> list[dict]:
    """Return sorted list of {video, shot_at} dicts from explicit list and/or directory."""
    paths = [str(Path(v).resolve()) for v in videos]
    if video_dir:
        for p in sorted(Path(video_dir).iterdir()):
            if p.suffix in VIDEO_EXTS and str(p.resolve()) not in paths:
                paths.append(str(p.resolve()))

    entries = []
    for p in paths:
        shot_at = get_shot_at(p)
        if shot_at is None:
            log.warning(f"no timestamp metadata for {Path(p).name} — using filename order")
        entries.append({"video": p, "shot_at": shot_at})

    def sort_key(e):
        return (0, e["shot_at"]) if e["shot_at"] else (1, e["video"])

    return sorted(entries, key=sort_key)


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="mode", required=True)

    # describe mode
    d = sub.add_parser("describe", help="detect scenes + describe with Qwen → chunks.json")
    d.add_argument("--video", required=True)
    d.add_argument("--output", required=True, help="Path to write chunks.json")
    d.add_argument("--scene-threshold", type=float, default=0.5)
    d.add_argument("--max-chunk-seconds", type=int, default=MAX_CHUNK_SECONDS)
    d.add_argument("--transcribe", action="store_true", default=True,
                   help="Also transcribe audio with Whisper (default: on)")
    d.add_argument("--no-transcribe", dest="transcribe", action="store_false",
                   help="Skip audio transcription")
    d.add_argument("--whisper-model", default="base", help="Whisper model size (default: base)")
    d.add_argument("--language", default=None, help="Audio language hint for Whisper, e.g. en, ja, ko")

    # cut mode
    c = sub.add_parser("cut", help="cut + concat selected chunks → highlight video")
    c.add_argument("--chunks-json", required=True)
    c.add_argument("--selected", required=True, help="Comma-separated chunk indices, e.g. 0,2,4")
    c.add_argument("--output", required=True, help="Output video path")
    c.add_argument("--speech-aware", action="store_true", default=True,
                   help="Snap cut points to sentence boundaries (default: on)")
    c.add_argument("--no-speech-aware", dest="speech_aware", action="store_false")
    c.add_argument("--normalize-audio", action="store_true", default=True,
                   help="Normalize audio loudness across clips (default: on)")
    c.add_argument("--no-normalize-audio", dest="normalize_audio", action="store_false")
    c.add_argument("--scale", type=int, default=None, metavar="HEIGHT",
                   help="Scale output to this height in pixels, e.g. 1080 or 720 (default: source resolution)")

    # merge mode
    m = sub.add_parser("merge", help="combine per-video chunks.json → all_chunks.json")
    m.add_argument("--output-dir", required=True, help="Dir containing *_chunks.json files")
    m.add_argument("--output", required=True, help="Path to write all_chunks.json")

    # batch mode
    b = sub.add_parser("batch", help="describe multiple videos → per-video chunks.json files")
    b.add_argument("--video-dir", default=None, help="Dir to scan for .MOV/.mp4 files")
    b.add_argument("--videos", nargs="*", default=[], help="Explicit video paths (additive with --video-dir)")
    b.add_argument("--output-dir", required=True, help="Dir to write <stem>_chunks.json files")
    b.add_argument("--scene-threshold", type=float, default=0.5)
    b.add_argument("--max-chunk-seconds", type=int, default=MAX_CHUNK_SECONDS)
    b.add_argument("--transcribe", action="store_true", default=True)
    b.add_argument("--no-transcribe", dest="transcribe", action="store_false")
    b.add_argument("--whisper-model", default="base")
    b.add_argument("--language", default=None, help="Audio language hint for Whisper, e.g. en, ja, ko")
    b.add_argument("--force", nargs="*", metavar="VIDEO_STEM",
                   help="Re-process even if output exists. No args = force all; pass stem(s) to force specific videos")

    # thumbs mode
    t = sub.add_parser("thumbs", help="retroactively generate thumbnails + thumbs.html from chunks.json")
    t.add_argument("--chunks-json", required=True, help="Path to chunks.json or all_chunks.json")
    t.add_argument("--output-dir", default=None, help="Where to write thumbs/ and thumbs.html (default: same dir as chunks-json)")

    args = p.parse_args()

    if args.mode == "describe":
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        duration = get_duration(args.video)
        scenes = detect_scenes(args.video, threshold=args.scene_threshold, duration=duration)
        chunks = split_long_scenes(scenes, max_chunk=args.max_chunk_seconds)
        log.info(f"{len(scenes)} scenes → {len(chunks)} chunks (video: {duration:.1f}s)")

        model, processor = load_qwen()
        thumbs_dir = str(Path(args.output).parent / "thumbs")
        Path(thumbs_dir).mkdir(parents=True, exist_ok=True)
        chunks = describe_all(model, processor, args.video, chunks, thumbs_dir=thumbs_dir)

        speech = []
        if args.transcribe:
            speech = transcribe_audio(args.video, model_size=args.whisper_model, language=args.language)

        # tag each chunk with its source video
        video_abs = str(Path(args.video).resolve())
        for chunk in chunks:
            chunk["source_video"] = video_abs

        shot_at = get_shot_at(args.video)
        out = {
            "video": video_abs,
            "shot_at": shot_at,
            "duration": duration,
            "chunks": chunks,
            "speech": speech,
        }
        with open(args.output, "w") as f:
            json.dump(out, f, indent=2)
        log.info(f"wrote {args.output}")

        html_path = str(Path(args.output).parent / "thumbs.html")
        write_thumbs_html(chunks, speech, html_path, title=Path(args.video).name)
        log.info(f"wrote {html_path}")

        # Print summary for selection
        print("\n--- CHUNK SUMMARY ---")
        for i, ch in enumerate(chunks):
            spoken = speech_in_range(speech, ch["start"], ch["end"])
            speech_tag = f" [speech: {' / '.join(spoken)[:60]}]" if spoken else ""
            print(f"[{i}] {ch['start']:.1f}s-{ch['end']:.1f}s:{speech_tag}")
            if ch.get("action"):
                print(f"     {ch['action'][:100]}")
                tags = []
                if ch.get("setting"): tags.append(ch["setting"])
                if ch.get("shot"): tags.append(ch["shot"])
                if ch.get("energy"): tags.append(ch["energy"] + " energy")
                if ch.get("quality") and ch["quality"].lower() != "good": tags.append(f"[!] {ch['quality']}")
                if tags: print(f"     {' · '.join(tags)}")
            else:
                print(f"     {ch.get('description', '')[:120]}...")
        print("\nRun: python pipeline.py cut --chunks-json <file> --selected 0,1,2 --output highlight.mp4")

    elif args.mode == "cut":
        with open(args.chunks_json) as f:
            data = json.load(f)
        chunks = data["chunks"]
        speech = data.get("speech", [])
        if "sources" in data:
            duration = {s["video"]: s["duration"] for s in data["sources"]}
        else:
            duration = data.get("duration", 0.0)
        selected = [int(x.strip()) for x in args.selected.split(",")]

        max_idx = len(chunks) - 1
        invalid = [i for i in selected if i < 0 or i > max_idx]
        if invalid:
            log.error(f"invalid chunk indices {invalid} — valid range is 0-{max_idx}")
            return 1

        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        use_speech = speech if (args.speech_aware and speech) else None
        if args.speech_aware and not speech:
            log.info("no speech data in chunks.json — cutting without speech snapping")

        video_path = data.get("video")
        concat_chunks(video_path, chunks, selected, args.output,
                      speech=use_speech, video_duration=duration,
                      normalize_audio=args.normalize_audio, scale_height=args.scale)

    elif args.mode == "merge":
        chunk_files = sorted(
            p for p in Path(args.output_dir).glob("*_chunks.json")
            if p.name != "all_chunks.json"
        )
        if not chunk_files:
            log.error(f"no *_chunks.json files found in {args.output_dir}")
            return 1
        log.info(f"merging {len(chunk_files)} chunk file(s)...")
        merged = merge_chunks_json([str(f) for f in chunk_files])
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(merged, f, indent=2)
        log.info(f"wrote {args.output}")

        html_path = str(Path(args.output).parent / "thumbs.html")
        write_thumbs_html(merged["chunks"], merged["speech"], html_path, title="All Chunks")
        log.info(f"wrote {html_path}")

        # Print combined summary grouped by source
        print("\n--- COMBINED CHUNK SUMMARY ---")
        speech_by_src = merged["speech"]
        src_videos = {s["video"]: s for s in merged["sources"]}
        current_src = None
        for ch in merged["chunks"]:
            src = ch.get("source_video", "unknown")
            if src != current_src:
                current_src = src
                meta = src_videos[src]
                shot_str = ""
                if meta.get("shot_at"):
                    try:
                        dt = datetime.fromisoformat(meta["shot_at"].replace("Z", "+00:00"))
                        shot_str = f" — {dt.strftime('%b %-d, %-I:%M %p')}"
                    except ValueError:
                        shot_str = f" — {meta['shot_at']}"
                print(f"\n=== {Path(src).name}{shot_str} ===")
            src_speech = speech_by_src.get(src, [])
            spoken = speech_in_range(src_speech, ch["start"], ch["end"])
            speech_tag = f" [speech: {' / '.join(spoken)[:60]}]" if spoken else ""
            print(f"[{ch['index']}] {ch['start']:.1f}s-{ch['end']:.1f}s:{speech_tag}")
            if ch.get("action"):
                print(f"     {ch['action'][:100]}")
                tags = []
                if ch.get("setting"): tags.append(ch["setting"])
                if ch.get("shot"): tags.append(ch["shot"])
                if ch.get("energy"): tags.append(ch["energy"] + " energy")
                if ch.get("quality") and ch["quality"].lower() != "good": tags.append(f"[!] {ch['quality']}")
                if tags: print(f"     {' · '.join(tags)}")
            else:
                print(f"     {ch.get('description', '')[:100]}...")
        print(f"\nRun: python pipeline.py cut --chunks-json {args.output} --selected 0,1,2 --output highlight.mp4")

    elif args.mode == "batch":
        entries = collect_videos(videos=args.videos, video_dir=args.video_dir)
        if not entries:
            log.error("no videos found — provide --video-dir or --videos")
            return 1
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)

        # Pre-fetch durations for ETA (fast ffprobe, before Qwen loads)
        dur_cache = {e["video"]: get_duration(e["video"]) for e in entries}

        model, processor = load_qwen()
        batch_start = time.time()
        chunks_done = 0

        for i, entry in enumerate(entries):
            video_path = entry["video"]
            stem = Path(video_path).stem
            out_path = Path(args.output_dir) / f"{stem}_chunks.json"

            force_all = args.force is not None and len(args.force) == 0
            force_this = args.force is not None and stem in args.force
            if out_path.exists() and not force_all and not force_this:
                try:
                    with open(out_path) as _f:
                        json.load(_f)
                    log.info(f"[{i+1}/{len(entries)}] skipping {stem} (already done)")
                    continue
                except (json.JSONDecodeError, OSError):
                    log.warning(f"[{i+1}/{len(entries)}] {stem}_chunks.json is corrupt — re-processing")
            elif out_path.exists() and (force_all or force_this):
                log.info(f"[{i+1}/{len(entries)}] re-processing {stem} (--force)")

            shot_str = ""
            if entry.get("shot_at"):
                try:
                    dt = datetime.fromisoformat(entry["shot_at"].replace("Z", "+00:00"))
                    shot_str = f" — {dt.strftime('%b %-d, %-I:%M %p')}"
                except ValueError:
                    shot_str = f" — {entry['shot_at']}"
            log.info(f"[{i+1}/{len(entries)}] {stem}{shot_str}")

            duration = dur_cache[video_path]
            scenes = detect_scenes(video_path, threshold=args.scene_threshold, duration=duration)
            chunks = split_long_scenes(scenes, max_chunk=args.max_chunk_seconds)
            log.info(f"  {len(scenes)} scenes → {len(chunks)} chunks")

            thumbs_dir = str(Path(args.output_dir) / f"{stem}_thumbs")
            Path(thumbs_dir).mkdir(parents=True, exist_ok=True)
            chunks = describe_all(model, processor, video_path, chunks, thumbs_dir=thumbs_dir)

            chunks_done += len(chunks)
            remaining = [
                e for e in entries[i + 1:]
                if not (Path(args.output_dir) / f"{Path(e['video']).stem}_chunks.json").exists()
            ]
            if remaining:
                elapsed = time.time() - batch_start
                avg = elapsed / max(chunks_done, 1)
                est_remaining = sum(
                    max(1, round(dur_cache[e["video"]] / args.max_chunk_seconds))
                    for e in remaining
                )
                eta_s = avg * est_remaining
                eta_str = (f"~{eta_s/3600:.1f}h" if eta_s >= 3600
                           else f"~{int(eta_s/60)}m" if eta_s >= 60
                           else f"~{int(eta_s)}s")
                log.info(f"  {len(remaining)} video(s) left — {eta_str} remaining")

            speech = []
            if args.transcribe:
                speech = transcribe_audio(video_path, model_size=args.whisper_model, language=args.language)

            video_abs = video_path  # already resolved by collect_videos()
            for chunk in chunks:
                chunk["source_video"] = video_abs

            out_data = {
                "video": video_abs,
                "shot_at": entry.get("shot_at"),
                "duration": duration,
                "chunks": chunks,
                "speech": speech,
            }
            with open(out_path, "w") as f:
                json.dump(out_data, f, indent=2)

            html_path = str(Path(args.output_dir) / f"{stem}_thumbs.html")
            write_thumbs_html(chunks, speech, html_path, title=stem)
            log.info(f"  wrote {out_path} ({len(chunks)} chunks) + {Path(html_path).name}")

        log.info("batch complete")
        print(f"\nRun: python pipeline.py merge --output-dir {args.output_dir} --output {args.output_dir}/all_chunks.json")

    elif args.mode == "thumbs":
        with open(args.chunks_json) as f:
            data = json.load(f)
        out_dir = Path(args.output_dir) if args.output_dir else Path(args.chunks_json).parent
        thumbs_dir = out_dir / "thumbs"
        thumbs_dir.mkdir(parents=True, exist_ok=True)

        chunks = data["chunks"]
        log.info(f"extracting thumbnails for {len(chunks)} chunk(s)...")
        for i, ch in enumerate(chunks):
            src = ch.get("source_video") or data.get("video")
            if not src:
                log.warning(f"chunk {i}: no source video, skipping")
                continue
            idx = ch.get("index", i)
            thumb_path = str(thumbs_dir / f"chunk_{idx}.jpg")
            seek = (ch["start"] + ch["end"]) / 2
            try:
                extract_thumb(src, thumb_path, seek)
                ch["thumb"] = thumb_path
            except subprocess.CalledProcessError:
                log.warning(f"chunk {i}: thumb extraction failed")
            if (i + 1) % 10 == 0 or (i + 1) == len(chunks):
                log.info(f"  {i+1}/{len(chunks)} done")

        with open(args.chunks_json, "w") as f:
            json.dump(data, f, indent=2)
        log.info(f"updated {args.chunks_json}")

        speech = data.get("speech", [])
        is_merged = "sources" in data
        title = "All Chunks" if is_merged else Path(data.get("video", "")).name
        html_path = str(out_dir / "thumbs.html")
        write_thumbs_html(chunks, speech, html_path, title=title)
        log.info(f"wrote {html_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
