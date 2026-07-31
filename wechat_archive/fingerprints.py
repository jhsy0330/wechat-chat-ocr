from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageOps

from .models import Message


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def image_dhash(path: Path) -> str:
    with Image.open(path) as image:
        return image_object_dhash(image)


def image_object_dhash(image: Image.Image) -> str:
    grayscale = ImageOps.grayscale(image).resize((9, 8))
    pixels = list(grayscale.get_flattened_data())
    bits = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            bits = (bits << 1) | int(
                pixels[offset + column] > pixels[offset + column + 1]
            )
    return f"{bits:016x}"


def hash_distance(left: str, right: str) -> int:
    if not left or not right or len(left) != len(right):
        return 64
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError:
        return 64


def normalized_message_text(message: Message) -> str:
    text = message.original_text or message.text
    return "".join(text.casefold().split())


def message_fingerprint(message: Message) -> str:
    if message.kind == "voice":
        payload = "\x1f".join(
            (
                message.speaker.strip().casefold(),
                message.kind,
                str(message.voice_duration_seconds or ""),
                message.voice_visual_hash or "",
            )
        )
        return hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()
    payload = "\x1f".join(
        (
            message.speaker.strip().casefold(),
            normalized_message_text(message),
            message.kind,
        )
    )
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()
