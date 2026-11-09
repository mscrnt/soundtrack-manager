"""Optional AI-assisted discovery through a local OpenAI-compatible LLM.

The model only translates a request into search parameters: game titles to
search for, plus platform, type, year and sort filters. Every value is checked
against the site's vocabularies. Results always come from the site's own search,
so the model cannot invent albums. Nothing else in the app depends on this module.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

import httpx

from .discovery import QueryError, parse_year_range
from .khinsider.catalog import ALBUM_TYPES, PLATFORMS, SEARCH_SORTS, find_album_type, find_platform

log = logging.getLogger(__name__)

MAX_TITLES = 8

SYSTEM_PROMPT = f"""You convert a request for video game music into search parameters for downloads.khinsider.com.
Reply with ONLY one JSON object, no prose:
{{"titles": [string], "platforms": [string], "album_type": string|null, "year": string|null, "sort": string}}

Rules:
- "titles": up to {MAX_TITLES} game or franchise names to search for. The site has NO genre or mood filter, so for
  requests like "survival horror" list well-known matching games (e.g. "Resident Evil", "Silent Hill").
  Use [] if the user only gave filters such as a platform and year.
- "platforms": zero or more, chosen ONLY from: {", ".join(p.name for p in PLATFORMS)}.
- "album_type": null or one of: {", ".join(t.row_label for t in ALBUM_TYPES)}.
- "year": null, a single year "1998", or a range "1996-2000".
- "sort": one of {", ".join(SEARCH_SORTS)} (default "relevance").
Do not invent album names. Search terms only."""


class AiError(Exception):
    pass


@dataclass
class AiPlan:
    titles: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    album_type: str | None = None
    year: str | None = None
    sort: str = "relevance"
    warnings: list[str] = field(default_factory=list)

    def query_params(self) -> list[tuple[str, str]]:
        params = [("q", " | ".join(self.titles))] if self.titles else []
        params += [("platform", p) for p in self.platforms]
        if self.album_type:
            params.append(("type", str(find_album_type(self.album_type).id)))  # id, as the form's select uses
        if self.year:
            params.append(("year", self.year))
        params.append(("sort", self.sort))
        return params

    def describe(self) -> str:
        parts = []
        if self.titles:
            parts.append("titles: " + ", ".join(self.titles))
        if self.platforms:
            parts.append("platforms: " + ", ".join(self.platforms))
        if self.album_type:
            parts.append(f"type: {self.album_type}")
        if self.year:
            parts.append(f"year: {self.year}")
        parts.append(f"sort: {self.sort}")
        return "; ".join(parts)


def _extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
    raise AiError("the model did not return valid JSON")


def validate_plan(data: dict) -> AiPlan:
    plan = AiPlan()
    titles = data.get("titles") or []
    if isinstance(titles, str):
        titles = [titles]
    for t in titles:
        if isinstance(t, str) and t.strip() and "|" not in t:
            t = t.strip()[:100]
            if t.lower() not in (x.lower() for x in plan.titles):
                plan.titles.append(t)
    if len(plan.titles) > MAX_TITLES:
        plan.warnings.append(f"kept the first {MAX_TITLES} titles")
        plan.titles = plan.titles[:MAX_TITLES]
    plats = data.get("platforms") or []
    if isinstance(plats, str):
        plats = [plats]
    for p in plats:
        found = find_platform(str(p)) if p else None
        if found is None:
            plan.warnings.append(f"ignored unknown platform {p!r}")
        elif found.name not in plan.platforms:
            plan.platforms.append(found.name)
    if data.get("album_type"):
        t = find_album_type(str(data["album_type"]))
        if t is None:
            plan.warnings.append(f"ignored unknown album type {data['album_type']!r}")
        else:
            plan.album_type = t.row_label.lower()
    if data.get("year"):
        try:
            lo, hi = parse_year_range(str(data["year"]))
            plan.year = str(lo) if lo == hi else f"{lo or ''}-{hi or ''}"
        except QueryError:
            plan.warnings.append(f"ignored invalid year {data['year']!r}")
    sort = str(data.get("sort") or "relevance").lower()
    plan.sort = sort if sort in SEARCH_SORTS else "relevance"
    if not plan.titles and not plan.platforms:
        raise AiError("the model gave no titles and no platform; try naming a game, series or platform")
    return plan


async def plan_from_request(request: str, *, endpoint: str, model: str | None, api_key: str | None = None,
                            timeout: float = 60.0, transport: httpx.AsyncBaseTransport | None = None) -> AiPlan:
    if not endpoint:
        raise AiError("no LLM endpoint configured (Settings → AI, or LLM_ENDPOINT)")
    request = request.strip()[:500]
    if not request:
        raise AiError("empty request")
    url = endpoint.rstrip("/") + "/chat/completions"
    body = {"model": model or "local-model", "temperature": 0,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": request}],
            "response_format": {"type": "json_object"}}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        try:
            r = await client.post(url, json=body, headers=headers)
            if r.status_code == 400:  # some servers reject response_format; retry without it
                body.pop("response_format")
                r = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise AiError(f"cannot reach the LLM at {endpoint}: {type(exc).__name__}") from exc
    if r.status_code >= 400:
        raise AiError(f"LLM server answered HTTP {r.status_code}")
    try:
        content = r.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AiError("unexpected reply from the LLM server (not OpenAI-compatible?)") from exc
    plan = validate_plan(_extract_json(content or ""))
    log.info("AI plan for %r: %s", request, plan.describe())
    return plan
