from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_DESCRIBE_PROMPT = (
    "Describe this photo. Reply with ONLY this JSON — no markdown, no extra text:\n"
    '{"caption": "one sentence describing the main subject and what is happening",\n'
    ' "quality": "one word: good, blurry, dark, overexposed, or obstructed",\n'
    ' "scene": "brief location context, e.g. mountain trail, indoor kitchen, city street",\n'
    ' "people": "one word: none, one, few, or many"}'
)

_RETRY_TEMPLATE = (
    "That was not valid JSON. Required format:\n"
    '{{"caption": "...", "quality": "good|blurry|dark|overexposed|obstructed", '
    '"scene": "...", "people": "none|one|few|many"}}\n'
    "Your response was: {prev}\n"
    "Output ONLY the JSON object."
)

CLAUDE_PROMPT = (
    "Read this image file and return ONLY a JSON object with no markdown, no extra text:\n"
    '{{"caption": "one sentence describing the main subject and what is happening",\n'
    ' "quality": "one word: good, blurry, dark, overexposed, or obstructed",\n'
    ' "scene": "brief location context, e.g. mountain trail, indoor kitchen, city street",\n'
    ' "people": "one word: none, one, few, or many"}}\n\n'
    "Image: {path}"
)

_VIDEO_CLAUDE_PROMPT = """\
These are evenly-spaced keyframes from a video (one every ~{interval}s).
Describe what happens throughout the video. Return ONLY this JSON — no markdown, no extra text:
{{"caption": "one sentence describing the overall video",
 "quality": "one word: good, blurry, dark, overexposed, or obstructed",
 "scene": "brief location context, e.g. mountain trail, indoor kitchen, city street",
 "people": "one word: none, one, few, or many",
 "scenes": [
   {{"start_sec": 0.0, "end_sec": {interval}.0, "caption": "what happens in this segment", "score": 2.0}}
 ]}}

Keyframes (read each file path to see the frame):
{frames}"""

_NULL = {"caption": None, "quality": None, "scene": None, "people": None}


class ClaudeDescriber:
    def __init__(self, model: str = "haiku", workers: int = 5):
        bin_path = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
        if not Path(bin_path).exists():
            raise RuntimeError(
                f"claude CLI not found. Expected at {bin_path}. "
                "Install Claude Code: https://claude.ai/code"
            )
        self.claude_bin = bin_path
        self.model = model
        self.workers = workers

    async def describe_one(self, photo_path: str) -> dict:
        prompt = CLAUDE_PROMPT.format(path=photo_path)
        try:
            proc = await asyncio.create_subprocess_exec(
                self.claude_bin, "-p", "--model", self.model,
                "--dangerously-skip-permissions", prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            if proc.returncode != 0:
                log.warning(f"claude exited {proc.returncode} for {photo_path}: {stderr.decode('utf-8', errors='replace')[:200]}")
            raw = stdout.decode("utf-8", errors="replace")
            result = _parse_describe_json(raw)
            return result if result is not None else _NULL.copy()
        except asyncio.TimeoutError:
            log.warning(f"claude timeout for {photo_path}")
            return _NULL.copy()
        except Exception as e:
            log.warning(f"claude error for {photo_path}: {e}")
            return _NULL.copy()

    async def describe_batch(self, photos: list[dict]) -> list[dict]:
        sem = asyncio.Semaphore(self.workers)
        results = [None] * len(photos)

        async def _one(i, photo):
            async with sem:
                results[i] = await self.describe_one(photo["path"])

        await asyncio.gather(*[_one(i, p) for i, p in enumerate(photos)])
        return results

    async def describe_video_one(self, video_path: str, interval: int = 30) -> dict:
        """Extract keyframes with ffmpeg and describe the video via claude -p."""
        import subprocess
        _null = {"caption": None, "quality": "good", "scene": None, "people": "none", "scenes": []}
        tmp_dir = tempfile.mkdtemp(prefix="video_claude_")
        try:
            # Get duration first so we can pick an appropriate interval
            dur_result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "csv=p=0", video_path],
                capture_output=True, text=True,
            )
            try:
                duration = float(dur_result.stdout.strip())
            except (ValueError, AttributeError):
                duration = 0.0
            # For short clips, grab one frame per second (up to 8); for very short clips, seek to midpoint
            frame_pattern = os.path.join(tmp_dir, "frame_%03d.jpg")
            if duration < 1.0:
                effective_interval = 1
                proc_result = subprocess.run(
                    ["ffmpeg", "-ss", str(duration / 2), "-i", video_path,
                     "-frames:v", "1", "-q:v", "3", frame_pattern,
                     "-loglevel", "error", "-y"],
                    capture_output=True,
                )
            else:
                effective_interval = interval if duration >= interval else max(1, int(duration / 8) or 1)
                proc_result = subprocess.run(
                    ["ffmpeg", "-i", video_path,
                     "-vf", f"fps=1/{effective_interval}", "-frames:v", "8",
                     "-q:v", "3", frame_pattern,
                     "-loglevel", "error", "-y"],
                    capture_output=True,
                )
            if proc_result.returncode != 0:
                log.warning(f"ffmpeg failed for {video_path}: {proc_result.stderr.decode()[:200]}")
                return _null

            frames = sorted(Path(tmp_dir).glob("frame_*.jpg"))
            if not frames:
                return _null

            frame_lines = "\n".join(
                f"Frame {i + 1} ({i * effective_interval}s): {f}" for i, f in enumerate(frames)
            )
            interval = effective_interval  # use for prompt and parse
            prompt = _VIDEO_CLAUDE_PROMPT.format(interval=interval, frames=frame_lines)

            proc = await asyncio.create_subprocess_exec(
                self.claude_bin, "-p", "--model", self.model,
                "--dangerously-skip-permissions", prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90.0)
            if proc.returncode != 0:
                log.warning(f"claude video exited {proc.returncode} for {video_path}: {stderr.decode()[:200]}")
            raw = stdout.decode("utf-8", errors="replace")
            result = _parse_video_describe_json(raw, interval=interval, n_frames=len(frames))
            return result if result is not None else _null
        except asyncio.TimeoutError:
            log.warning(f"claude video timeout for {video_path}")
            return _null
        except Exception as e:
            log.warning(f"claude video error for {video_path}: {e}")
            return _null
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    async def describe_video_batch(self, videos: list[dict]) -> list[dict]:
        sem = asyncio.Semaphore(self.workers)
        results = [None] * len(videos)

        async def _one(i, video):
            async with sem:
                results[i] = await self.describe_video_one(video["path"])

        await asyncio.gather(*[_one(i, v) for i, v in enumerate(videos)])
        return results


