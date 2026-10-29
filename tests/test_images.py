"""Pictures and 3D models: free services in order, the request parser, the preview window."""

from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path
from typing import Any

import httpx
import pytest

from jarvis.agent.assistant import Assistant
from jarvis.app.events import ChatEntry, EntryKind, EventBus
from jarvis.app.state import StateMachine
from jarvis.storage.db import HistoryStore
from jarvis.tools.files import FileArea
from jarvis.tools.images import (
    ImageError,
    ImageMaker,
    ImageRequest,
    Picture,
    image_command,
    image_type,
    is_free_listing,
    register_image_tools,
)
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.threed import Model3D, ModelError, mesh_in, register_model_tools
from tests.test_assistant import ScriptedRouter


def png(width: int = 4, height: int = 3) -> bytes:
    """A tiny real PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + b"\x22\xd3\xee" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


PNG = png()


class Services:
    """A fake internet: OpenRouter, Cloudflare and Pollinations."""

    def __init__(self) -> None:
        self.listed: list[dict[str, Any]] = [
            {"id": "openai/gpt-5-image", "pricing": {"prompt": "0.00001", "image": "0.04"}}
        ]
        self.cloudflare_status = 200
        self.pollinations_status = 200
        self.calls: list[str] = []
        self.bodies: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "openrouter.ai/api/v1/models" in url:
            self.calls.append("openrouter-list")
            return httpx.Response(200, json={"data": self.listed})
        if "openrouter.ai/api/v1/images" in url:
            body = json.loads(request.content)
            self.calls.append(f"openrouter:{body['model']}")
            self.bodies.append(body)
            item = {"b64_json": base64.b64encode(PNG).decode(), "media_type": "image/png"}
            return httpx.Response(200, json={"data": [item]})
        if "api.cloudflare.com" in url:
            self.calls.append("cloudflare")
            self.bodies.append(json.loads(request.content))
            if self.cloudflare_status != 200:
                return httpx.Response(self.cloudflare_status, json={"success": False})
            image = base64.b64encode(PNG).decode()
            return httpx.Response(200, json={"result": {"image": image}, "success": True})
        if "image.pollinations.ai" in url:
            self.calls.append("pollinations")
            if self.pollinations_status != 200:
                return httpx.Response(self.pollinations_status, headers={"Retry-After": "40"})
            return httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"})
        return httpx.Response(404)


def maker(services: Services, keys: dict[str, str], **options: Any) -> ImageMaker:
    client = httpx.AsyncClient(transport=httpx.MockTransport(services.handler))
    return ImageMaker(client, keys.get, **options)


def test_free_listings_and_picture_types() -> None:
    assert is_free_listing({"id": "x/y:free"})
    assert is_free_listing({"id": "x/y", "pricing": {"prompt": "0", "image": "0"}})
    assert not is_free_listing({"id": "x/y", "pricing": {"prompt": "0", "image": "0.04"}})
    assert not is_free_listing({"id": "x/y"})
    assert image_type(PNG) == "image/png"
    assert image_type(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert image_type(b"<html>busy</html>") is None


async def test_paid_openrouter_models_are_never_used_by_default() -> None:
    services = Services()
    made = await maker(services, {"OPENROUTER_API_KEY": "k"}).make("a red panda")
    assert made.source == "Pollinations" and made.free and made.data == PNG
    assert services.calls == ["openrouter-list", "pollinations"]


async def test_a_free_openrouter_image_model_is_used_when_one_appears() -> None:
    services = Services()
    services.listed.append({"id": "new/image-model:free", "pricing": {}})
    made = await maker(services, {"OPENROUTER_API_KEY": "k"}).make("a fox", "16:9")
    assert made.source == "OpenRouter" and made.model == "new/image-model:free" and made.free
    assert services.bodies[0]["aspect_ratio"] == "16:9"
    assert "Pollinations" not in made.credit and "free" in made.credit


async def test_cloudflare_comes_before_pollinations_when_its_keys_are_saved() -> None:
    services = Services()
    keys = {"CLOUDFLARE_ACCOUNT_ID": "acc", "CLOUDFLARE_API_TOKEN": "tok"}
    made = await maker(services, keys, cloudflare_steps=6).make("a castle")
    assert made.source == "Cloudflare" and made.model == "flux-1-schnell"
    assert services.bodies[0]["steps"] == 6 and services.calls == ["cloudflare"]
    services.cloudflare_status = 429  # allowance used up: Pollinations steps in
    made = await maker(services, keys).make("a castle")
    assert made.source == "Pollinations"


async def test_every_failure_is_named_and_paid_is_mentioned() -> None:
    services = Services()
    services.pollinations_status = 402
    with pytest.raises(ImageError) as caught:
        await maker(services, {}).make("a castle")
    text = str(caught.value)
    assert "Pollinations: busy" in text and "40 s" in text and "allow_paid" in text


async def test_paid_models_only_when_switched_on() -> None:
    services = Services()
    services.pollinations_status = 500
    made = await maker(
        services,
        {"OPENROUTER_API_KEY": "k"},
        allow_paid=True,
        paid_models=["google/gemini-2.5-flash-image"],
    ).make("a castle")
    assert made.source == "OpenRouter" and not made.free and "paid" in made.credit


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "generate an image of a red panda astronaut",
            ImageRequest("a red panda astronaut", "1:1", "", False),
        ),
        (
            "make a cyberpunk wallpaper of tokyo at night",
            ImageRequest("a wallpaper of tokyo at night", "16:9", "cyberpunk", False),
        ),
        ("draw a castle at dusk", ImageRequest("a castle at dusk", "1:1", "", False)),
        (
            "Jarvis, generate a photorealistic picture of a lion, widescreen",
            ImageRequest("a lion", "16:9", "photorealistic", False),
        ),
        (
            "can you make me a logo for my bakery called Crumbs",
            ImageRequest("a logo for my bakery called Crumbs", "1:1", "", False),
        ),
        ("create a 3D render of a sports car", ImageRequest("a sports car", "1:1", "", True)),
        ("make a 3d model of a chair as stl", ImageRequest("a chair", "1:1", "", True, True)),
        ("create a 3D model of a dragon", ImageRequest("a dragon", "1:1", "", True, True)),
    ],
)
def test_picture_requests_are_understood(text: str, expected: ImageRequest) -> None:
    assert image_command(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "make a presentation on cats",
        "make a note",
        "render the video",
        "make an image",
        "show me photos of my trip",
        "make a model of the solar system",
        "create a workflow that draws a chart",
    ],
)
def test_other_requests_are_left_alone(text: str) -> None:
    assert image_command(text) is None


async def test_the_tool_shows_the_picture_and_saves_nothing() -> None:
    shown: list[Picture] = []
    registry = ToolRegistry()
    register_image_tools(registry, maker(Services(), {}), shown.append)
    spec = registry.get("make_image")
    assert spec is not None
    result = await registry.run(spec, {"prompt": "a lighthouse", "style": "watercolour"})
    assert result.ok and "preview window" in result.summary and len(shown) == 1
    assert shown[0].prompt == "a lighthouse. Style: watercolour"
    empty = await registry.run(spec, {"prompt": " "})
    assert not empty.ok


def test_the_textured_mesh_is_picked_from_a_space_answer() -> None:
    answer: tuple[Any, ...] = (
        {"value": "/tmp/white.glb"},
        {"value": "/tmp/textured.glb"},
        "<html>",
        {},
        1,
    )
    assert mesh_in(answer) == "/tmp/textured.glb"
    assert mesh_in(["/tmp/only.glb", "stats"]) == "/tmp/only.glb"
    assert mesh_in(["no", "files"]) is None


class FakeModeller:
    space = "tencent/Hunyuan3D-2"

    def __init__(self, fail: str = "") -> None:
        self.fail = fail
        self.pictures: list[Picture] = []

    async def make(self, picture: Picture) -> tuple[bytes, str, bool]:
        self.pictures.append(picture)
        if self.fail:
            raise ModelError(self.fail)
        return b"glTF-binary", ".glb", True


async def test_a_3d_model_is_drawn_then_built(tmp_path: Path) -> None:
    shown: list[Model3D] = []
    modeller = FakeModeller()
    registry = ToolRegistry()
    area = FileArea([tmp_path], [])
    register_model_tools(registry, maker(Services(), {}), modeller, area, shown.append)  # type: ignore[arg-type]
    spec = registry.get("make_3d_model")
    assert spec is not None
    result = await registry.run(spec, {"prompt": "a dragon"})
    assert result.ok and ".glb" in result.summary and shown[0].data == b"glTF-binary"
    assert "plain white background" in modeller.pictures[0].prompt
    (tmp_path / "photo.png").write_bytes(PNG)
    result = await registry.run(spec, {"source_image": str(tmp_path / "photo.png")})
    assert result.ok and "your picture" in result.summary
    (tmp_path / "notes.txt").write_text("hi")
    result = await registry.run(spec, {"source_image": str(tmp_path / "notes.txt")})
    assert not result.ok and "isn't a PNG" in result.summary
    modeller.fail = "today's free Hugging Face GPU time is used up"
    result = await registry.run(spec, {"prompt": "a dragon"})
    assert not result.ok and "GPU time is used up" in result.summary


async def test_assistant_makes_a_picture_without_the_chat_model(tmp_path: Path) -> None:
    shown: list[Picture] = []
    registry = ToolRegistry()
    register_image_tools(registry, maker(Services(), {}), shown.append)
    bus = EventBus()
    entries: list[ChatEntry] = []

    async def on_entry(entry: ChatEntry) -> None:
        entries.append(entry)

    bus.subscribe(ChatEntry, on_entry)

    async def confirm(_: Any) -> bool:
        return True

    async def capture() -> bytes | None:
        return None

    router = ScriptedRouter()  # no replies queued: a model call would fail
    assistant = Assistant(
        router,  # type: ignore[arg-type]
        HistoryStore(tmp_path / "h.sqlite3"),
        registry,
        bus,
        StateMachine(bus),
        confirm,
        capture,
    )
    reply = await assistant.handle("draw a castle at dusk")
    assert reply.ok and "preview window" in reply.text and router.requests == []
    last = [e for e in entries if e.kind is EntryKind.ASSISTANT][-1]
    assert (last.provider, last.model, last.mode) == ("Pollinations", "flux", "image")
    assert len(shown) == 1


def test_the_preview_saves_only_when_asked(qtbot: Any, tmp_path: Path) -> None:
    from jarvis.ui.media_view import MediaPreview, file_name

    prompt = "a red panda. Style: watercolour"
    picture = Picture(PNG, "image/png", prompt, "flux", "Pollinations", True)
    folder = tmp_path / "Images"
    window = MediaPreview(picture, folder)
    qtbot.addWidget(window)
    assert not folder.exists()  # shown, not saved
    window.save.click()
    saved = folder / "red panda.png"
    assert saved.read_bytes() == PNG and not window.save.isEnabled()
    second = MediaPreview(picture, folder)
    qtbot.addWidget(second)
    second.save.click()
    assert len(list(folder.glob("*.png"))) == 2  # a new name, never overwritten
    assert file_name('a "cat": on/a mat?', ".png") == "cat on mat.png"


def test_a_model_preview_cleans_up_its_cache_file(qtbot: Any, tmp_path: Path) -> None:
    from jarvis.ui.media_view import MediaPreview

    picture = Picture(PNG, "image/png", "a dragon", "flux", "Pollinations", True)
    model = Model3D(b"glTF-binary", ".glb", picture, "tencent/Hunyuan3D-2", True)
    cache = tmp_path / "cache"
    window = MediaPreview(model, tmp_path / "Images", cache)
    qtbot.addWidget(window)
    assert len(list(cache.glob("preview-*.glb"))) == 1
    if window.flip.isVisible():  # the 3D view, with the picture one press away
        window.flip.click()
        assert window.flip.text() == "3D view"
    window.close()
    assert list(cache.glob("preview-*")) == []
    assert not (tmp_path / "Images").exists()
