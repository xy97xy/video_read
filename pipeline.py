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
import subprocess
import sys
import tempfile
from pathlib import Path

import torch
from transformers import AutoProcessor, BitsAndBytesConfig, Qwen2_5_VLForConditionalGeneration
from transnetv2_pytorch import TransNetV2
from qwen_vl_utils import process_vision_info

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pipeline")

QWEN_MODEL = "Qwen/Qwen2.5-VL-7B-Instruct"
MAX_CHUNK_SECONDS = 30
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


def describe_chunk(model, processor, seg_path: str, start: float, end: float) -> str:
    messages = [{
        "role": "user",
        "content": [
            {"type": "video", "video": seg_path, "fps": 1.0, "max_pixels": 360 * 420},
            {"type": "text", "text": (
                f"Describe what happens in this video clip in detail. "
                f"Cover: subjects, actions, camera movement, notable details, background. "
                f"Be specific — mention objects, colors, and movements you observe. "
                f"This clip covers {start:.1f}s-{end:.1f}s of the original video."
            )},
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
        outputs = model.generate(**inputs, max_new_tokens=512, do_sample=False)
    decoded = processor.decode(outputs[0], skip_special_tokens=True)
    for marker in ("assistant\n", "Assistant: ", "assistant: "):
        if marker in decoded:
            return decoded.split(marker, 1)[1].strip()
    return decoded.strip()


def cut_segment(video_path: str, start: float, end: float, out_path: str) -> None:
    cmd = ["ffmpeg", "-y", "-i", video_path,
           "-ss", str(start), "-t", str(end - start),
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", out_path]
    subprocess.run(cmd, check=True, capture_output=True)


def describe_all(model, processor, video_path: str, chunks: list[dict]) -> list[dict]:
    seg_dir = tempfile.mkdtemp(prefix="pipeline_seg_")
    for i, chunk in enumerate(chunks):
        seg_path = os.path.join(seg_dir, f"seg_{i}.mp4")
        cut_segment(video_path, chunk["start"], chunk["end"], seg_path)
        log.info(f"[{i+1}/{len(chunks)}] describing {chunk['start']:.1f}s-{chunk['end']:.1f}s ...")
        chunk["description"] = describe_chunk(model, processor, seg_path, chunk["start"], chunk["end"])
        log.info(f"  {chunk['description'][:100]}")
        os.remove(seg_path)
    try:
        os.rmdir(seg_dir)
    except OSError:
        pass
    return chunks


# ---------------------------------------------------------------------------
# Stage 2b: speech transcription (optional, faster-whisper)
# ---------------------------------------------------------------------------

def transcribe_audio(video_path: str, model_size: str = "base") -> list[dict]:
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

    segments_iter, info = whisper_model.transcribe(video_path, word_timestamps=True)
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


def speech_in_range(speech: list[dict], start: float, end: float) -> list[str]:
    """Return text of speech segments that overlap with [start, end]."""
    texts = []
    for seg in speech:
        if seg["end"] > start and seg["start"] < end:
            texts.append(seg["text"])
    return texts


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
    video_path: str,
    chunks: list[dict],
    selected: list[int],
    output_path: str,
    speech: list[dict] | None = None,
    video_duration: float = 0.0,
) -> None:
    raw_scenes = [(chunks[i]["start"], chunks[i]["end"]) for i in selected]
    if not raw_scenes:
        log.warning("no chunks selected — nothing to cut")
        return

    if speech:
        scenes = [(snap_to_silence(s, speech, video_duration), snap_to_silence(e, speech, video_duration))
                  for s, e in raw_scenes]
    else:
        scenes = raw_scenes

    parts, inputs = [], []
    for i, (s, e) in enumerate(scenes):
        parts.append(f"[0:v]trim=start={s}:end={e},setpts=PTS-STARTPTS[v{i}];")
        parts.append(f"[0:a]atrim=start={s}:end={e},asetpts=PTS-STARTPTS[a{i}];")
        inputs.append(f"[v{i}][a{i}]")
    filter_complex = "".join(parts) + f"{''.join(inputs)}concat=n={len(scenes)}:v=1:a=1[outv][outa]"
    cmd = ["ffmpeg", "-y", "-i", video_path,
           "-filter_complex", filter_complex,
           "-map", "[outv]", "-map", "[outa]",
           "-c:v", "libx264", "-c:a", "aac", output_path]
    subprocess.run(cmd, check=True, capture_output=True)
    kept = sum(e - s for s, e in scenes)
    log.info(f"wrote {output_path} ({kept:.1f}s from {len(scenes)} chunks)")


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

    # cut mode
    c = sub.add_parser("cut", help="cut + concat selected chunks → highlight video")
    c.add_argument("--chunks-json", required=True)
    c.add_argument("--selected", required=True, help="Comma-separated chunk indices, e.g. 0,2,4")
    c.add_argument("--output", required=True, help="Output video path")
    c.add_argument("--speech-aware", action="store_true", default=True,
                   help="Snap cut points to sentence boundaries (default: on)")
    c.add_argument("--no-speech-aware", dest="speech_aware", action="store_false")

    args = p.parse_args()

    if args.mode == "describe":
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        duration = get_duration(args.video)
        scenes = detect_scenes(args.video, threshold=args.scene_threshold, duration=duration)
        chunks = split_long_scenes(scenes, max_chunk=args.max_chunk_seconds)
        log.info(f"{len(scenes)} scenes → {len(chunks)} chunks (video: {duration:.1f}s)")

        model, processor = load_qwen()
        chunks = describe_all(model, processor, args.video, chunks)

        speech = []
        if args.transcribe:
            speech = transcribe_audio(args.video, model_size=args.whisper_model)

        out = {"video": args.video, "duration": duration, "chunks": chunks, "speech": speech}
        with open(args.output, "w") as f:
            json.dump(out, f, indent=2)
        log.info(f"wrote {args.output}")

        # Print summary for selection
        print("\n--- CHUNK SUMMARY ---")
        for i, ch in enumerate(chunks):
            spoken = speech_in_range(speech, ch["start"], ch["end"])
            speech_tag = f" [speech: {' / '.join(spoken)[:60]}]" if spoken else ""
            print(f"[{i}] {ch['start']:.1f}s-{ch['end']:.1f}s:{speech_tag}")
            print(f"     {ch['description'][:120]}...")
        print("\nRun: python pipeline.py cut --chunks-json <file> --selected 0,1,2 --output highlight.mp4")

    elif args.mode == "cut":
        with open(args.chunks_json) as f:
            data = json.load(f)
        chunks = data["chunks"]
        speech = data.get("speech", [])
        duration = data.get("duration", 0.0)
        selected = [int(x.strip()) for x in args.selected.split(",")]
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        use_speech = speech if (args.speech_aware and speech) else None
        if args.speech_aware and not speech:
            log.info("no speech data in chunks.json — cutting without speech snapping")
        concat_chunks(data["video"], chunks, selected, args.output,
                      speech=use_speech, video_duration=duration)

    return 0


if __name__ == "__main__":
    sys.exit(main())