def _parse_describe_json(raw: str) -> dict | None:
    text = re.sub(r"```(?:json)?\s*", "", raw).strip()

    candidates = [text]
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first:last + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        if not {"caption", "quality", "scene", "people"}.issubset(parsed.keys()):
            continue
        return {
            "caption": str(parsed["caption"]).strip() or None,
            "quality": str(parsed["quality"]).strip() or None,
            "scene":   str(parsed["scene"]).strip() or None,
            "people":  str(parsed["people"]).strip() or None,
        }
    return None


def _parse_video_describe_json(raw: str, interval: int = 30, n_frames: int = 1) -> dict | None:
    text = re.sub(r"```(?:json)?\s*", "", raw).strip()
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last <= first:
        return None
    try:
        parsed = json.loads(text[first:last + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None

    scenes = parsed.get("scenes", [])
    if not isinstance(scenes, list):
        scenes = []

    normalized = []
    for i, s in enumerate(scenes):
        if not isinstance(s, dict):
            continue
        normalized.append({
            "start_sec": float(s.get("start_sec", i * interval)),
            "end_sec": float(s.get("end_sec", (i + 1) * interval)),
            "caption": str(s.get("caption", "")).strip() or None,
            "score": float(s.get("score", 2.0)),
        })

    if not normalized:
        cap = str(parsed.get("caption", "")).strip() or None
        normalized = [{"start_sec": i * interval, "end_sec": (i + 1) * interval, "caption": cap, "score": 2.0} for i in range(n_frames)]

    return {
        "caption": str(parsed.get("caption", "")).strip() or None,
        "quality": str(parsed.get("quality", "good")).strip() or "good",
        "scene": str(parsed.get("scene", "")).strip() or None,
        "people": str(parsed.get("people", "none")).strip() or "none",
        "scenes": normalized,
    }


try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

QWEN_MODEL = "Qwen/Qwen2.5-VL-7B-Instruct"


def load_qwen():
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    import torch
    from transformers import AutoProcessor, BitsAndBytesConfig, Qwen2_5_VLForConditionalGeneration
    log.info(f"loading {QWEN_MODEL} (4-bit)...")
    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
    )
    processor = AutoProcessor.from_pretrained(QWEN_MODEL)
    model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
        QWEN_MODEL, quantization_config=bnb, device_map="cuda:0",
        max_memory={0: "6144MiB"},
    )
    model.eval()
    log.info("qwen loaded")
    return model, processor


