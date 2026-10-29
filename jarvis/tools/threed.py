"""Real 3D models (.glb) from a picture, on Hugging Face's free GPUs.

No OpenRouter model makes 3D, and the owner's laptop can't run one (they need a 6-24 GB
graphics card), so JARVIS sends the picture to Tencent's Hunyuan3D-2 Space, which runs
on Hugging Face's ZeroGPU: free, with a daily allowance of GPU time per account (2 min
without a token, 5 min with a free ``HF_TOKEN``; a textured model reserves 90 s, a plain
one 40 s). The Space is called through ``gradio_client`` (Apache-2.0); its telemetry is
switched off before import. A description is first drawn as a picture by
:class:`jarvis.tools.images.ImageMaker` (one object, plain background), then turned
into the model.

The model is only shown (``show``); nothing is written to the user's folders until they
press Save. The client's download is read into memory and its temporary folder removed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from jarvis.tools.files import FileAccessError, FileArea
from jarvis.tools.images import ImageError, ImageMaker, Picture, image_type
from jarvis.tools.registry import Risk, ToolRegistry, ToolResult, ToolSpec

HF_TOKEN = "HF_TOKEN"
MAX_MODEL_BYTES = 120_000_000
FROM_FILE = "your file"
MESH_SUFFIXES = (".glb", ".gltf", ".obj", ".ply", ".stl")

# What helps image-to-3D most: one whole object, nothing behind it.
OBJECT_STYLE = (
    "a single object, whole and centred, front three-quarter view, plain white background, "
    "soft even lighting, no text"
)


class ModelError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Model3D:
    data: bytes
    suffix: str  # ".glb" normally
    picture: Picture  # the picture it was made from
    space: str
    textured: bool

    @property
    def credit(self) -> str:
        made = (
            "your picture"
            if self.picture.source == FROM_FILE
            else (f"a picture by {self.picture.credit}")
        )
        return f"Hunyuan3D-2 on Hugging Face (free GPU), from {made}"


def mesh_in(result: Any) -> str | None:
    """The mesh file in a Space's answer: the textured one when there are two."""
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            if value.lower().endswith(MESH_SUFFIXES):
                found.append(value)
        elif isinstance(value, dict):
            for key in ("value", "path", "name"):
                if key in value:
                    walk(value[key])
        elif isinstance(value, list | tuple):
            for item in value:
                walk(item)

    walk(result)
    if not found:
        return None
    # generation_all answers (white mesh, textured mesh, html, stats, seed).
    return found[1] if len(found) > 1 else found[0]


def _arguments(image: Any, textured: bool) -> tuple[list[Any], str]:
    """Hunyuan3D-2's inputs, in its order: caption, image, 4 multi-view images, steps,
    guidance, seed, octree resolution, remove background, chunks, random seed."""
    args = [None, image, None, None, None, None, 30, 5.0, 1234, 256, True, 8000, True]
    return args, "/generation_all" if textured else "/shape_generation"


class SpaceModeller:
    """Calls the Space in a worker thread (``gradio_client`` is synchronous)."""

    def __init__(
        self,
        token: Callable[[], str | None],
        *,
        space: str = "tencent/Hunyuan3D-2",
        textured: bool = True,
        timeout_s: float = 600.0,
        work_dir: Path | None = None,
    ) -> None:
        self._token = token
        self._space = space
        self._textured = textured
        self._timeout_s = timeout_s
        self._work_dir = work_dir

    def _run(self, picture: Picture, textured: bool) -> tuple[bytes, str]:
        os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        try:
            from gradio_client import Client, handle_file
        except ImportError:
            raise ModelError(
                "the gradio_client package isn't installed (pip install -e .)"
            ) from None
        folder = Path(tempfile.mkdtemp(prefix="jarvis-3d-", dir=self._work_dir))
        try:
            source = folder / f"source{picture.extension}"
            source.write_bytes(picture.data)
            token = (self._token() or "").strip() or None
            try:
                client = Client(
                    self._space,
                    token=token,
                    verbose=False,
                    download_files=str(folder / "out"),
                    analytics_enabled=False,
                )
                args, api = _arguments(handle_file(str(source)), textured)
                job = client.submit(*args, api_name=api)
                result = job.result(timeout=self._timeout_s)
            except Exception as exc:  # the client raises many kinds; never shows the token
                raise ModelError(_space_problem(exc)) from None
            path = mesh_in(result)
            if path is None or not Path(path).is_file():
                raise ModelError("the Space answered without a 3D file")
            mesh = Path(path)
            if mesh.stat().st_size > MAX_MODEL_BYTES:
                raise ModelError("the 3D file was too large")
            return mesh.read_bytes(), mesh.suffix.lower()
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    async def make(self, picture: Picture) -> tuple[bytes, str, bool]:
        try:
            data, suffix = await asyncio.to_thread(self._run, picture, self._textured)
            return data, suffix, self._textured
        except ModelError as exc:
            if not self._textured or "quota" not in str(exc).lower():
                raise
            # A textured model reserves 90 s of GPU; a plain one only 40 s.
            logger.info("3D: textured model over the allowance; trying an untextured one")
            data, suffix = await asyncio.to_thread(self._run, picture, False)
            return data, suffix, False

    @property
    def space(self) -> str:
        return self._space


