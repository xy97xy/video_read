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


try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

QWEN_MODEL = "Qwen/Qwen2.5-VL-7B-Instruct"


def load_qwen():
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
        QWEN_MODEL, quantization_config=bnb, device_map="cuda:0"
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
