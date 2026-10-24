"""One photo from the default webcam, through Qt Multimedia. Never saved to disk.

Called only by the ``look_through_webcam`` tool, which asks the user first. The camera
is switched on for the photo and off straight after (its light shows while it's on).
"""

from __future__ import annotations

import asyncio

from loguru import logger

_SETTLE_S = 0.8  # let exposure and focus adjust before the photo


class CameraError(RuntimeError):
    pass


async def capture_webcam(timeout_s: float = 8.0) -> bytes | None:
    """A JPEG from the default camera, or None when there's no camera."""
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtMultimedia import QCamera, QImageCapture, QMediaCaptureSession, QMediaDevices

    device = QMediaDevices.defaultVideoInput()
    if device.isNull():
        return None
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()
    shot: asyncio.Future[object] = loop.create_future()

    def fail(message: str) -> None:
        for future in (ready, shot):
            if not future.done():
                future.set_exception(CameraError(message or "camera error"))

    camera = QCamera(device)
    session = QMediaCaptureSession()
    capture = QImageCapture()
    session.setCamera(camera)
    session.setImageCapture(capture)

    def on_ready(ok: bool) -> None:
        if ok and not ready.done():
            ready.set_result(None)

    def on_image(_id: int, image: object) -> None:
        if not shot.done():
            shot.set_result(image)

    capture.readyForCaptureChanged.connect(on_ready)
    capture.imageCaptured.connect(on_image)
    capture.errorOccurred.connect(lambda _id, _error, message: fail(message))
    camera.errorOccurred.connect(lambda _error, message: fail(message))
    try:
        camera.start()
        if capture.isReadyForCapture() and not ready.done():
            ready.set_result(None)
        await asyncio.wait_for(ready, timeout_s)
        await asyncio.sleep(_SETTLE_S)
        capture.capture()
        image = await asyncio.wait_for(shot, timeout_s)
    except TimeoutError as exc:
        raise CameraError("the camera didn't respond; another app may be using it") from exc
    finally:
        camera.stop()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    scaled = image.scaledToWidth(min(image.width(), 1024))  # type: ignore[attr-defined]
    if not scaled.save(buffer, "JPG", 85):
        raise CameraError("the photo couldn't be encoded")
    logger.info("Took one webcam photo for the assistant")
    return bytes(buffer.data().data())
