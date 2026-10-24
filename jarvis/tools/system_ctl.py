"""Volume, brightness and wallpaper, each read before it is changed so it can be undone.

Volume uses Windows' Core Audio (through pycaw), brightness the WMI monitor brightness
class (laptop and other built-in screens only), wallpaper SystemParametersInfo. Nothing
here runs a command line.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from typing import Any, Protocol


class SettingUnavailableError(RuntimeError):
    pass


class SystemControl(Protocol):
    def volume(self) -> int: ...
    def set_volume(self, percent: int) -> None: ...
    def muted(self) -> bool: ...
    def set_muted(self, muted: bool) -> None: ...
    def brightness(self) -> int: ...
    def set_brightness(self, percent: int) -> None: ...
    def wallpaper(self) -> str: ...
    def set_wallpaper(self, path: Path) -> None: ...


def _require_windows() -> None:
    if sys.platform != "win32":
        raise SettingUnavailableError("this only works on Windows")


class WindowsSystem:
    # --- volume ---

    def _endpoint(self) -> Any:
        _require_windows()
        try:
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        except ImportError as exc:
            raise SettingUnavailableError("the pycaw package isn't installed") from exc
        speakers = AudioUtilities.GetSpeakers()
        endpoint = getattr(speakers, "EndpointVolume", None)  # pycaw 2025+
        if endpoint is not None:
            return endpoint
        from comtypes import CLSCTX_ALL

        interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return ctypes.cast(interface, ctypes.POINTER(IAudioEndpointVolume))

    def volume(self) -> int:
        return round(float(self._endpoint().GetMasterVolumeLevelScalar()) * 100)

    def set_volume(self, percent: int) -> None:
        self._endpoint().SetMasterVolumeLevelScalar(max(0, min(100, percent)) / 100, None)

    def muted(self) -> bool:
        return bool(self._endpoint().GetMute())

    def set_muted(self, muted: bool) -> None:
        self._endpoint().SetMute(int(muted), None)

    # --- brightness ---

    def _wmi(self) -> Any:
        _require_windows()
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        return win32com.client.GetObject(r"winmgmts:\\.\root\WMI")

    def brightness(self) -> int:
        for monitor in self._wmi().ExecQuery("SELECT CurrentBrightness FROM WmiMonitorBrightness"):
            return int(monitor.CurrentBrightness)
        raise SettingUnavailableError("this screen's brightness can't be set by Windows")

    def set_brightness(self, percent: int) -> None:
        methods = list(self._wmi().ExecQuery("SELECT * FROM WmiMonitorBrightnessMethods"))
        if not methods:
            raise SettingUnavailableError("this screen's brightness can't be set by Windows")
        for monitor in methods:
            monitor.WmiSetBrightness(1, max(0, min(100, percent)))

    # --- wallpaper ---

    def wallpaper(self) -> str:
        _require_windows()
        buffer = ctypes.create_unicode_buffer(520)
        ctypes.windll.user32.SystemParametersInfoW(0x0073, 520, buffer, 0)  # type: ignore[attr-defined]
        return str(buffer.value)

    def set_wallpaper(self, path: Path) -> None:
        _require_windows()
        # SPI_SETDESKWALLPAPER, saved to the profile and broadcast to open windows.
        ok = ctypes.windll.user32.SystemParametersInfoW(0x0014, 0, str(path), 0x01 | 0x02)  # type: ignore[attr-defined]
        if not ok:
            raise SettingUnavailableError("Windows refused that picture as a wallpaper")
