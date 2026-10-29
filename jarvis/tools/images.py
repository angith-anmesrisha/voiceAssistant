"""Making pictures from a description ("generate an image of a red panda astronaut").

The owner asked for free OpenRouter models (2026-10-29). OpenRouter has no free image
model yet (none of its ``:free`` models outputs images, and its own guide says so), so:

1. any free image model OpenRouter lists at the time (``/models?output_modalities=image``,
   an id ending ``:free`` or every price 0) is tried first, so one is picked up the day
   it appears;
2. Cloudflare Workers AI's FLUX.1 schnell, when ``CLOUDFLARE_ACCOUNT_ID`` and
   ``CLOUDFLARE_API_TOKEN`` are saved (a free account's 10,000 daily "neurons" are a few
   hundred pictures);
3. Pollinations' keyless classic API (FLUX, about 768x768 at most, one image at a time
   per address, a small logo);
4. paid OpenRouter image models, only when ``images.allow_paid`` is on (about one cent
   an image, and OpenRouter wants more than $1 of credit before it makes any).

Real 3D models (.glb) are made from a picture by :mod:`jarvis.tools.threed`.

The picture is only shown (``show``): nothing is written to disk until the user presses
Save in the preview window. The description goes to the image service, but is never
logged.
"""

from __future__ import annotations

import asyncio
import base64
import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger

from jarvis.tools.registry import Risk, ToolRegistry, ToolResult, ToolSpec

OPENROUTER = "https://openrouter.ai/api/v1"
CLOUDFLARE = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"
CLOUDFLARE_ACCOUNT = "CLOUDFLARE_ACCOUNT_ID"
CLOUDFLARE_TOKEN = "CLOUDFLARE_API_TOKEN"
POLLINATIONS = "https://image.pollinations.ai/prompt/"
MAX_BYTES = 25_000_000
MAX_PROMPT = 1_000

# Pollinations keeps the image area at or under 768x768.
_POLLINATIONS_SIZE = {
    "1:1": (768, 768),
    "16:9": (1024, 576),
    "9:16": (576, 1024),
    "4:3": (880, 660),
    "3:4": (660, 880),
    "3:2": (936, 624),
    "2:3": (624, 936),
}
ASPECTS = tuple(_POLLINATIONS_SIZE)

THREE_D_STYLE = (
    "3D render, physically based materials, soft studio lighting, subtle depth of field, "
    "high detail"
)


class ImageError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Picture:
    data: bytes
    mime: str
    prompt: str
    model: str  # e.g. "flux" or "google/gemini-2.5-flash-image"
    source: str  # "OpenRouter", "Cloudflare" or "Pollinations"
    free: bool

    @property
    def extension(self) -> str:
        return {"image/jpeg": ".jpg", "image/webp": ".webp", "image/svg+xml": ".svg"}.get(
            self.mime, ".png"
        )

    @property
    def credit(self) -> str:
        """Who made it, for replies and the preview window's title."""
        name = self.model.split("/")[-1].removesuffix(":free")
        return f"{name} ({self.source}, {'free' if self.free else 'paid'})"


def image_type(data: bytes) -> str | None:
    """The picture's MIME type from its first bytes, or None if it isn't one we show."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def is_free_listing(model: dict[str, Any]) -> bool:
    """A model from OpenRouter's list that costs nothing (``:free``, or every price 0)."""
    model_id = str(model.get("id", ""))
    if model_id.endswith(":free"):
        return True
    pricing = model.get("pricing")
    if not isinstance(pricing, dict) or not pricing:
        return False
    try:
        return all(float(value) == 0 for value in pricing.values() if value not in (None, ""))
    except (TypeError, ValueError):
        return False


def styled(prompt: str, style: str = "", three_d: bool = False) -> str:
    text = prompt.strip()
    extras = [s for s in (style.strip(), THREE_D_STYLE if three_d else "") if s]
    if extras:
        text = f"{text}. Style: {'; '.join(extras)}"
    return text[:MAX_PROMPT]


