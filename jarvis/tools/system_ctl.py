"""Volume, brightness, wallpaper, dark mode and Wi-Fi, each read before it is changed so
it can be undone.

Volume uses Windows' Core Audio (through pycaw), brightness the WMI monitor brightness
class (laptop and other built-in screens only), wallpaper SystemParametersInfo, dark
mode the two per-user "use light theme" values Settings itself writes (then tells open
windows), and Wi-Fi the Windows radio API Settings' Wi-Fi switch uses. Nothing here runs
a command line or needs admin rights.
"""

from __future__ import annotations

import asyncio
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
    def dark_mode(self) -> bool: ...
    def set_dark_mode(self, on: bool) -> None: ...
    def wifi(self) -> bool: ...
    def set_wifi(self, on: bool) -> None: ...


_THEME_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_THEME_VALUES = ("AppsUseLightTheme", "SystemUsesLightTheme")


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

    # --- dark mode ---

    def dark_mode(self) -> bool:
        if sys.platform != "win32":
            raise SettingUnavailableError("this only works on Windows")
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _THEME_KEY) as key:
                return int(winreg.QueryValueEx(key, _THEME_VALUES[0])[0]) == 0
        except OSError:
            return False  # never set: Windows' default light theme

    def set_dark_mode(self, on: bool) -> None:
        if sys.platform != "win32":
            raise SettingUnavailableError("this only works on Windows")
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _THEME_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            for name in _THEME_VALUES:
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, 0 if on else 1)
        # Tell open windows, as Settings does (WM_SETTINGCHANGE "ImmersiveColorSet").
        result = ctypes.c_size_t()
        ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined,unused-ignore]
            0xFFFF, 0x001A, 0, "ImmersiveColorSet", 0x0002, 200, ctypes.byref(result)
        )

    # --- Wi-Fi ---

    @staticmethod
    def _radio(action: Any) -> Any:
        _require_windows()
        try:
            from winrt.windows.devices.radios import Radio, RadioKind
        except ImportError as exc:
            raise SettingUnavailableError("the winrt radio package isn't installed") from exc

        async def go() -> Any:
            for radio in await Radio.get_radios_async():
                if radio.kind == RadioKind.WI_FI:
                    return await action(radio)
            raise SettingUnavailableError("this PC has no Wi-Fi radio")

        return asyncio.run(go())  # on the tool's worker thread, its own loop

    def wifi(self) -> bool:
        return bool(self._radio(_read_radio))

    def set_wifi(self, on: bool) -> None:
        async def switch(radio: Any) -> None:
            from winrt.windows.devices.radios import RadioAccessStatus, RadioState

            if await radio.request_access_async() != RadioAccessStatus.ALLOWED:
                raise SettingUnavailableError("Windows didn't allow changing the Wi-Fi radio")
            await radio.set_state_async(RadioState.ON if on else RadioState.OFF)

        self._radio(switch)


async def _read_radio(radio: Any) -> bool:
    from winrt.windows.devices.radios import RadioState

    return bool(radio.state == RadioState.ON)
