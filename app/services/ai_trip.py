# app/services/ai_trip.py
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

import httpx

from app.core.settings import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a local trip planner. When given a vibe description, return ONLY a JSON object (no markdown, no explanation) in this exact shape:

{
  "title": "short evocative trip title",
  "stops": [
    {
      "name": "Place Name",
      "lat": 0.0,
      "lng": 0.0,
      "reason": "one sentence why this stop fits the vibe"
    }
  ]
}

Rules:
- Include as many stops as the trip naturally calls for - there is NO upper limit. A weekend city trip might have 4–6 stops, but a multi-day road trip or scenic route can easily have 15–40+ stops. Let the distance, duration, and vibe dictate the count. MORE stops is almost always better - travellers want a rich, detailed itinerary, not a sparse skeleton.
- Order stops as a sensible driving/riding route from start to end
- Use real place names with accurate coordinates
- Avoid generic tourist traps unless the vibe explicitly asks for them
- Reason must be specific to the vibe, not generic
- Coordinates must be accurate to 4 decimal places
- No markdown, no extra keys, no explanation outside the JSON"""


class AiTripService:
    def __init__(self) -> None:
        self._api_key = settings.deepseek_api_key
        self._model = settings.deepseek_model
        self._base = settings.deepseek_base_url.rstrip("/")
        self._timeout = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)

    async def generate(self, vibe: str) -> Dict[str, Any]:
        if not self._api_key:
            raise RuntimeError("DEEPSEEK_API_KEY not configured")

        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": vibe},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.9,
            "max_tokens": 4096,
        }
        url = f"{self._base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        logger.info("AI trip generate: vibe=%d chars", len(vibe))

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(url, headers=headers, json=body)
            if r.status_code >= 400:
                raise RuntimeError(f"DeepSeek {r.status_code}: {r.text[:500]}")
            data = r.json()

        usage = data.get("usage", {})
        if usage:
            logger.info(
                "AI trip LLM usage: prompt=%s completion=%s total=%s",
                usage.get("prompt_tokens", "?"),
                usage.get("completion_tokens", "?"),
                usage.get("total_tokens", "?"),
            )

        try:
            raw: str = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"AI trip: unexpected response shape: {e}")

        if not raw:
            raise RuntimeError("AI trip: empty LLM response")

        parsed = _parse_json(raw)
        _validate(parsed)
        return parsed


def _parse_json(text: str) -> Dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"AI returned invalid JSON: {e}. Raw: {text[:300]}")


def _validate(d: Dict[str, Any]) -> None:
    if not isinstance(d.get("title"), str) or not d["title"]:
        raise RuntimeError("AI response missing 'title'")

    stops: List[Any] = d.get("stops", [])
    if not isinstance(stops, list) or len(stops) < 2:
        raise RuntimeError("AI response needs at least 2 stops")

    for i, s in enumerate(stops):
        if not isinstance(s, dict):
            raise RuntimeError(f"Stop {i} is not an object")
        if not isinstance(s.get("name"), str) or not s["name"]:
            raise RuntimeError(f"Stop {i} missing 'name'")
        if not isinstance(s.get("lat"), (int, float)):
            raise RuntimeError(f"Stop {i} missing numeric 'lat'")
        if not isinstance(s.get("lng"), (int, float)):
            raise RuntimeError(f"Stop {i} missing numeric 'lng'")
        if "reason" not in s:
            s["reason"] = ""