class ImageMaker:
    def __init__(
        self,
        client: httpx.AsyncClient,
        get_key: Callable[[str], str | None],
        *,
        openrouter_env: str = "OPENROUTER_API_KEY",
        cloudflare_model: str = "@cf/black-forest-labs/flux-1-schnell",
        cloudflare_steps: int = 6,
        pollinations: bool = True,
        allow_paid: bool = False,
        paid_models: list[str] | None = None,
        timeout_s: float = 120.0,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self._client = client
        self._get_key = get_key
        self._openrouter_env = openrouter_env
        self._cloudflare_model = cloudflare_model
        self._cloudflare_steps = max(1, min(cloudflare_steps, 8))
        self._pollinations = pollinations
        self._allow_paid = allow_paid
        self._paid = list(paid_models or [])
        self._timeout = httpx.Timeout(timeout_s, connect=15.0)
        self._notify = notify or (lambda _text: None)
        self._free: list[str] | None = None  # listed once per session
        self._gone: set[str] = set()  # 404s: not asked again this session
        self._one_at_a_time = asyncio.Lock()  # Pollinations allows one image at a time

    async def free_models(self) -> list[str]:
        """OpenRouter's free image models right now (an empty list today)."""
        if self._free is not None:
            return self._free
        try:
            response = await self._client.get(
                f"{OPENROUTER}/models",
                params={"output_modalities": "image"},
                timeout=httpx.Timeout(20.0, connect=10.0),
            )
            response.raise_for_status()
            listed = response.json().get("data", [])
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            logger.info("Images: couldn't list OpenRouter's image models ({})", type(exc).__name__)
            return []  # not remembered: tried again next time
        self._free = [
            str(m["id"])
            for m in listed
            if isinstance(m, dict) and m.get("id") and is_free_listing(m)
        ]
        logger.info("Images: OpenRouter lists {} free image model(s)", len(self._free))
        return self._free

    async def make(self, prompt: str, aspect: str = "1:1") -> Picture:
        aspect = aspect if aspect in _POLLINATIONS_SIZE else "1:1"
        problems: list[str] = []
        key = self._get_key(self._openrouter_env) if self._openrouter_env else None
        if key:
            for model in await self.free_models():
                if model in self._gone:
                    continue
                try:
                    return await self._openrouter(key, model, prompt, aspect, free=True)
                except ImageError as exc:
                    problems.append(f"{model.split('/')[-1]}: {exc}")
        account, token = self._get_key(CLOUDFLARE_ACCOUNT), self._get_key(CLOUDFLARE_TOKEN)
        if account and token and self._cloudflare_model:
            try:
                return await self._cloudflare(account.strip(), token.strip(), prompt)
            except ImageError as exc:
                problems.append(f"Cloudflare: {exc}")
        if self._pollinations:
            try:
                return await self._pollinations_image(prompt, aspect)
            except ImageError as exc:
                problems.append(f"Pollinations: {exc}")
        if self._allow_paid and key:
            for model in self._paid:
                if model in self._gone:
                    continue
                try:
                    return await self._openrouter(key, model, prompt, aspect, free=False)
                except ImageError as exc:
                    problems.append(f"{model.split('/')[-1]}: {exc}")
        if not problems:
            if not self._pollinations and not (self._allow_paid and key):
                raise ImageError(
                    "OpenRouter has no free image model right now, Cloudflare isn't set up, "
                    "and Pollinations and paid models are switched off in settings (images)"
                )
            raise ImageError("no image service is set up")
        if not self._allow_paid:
            problems.append(
                "paid OpenRouter models are off (images.allow_paid; about 1 cent an image)"
            )
        raise ImageError("; ".join(problems))

    async def _openrouter(
        self, key: str, model: str, prompt: str, aspect: str, *, free: bool
    ) -> Picture:
        name = model.split("/")[-1].removesuffix(":free")
        self._notify(f"Drawing it with {name} (OpenRouter, {'free' if free else 'paid'})…")
        try:
            response = await self._client.post(
                f"{OPENROUTER}/images",
                headers={"Authorization": f"Bearer {key}", "X-Title": "JARVIS"},
                json={"model": model, "prompt": prompt, "aspect_ratio": aspect, "n": 1},
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            raise ImageError("took too long") from None
        except httpx.HTTPError as exc:
            raise ImageError(f"couldn't connect ({type(exc).__name__})") from None
        if response.status_code == 404:
            self._gone.add(model)
        if response.status_code == 400 and "parameter" in response.text.lower():
            # A provider that can't take aspect_ratio: once more without it.
            response = await self._client.post(
                f"{OPENROUTER}/images",
                headers={"Authorization": f"Bearer {key}", "X-Title": "JARVIS"},
                json={"model": model, "prompt": prompt, "n": 1},
                timeout=self._timeout,
            )
        if response.status_code != 200:
            raise ImageError(_openrouter_problem(response))
        try:
            item = response.json()["data"][0]
            data = base64.b64decode(item["b64_json"], validate=False)
        except (ValueError, KeyError, IndexError, TypeError):
            raise ImageError("the answer had no picture in it") from None
        mime = image_type(data)
        if mime is None:
            raise ImageError("the answer wasn't a PNG, JPEG or WebP picture")
        return Picture(data, mime, prompt, model, "OpenRouter", free)

    async def _cloudflare(self, account: str, token: str, prompt: str) -> Picture:
        self._notify("Drawing it with FLUX schnell (Cloudflare, free)…")
        url = CLOUDFLARE.format(account=quote(account, safe=""), model=self._cloudflare_model)
        try:
            response = await self._client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "prompt": prompt,
                    "steps": self._cloudflare_steps,
                    "seed": random.randint(1, 2_000_000_000),
                },
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            raise ImageError("took too long") from None
        except httpx.HTTPError as exc:  # never str(exc): the URL has the account id
            raise ImageError(f"couldn't connect ({type(exc).__name__})") from None
        if response.status_code in (401, 403):
            raise ImageError("the account ID or API token was refused")
        if response.status_code == 429:
            raise ImageError("today's free allowance is used up (it resets at 00:00 UTC)")
        if response.status_code != 200:
            raise ImageError(f"HTTP {response.status_code}")
        try:
            body = response.json()
            result = body.get("result", body)
            data = base64.b64decode(result["image"], validate=False)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ImageError("the answer had no picture in it") from None
        mime = image_type(data)
        if mime is None:
            raise ImageError("the answer wasn't a picture")
        model = self._cloudflare_model.split("/")[-1]
        return Picture(data, mime, prompt, model, "Cloudflare", True)

    async def _pollinations_image(self, prompt: str, aspect: str) -> Picture:
        width, height = _POLLINATIONS_SIZE[aspect]
        params: dict[str, str | int] = {
            "width": width,
            "height": height,
            "seed": random.randint(1, 2_000_000_000),
            "model": "flux",
            "nologo": "true",  # honoured only with a key; harmless otherwise
            "private": "true",  # kept out of the public feed
            "referrer": "jarvis-desktop",
        }
        if self._one_at_a_time.locked():
            self._notify("Waiting for the previous picture to finish…")
        async with self._one_at_a_time:
            self._notify("Drawing it with FLUX (Pollinations, free)…")
            try:
                response = await self._client.get(
                    POLLINATIONS + quote(prompt, safe=""),
                    params=params,
                    timeout=self._timeout,
                    follow_redirects=True,
                )
            except httpx.TimeoutException:
                raise ImageError("took too long") from None
            except httpx.HTTPError as exc:
                raise ImageError(f"couldn't connect ({type(exc).__name__})") from None
        if response.status_code in (402, 429):
            wait = response.headers.get("retry-after", "").strip()
            after = f" Try again in about {wait} s." if wait.isdigit() else ""
            raise ImageError(f"busy: free pictures are one at a time, with a pause between.{after}")
        if response.status_code != 200:
            raise ImageError(f"HTTP {response.status_code}")
        data = response.content
        if len(data) > MAX_BYTES:
            raise ImageError("the picture was too large")
        mime = image_type(data)
        if mime is None:
            raise ImageError("the answer wasn't a picture")
        return Picture(data, mime, prompt, "flux", "Pollinations", True)


def _openrouter_problem(response: httpx.Response) -> str:
    status = response.status_code
    try:
        detail = str(response.json().get("error", {}).get("message", ""))[:160]
    except (ValueError, AttributeError):
        detail = ""
    if status == 401:
        return "the OpenRouter key was refused"
    if status == 402:
        return "OpenRouter wants more than $1 of credit on the account for image models"
    if status == 429:
        return "rate-limited, try again in a minute"
    return f"HTTP {status}" + (f": {detail}" if detail else "")


ShowFn = Callable[[Picture], object]

_MODEL_FILE = re.compile(
    r"\b(3d|three[- ]d)\s+(model|mesh|object|asset|file)s?\b|\.(glb|gltf|obj|stl|fbx|usdz)\b|"
    r"\b(glb|gltf|stl|fbx|usdz)\b|\b3d[- ]print",
    re.IGNORECASE,
)
_THREE_D = re.compile(r"\b(3d|three[- ]d)\b", re.IGNORECASE)


def wants_model_file(text: str) -> bool:
    return bool(_MODEL_FILE.search(text))


def register_image_tools(registry: ToolRegistry, maker: ImageMaker, show: ShowFn) -> None:
    async def make_image(
        prompt: str = "", aspect: str = "1:1", style: str = "", three_d: bool = False
    ) -> ToolResult:
        if not prompt.strip():
            return ToolResult(False, "What should the picture show?")
        model_file = wants_model_file(prompt) or wants_model_file(style)
        three_d = three_d or model_file or bool(_THREE_D.search(style))
        text = styled(prompt, style, three_d)
        try:
            picture = await maker.make(text, aspect)
        except ImageError as exc:
            return ToolResult(False, f"Couldn't make the picture: {exc}.")
        show(picture)
        note = (
            " No free model can make an actual 3D model file, so this is a 3D-style render."
            if model_file
            else ""
        )
        return ToolResult(
            True,
            f"Made the picture with {picture.credit}. It's in the preview window; "
            f"Save keeps it, Discard throws it away.{note}",
            {"model": picture.model, "provider": picture.source},
        )

    registry.register(
        ToolSpec(
            "make_image",
            "Generate a picture from a description (image, photo, drawing, wallpaper, logo, "
            "art, 3D render) and show it in a preview window where the user can save it. "
            "Describe the subject in detail in prompt. three_d: a 3D-render look (no tool "
            "makes 3D model files).",
            {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "maxLength": MAX_PROMPT},
                    "aspect": {"type": "string", "enum": list(ASPECTS)},
                    "style": {
                        "type": "string",
                        "maxLength": 200,
                        "description": "e.g. 'watercolour', 'photorealistic', 'anime'",
                    },
                    "three_d": {"type": "boolean"},
                },
                "required": ["prompt"],
            },
            make_image,
            Risk.LOW,
            lambda a: f"Make a picture of {str(a.get('prompt', ''))[:80]}",
            group="images",
        )
    )


