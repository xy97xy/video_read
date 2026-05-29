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
