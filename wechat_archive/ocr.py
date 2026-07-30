from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .models import OCRLine


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SWIFT_SOURCE = PROJECT_ROOT / "native" / "vision_ocr.swift"
BUILD_DIR = PROJECT_ROOT / ".build"
HELPER = BUILD_DIR / "vision_ocr"


class VisionOCR:
    def ensure_helper(self) -> None:
        BUILD_DIR.mkdir(exist_ok=True)
        if HELPER.exists() and HELPER.stat().st_mtime >= SWIFT_SOURCE.stat().st_mtime:
            return
        result = subprocess.run(
            ["/usr/bin/swiftc", "-O", str(SWIFT_SOURCE), "-o", str(HELPER)],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise RuntimeError(
                "无法编译 macOS Vision OCR 组件：" + result.stderr.strip()
            )

    def recognize(self, image: Path) -> list[OCRLine]:
        self.ensure_helper()
        result = subprocess.run(
            [str(HELPER), str(image)], capture_output=True, text=True
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"OCR 失败：{image.name}")
        try:
            records = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("OCR 组件返回了无效数据") from error
        return [
            OCRLine(
                text=str(record["text"]).strip(),
                confidence=float(record["confidence"]),
                x=float(record["x"]),
                y=float(record["y"]),
                width=float(record["width"]),
                height=float(record["height"]),
                source=image.name,
            )
            for record in records
            if str(record.get("text", "")).strip()
        ]