# --- requests JARVIS understands without asking a model ---

_NOUN = (
    r"(?P<noun>images?|pictures?|pics?|photos?|photographs?|drawings?|illustrations?|"
    r"wallpapers?|paintings?|artwork|art|logos?|icons?|posters?|renders?|rendering|"
    r"sketch(?:es)?|portraits?|concept art|3d (?:models?|renders?|objects?|meshes?|assets?)|"
    r"models?)"
)
_IMAGE = re.compile(
    r"^\s*(?:(?:hey |ok |okay )?jarvis[, ]+)?(?:(?:can|could|would) you |please |i want you to "
    r"|i'd like you to )?(?:please )?(?:generate|create|make|draw|paint|design|render|produce|"
    r"sketch)\b\s+(?:me\s+)?(?:an?\s+|the\s+|some\s+|\d+\s+)?"
    r"(?P<style>[\w\s,-]{0,40}?)\s*" + _NOUN + r"\b\s*(?P<link>of|showing|with|for|about|"
    r"that shows)?\s*(?P<what>.*)$",
    re.IGNORECASE,
)
_DRAW = re.compile(
    r"^\s*(?:(?:can|could|would) you |please )?(?:draw|paint|sketch)\s+(?:me\s+)?"
    r"(?P<what>(?:an?|the|some)\s+.+)$",
    re.IGNORECASE,
)
_ASPECT = re.compile(
    r",?\s*\b(?:in\s+|as\s+an?\s+)?(wide ?screen|wide|landscape|16:9|desktop wallpaper|banner|"
    r"tall|portrait orientation|vertical|9:16|phone wallpaper|square|1:1|4:3|3:4|3:2|2:3)"
    r"(?:\s+(?:format|size|orientation|aspect ratio))?\b",
    re.IGNORECASE,
)
_ASPECT_WORDS = {
    "wide": "16:9", "widescreen": "16:9", "wide screen": "16:9", "landscape": "16:9",
    "16:9": "16:9", "desktop wallpaper": "16:9", "banner": "16:9", "tall": "9:16",
    "portrait orientation": "9:16", "vertical": "9:16", "9:16": "9:16",
    "phone wallpaper": "9:16", "square": "1:1", "1:1": "1:1", "4:3": "4:3", "3:4": "3:4",
    "3:2": "3:2", "2:3": "2:3",
}  # fmt: skip
_NOT_A_PICTURE = re.compile(
    r"\b(ppt|pptx|power ?point|presentation|slides?|deck|workflow|document|report|folder|"
    r"file named|spreadsheet|excel|word doc|reminder|task|note|email|reply|list)\b",
    re.IGNORECASE,
)
# Plain words for "a picture": left out of the prompt. Others (a logo, a poster) stay.
_GENERIC = re.compile(r"^(images?|pictures?|pics?|renders?|rendering|artwork|art)$", re.I)
_FORMAT = re.compile(
    r"\s*\b(?:as|in)\s+(?:an?\s+)?\.?(glb|gltf|obj|stl|fbx|usdz)(?:\s+file)?\b", re.I
)