def _call_qwen(model, processor, messages: list[dict]) -> str:
    import torch
    from qwen_vl_utils import process_vision_info
    text_input = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    image_inputs, video_inputs, video_kwargs = process_vision_info(messages, return_video_kwargs=True)
    if isinstance(video_kwargs.get("fps"), list) and video_kwargs["fps"]:
        video_kwargs["fps"] = video_kwargs["fps"][0]
    elif "fps" in video_kwargs and not video_kwargs["fps"]:
        del video_kwargs["fps"]
    inputs = processor(
        text=[text_input], images=image_inputs, videos=video_inputs,
        return_tensors="pt", **video_kwargs,
    ).to(model.device)
    with torch.no_grad():
        outputs = model.generate(**inputs, max_new_tokens=200, do_sample=False)
    decoded = processor.decode(outputs[0], skip_special_tokens=True)
    del inputs, outputs
    torch.cuda.empty_cache()
    for marker in ("assistant\n", "Assistant: ", "assistant: "):
        if marker in decoded:
            decoded = decoded.split(marker, 1)[1].strip()
            break
    else:
        decoded = decoded.strip()
    return decoded


def describe_photo(model, processor, path: Path) -> dict:
    _NULL = {"caption": None, "quality": None, "scene": None, "people": None}
    if not path.exists():
        return _NULL
    tmp_name = None
    try:
        from PIL import Image
        if path.suffix.lower() == ".heic":
            img = Image.open(path)
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp.close()
            img.save(tmp.name, "JPEG")
            tmp_name = tmp.name
            img = Image.open(tmp_name)
        else:
            img = Image.open(path)
        img = img.convert("RGB")
        # Limit resolution to avoid VRAM OOM — same budget as the video pipeline
        img.thumbnail((630, 630), Image.LANCZOS)
        messages = [{
            "role": "user",
            "content": [
                {"type": "image", "image": img, "max_pixels": 360 * 420},
                {"type": "text", "text": _DESCRIBE_PROMPT},
            ],
        }]
        for _ in range(3):
            raw = _call_qwen(model, processor, messages)
            result = _parse_describe_json(raw)
            if result is not None:
                return result
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": _RETRY_TEMPLATE.format(prev=raw[:300]),
            })
        log.warning(f"describe_photo: 3 parse failures for {path.name}")
        return _NULL
    except Exception as e:
        import torch
        if isinstance(e, torch.cuda.OutOfMemoryError):
            raise
        log.warning(f"describe_photo failed for {path.name}: {e}")
        return _NULL
    finally:
        if tmp_name:
            os.unlink(tmp_name)


def describe_video(model, processor, path: Path) -> dict:
    from pipeline import (
        detect_scenes, split_long_scenes, describe_chunk,
        cut_segment, compute_chunk_score,
    )

    try:
        scenes = detect_scenes(str(path))
    except Exception as e:
        log.warning(f"scene detection failed for {path.name}: {e}")
        return {"caption": None, "quality": "good", "scene": None, "people": "none", "scenes": []}

    chunks = split_long_scenes(scenes)

    if not chunks:
        return {"caption": "(no scenes detected)", "quality": "good", "scene": None, "people": "none", "scenes": []}

    seg_dir = tempfile.mkdtemp(prefix="video_describe_")
    described_scenes = []
    try:
        for i, chunk in enumerate(chunks):
            seg_path = os.path.join(seg_dir, f"seg_{i}.mp4")
            try:
                cut_segment(str(path), chunk["start"], chunk["end"], seg_path)
                result = describe_chunk(model, processor, seg_path, chunk["start"], chunk["end"])
                score = compute_chunk_score(result)  # loudness/profile not available here; energy+quality only
                described_scenes.append({
                    "start_sec": chunk["start"],
                    "end_sec": chunk["end"],
                    "caption": result.get("action"),
                    "score": score,
                })
            except Exception as e:
                log.warning(f"scene {i} of {path.name} failed: {e}")
    finally:
        shutil.rmtree(seg_dir, ignore_errors=True)

    summary = next((s["caption"] for s in described_scenes if s.get("caption")), None)

    return {
        "caption": summary,
        "quality": "good",
        "scene": None,
        "people": "none",
        "scenes": described_scenes,
    }
