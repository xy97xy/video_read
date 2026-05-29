from __future__ import annotations
import json
import logging
import re

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


import os
import tempfile
from pathlib import Path

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

QWEN_MODEL = "Qwen/Qwen2.5-VL-7B-Instruct"


def load_qwen():
    import torch
    from transformers import AutoProcessor, BitsAndBytesConfig, Qwen2_5_VLForConditionalGeneration
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
    return model, processor


def _call_qwen(model, processor, messages: list[dict]) -> str:
    import torch
    from qwen_vl_utils import process_vision_info
    text_input = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    image_inputs, video_inputs, video_kwargs = process_vision_info(messages, return_video_kwargs=True)
    if isinstance(video_kwargs.get("fps"), list):
        video_kwargs["fps"] = video_kwargs["fps"][0]
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
    return decoded.strip()


def describe_photo(model, processor, path: Path) -> dict:
    _NULL = {"caption": None, "quality": None, "scene": None, "people": None}
    if not path.exists():
        return _NULL
    tmp_name = None
    try:
        from PIL import Image
        img_path = str(path)
        if path.suffix.lower() == ".heic":
            img = Image.open(path)
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            img.save(tmp.name, "JPEG")
            tmp_name = tmp.name
            img_path = tmp_name
        messages = [{
            "role": "user",
            "content": [
                {"type": "image", "image": img_path},
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
        log.warning(f"describe_photo failed for {path.name}: {e}")
        return _NULL
    finally:
        if tmp_name:
            os.unlink(tmp_name)