@dataclass(frozen=True, slots=True)
class ImageRequest:
    prompt: str
    aspect: str
    style: str
    three_d: bool  # a 3D-render look
    model: bool = False  # a real 3D model (.glb) was asked for


def image_command(text: str) -> ImageRequest | None:
    """ "generate an image of a red panda", "draw a castle at dusk", "make a 3D model of…"."""
    if _NOT_A_PICTURE.search(text):
        return None
    match = _IMAGE.match(text) or _DRAW.match(text)
    if match is None:
        return None
    groups = match.groupdict()
    noun = (groups.get("noun") or "").lower()
    if noun.startswith("model") and not re.search(r"\b(3d|three[- ]d)\b", text, re.I):
        return None  # "make a model of …" is ambiguous without "3D"
    what = _FORMAT.sub("", match.group("what"))
    aspect = "1:1"
    if found := _ASPECT.search(what):
        aspect = _ASPECT_WORDS.get(found.group(1).lower(), "1:1")
        what = (what[: found.start()] + what[found.end() :]).strip()
    elif found := _ASPECT.search(text):
        aspect = _ASPECT_WORDS.get(found.group(1).lower(), "1:1")
    what = what.strip(" .,?!")
    if not what:
        return None
    style = (groups.get("style") or "").strip(" ,")
    model = wants_model_file(text) or noun.startswith(
        ("3d model", "3d object", "3d mesh", "3d asset", "model")
    )
    three_d = model or bool(_THREE_D.search(text))
    style = re.sub(r"\b(3d|three[- ]d)\b", "", style, flags=re.IGNORECASE).strip(" ,")
    if re.fullmatch(r"(?:new|nice|cool|good|quick|simple|beautiful)?", style, re.IGNORECASE):
        style = ""
    if noun.startswith("wallpaper") and aspect == "1:1":
        aspect = "16:9"
    if noun and not _GENERIC.match(noun) and not noun.startswith(("3d", "model")):
        link = groups.get("link") or "of"
        article = "" if noun.endswith("s") or noun == "concept art" else "a "
        what = f"{article}{noun} {link} {what}"
    return ImageRequest(what, aspect, style, three_d, model)
