from __future__ import annotations

import subprocess
from pathlib import Path

import AppKit
import ApplicationServices
import Quartz

from .models import WindowInfo


WECHAT_BUNDLE_IDS = {"com.tencent.xinWeChat", "com.tencent.WeChat"}
WECHAT_OWNER_NAMES = ("wechat", "weixin", "微信")


def list_wechat_windows() -> list[WindowInfo]:
    options = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    records = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
    windows: list[WindowInfo] = []
    for record in records:
        owner = str(record.get(Quartz.kCGWindowOwnerName, ""))
        if not any(name in owner.lower() for name in WECHAT_OWNER_NAMES):
            continue
        bounds = record.get(Quartz.kCGWindowBounds, {})
        width = float(bounds.get("Width", 0))
        height = float(bounds.get("Height", 0))
        layer = int(record.get(Quartz.kCGWindowLayer, 0))
        if layer != 0 or width < 480 or height < 360:
            continue
        windows.append(
            WindowInfo(
                window_id=int(record[Quartz.kCGWindowNumber]),
                pid=int(record[Quartz.kCGWindowOwnerPID]),
                owner=owner,
                title=str(record.get(Quartz.kCGWindowName, "")),
                x=float(bounds.get("X", 0)),
                y=float(bounds.get("Y", 0)),
                width=width,
                height=height,
            )
        )
    return sorted(windows, key=lambda item: item.width * item.height, reverse=True)


def accessibility_granted() -> bool:
    return bool(ApplicationServices.AXIsProcessTrusted())


def request_accessibility() -> bool:
    options = {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
    return bool(ApplicationServices.AXIsProcessTrustedWithOptions(options))


def screen_capture_granted() -> bool:
    check = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
    return bool(check()) if check else True


def request_screen_capture() -> bool:
    request = getattr(Quartz, "CGRequestScreenCaptureAccess", None)
    return bool(request()) if request else True


def activate_window(window: WindowInfo) -> bool:
    application = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(
        window.pid
    )
    if application is None:
        return False
    return bool(
        application.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    )


def is_frontmost_wechat(window: WindowInfo) -> bool:
    application = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if application is None:
        return False
    bundle_id = str(application.bundleIdentifier() or "")
    name = str(application.localizedName() or "").lower()
    return (
        application.processIdentifier() == window.pid
        or bundle_id in WECHAT_BUNDLE_IDS
        or any(owner in name for owner in WECHAT_OWNER_NAMES)
    )


def capture_window(window_id: int, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "/usr/sbin/screencapture",
            "-x",
            "-o",
            f"-l{window_id}",
            str(destination),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode or not destination.exists() or destination.stat().st_size == 0:
        detail = result.stderr.strip() or "没有生成截图"
        raise RuntimeError(f"微信窗口截图失败：{detail}")


def post_scroll(point: tuple[float, float], pixels: int) -> None:
    event = Quartz.CGEventCreateScrollWheelEvent(
        None, Quartz.kCGScrollEventUnitPixel, 1, int(pixels)
    )
    if event is None:
        raise RuntimeError("无法创建滚动事件")
    Quartz.CGEventSetLocation(event, point)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def escape_pressed() -> bool:
    return bool(
        Quartz.CGEventSourceKeyState(
            Quartz.kCGEventSourceStateCombinedSessionState, 53
        )
    )