def _space_problem(exc: Exception) -> str:
    text = str(exc)
    lower = text.lower()
    if "quota" in lower or "exceeded" in lower:
        return (
            "today's free Hugging Face GPU time is used up; it refills 24 hours after first "
            "use (a free Hugging Face token, HF_TOKEN, gives 5 minutes a day instead of 2)"
        )
    if "timeout" in lower or isinstance(exc, TimeoutError):
        return "the Space took too long (it may be queueing); try again in a few minutes"
    if "401" in lower or "unauthorized" in lower or "invalid token" in lower:
        return "Hugging Face refused the token (HF_TOKEN)"
    if "paused" in lower or "sleeping" in lower or "runtime error" in lower or "404" in lower:
        return "the 3D Space is down or paused right now"
    return f"the 3D Space failed ({type(exc).__name__})"


ShowModelFn = Callable[[Model3D], object]
PickFn = Callable[[], Awaitable[Path | None]]


async def picture_from_file(path: Path) -> Picture:
    data = await asyncio.to_thread(path.read_bytes)
    mime = image_type(data)
    if mime is None:
        raise ModelError(f"{path.name} isn't a PNG, JPEG or WebP picture")
    return Picture(data, mime, path.stem, "your picture", FROM_FILE, True)


def register_model_tools(
    registry: ToolRegistry,
    maker: ImageMaker,
    modeller: SpaceModeller,
    area: FileArea,
    show: ShowModelFn,
    *,
    pick_file: PickFn | None = None,
    notify: Callable[[str], None] | None = None,
) -> None:
    say = notify or (lambda _text: None)

    async def make_3d_model(prompt: str = "", source_image: str = "") -> ToolResult:
        raw = source_image.strip()
        try:
            if raw.lower() in ("upload", "uploaded", "attached", "pick", "picked"):
                if pick_file is None:
                    return ToolResult(False, "I can't open a file picker here; say where it is.")
                picked = await pick_file()
                if picked is None:
                    return ToolResult(False, "No picture was picked, so I didn't make a model.")
                picture = await picture_from_file(picked)
            elif raw:
                picture = await picture_from_file(area.resolve(raw))
            elif prompt.strip():
                picture = await maker.make(f"{prompt.strip()}. {OBJECT_STYLE}", "1:1")
            else:
                return ToolResult(False, "What should the 3D model be of?")
        except (ImageError, ModelError) as exc:
            return ToolResult(False, f"Couldn't make the picture for the model: {exc}.")
        except (FileAccessError, OSError) as exc:
            return ToolResult(False, f"Couldn't use that picture: {exc}.")
        say("Turning the picture into a 3D model on Hugging Face (free GPU, 1-3 min)…")
        try:
            data, suffix, textured = await modeller.make(picture)
        except ModelError as exc:
            return ToolResult(False, f"Couldn't make the 3D model: {exc}.")
        model = Model3D(data, suffix, picture, modeller.space, textured)
        show(model)
        plain = "" if textured else " It has no colours (a plain shape), to fit the free GPU time."
        return ToolResult(
            True,
            f"Made the 3D model with {model.credit}. It's in the preview window; you can turn "
            f"it with the mouse, and Save keeps it as a {suffix} file.{plain}",
            {"model": "Hunyuan3D-2", "provider": "Hugging Face"},
        )

    registry.register(
        ToolSpec(
            "make_3d_model",
            "Make a real 3D model (.glb) from a description, or from a picture (source_image: "
            "a path, or 'upload' for a file the user gives), and show it in a 3D preview "
            "window where the user can save it. Takes 1-3 minutes.",
            {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "maxLength": 600},
                    "source_image": {"type": "string", "maxLength": 400},
                },
                "required": [],
            },
            make_3d_model,
            Risk.LOW,
            lambda a: (
                "Make a 3D model"
                + (f" of {str(a['prompt'])[:80]}" if a.get("prompt") else " from a picture")
            ),
            group="images",
        )
    )
