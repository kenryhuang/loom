"""Artifact-backed image messages. Binary data is materialized only at the API boundary."""

import base64
import hashlib
import re
import struct
import zlib
from dataclasses import dataclass


def image_dimensions(raw, media_type="image/png"):
    if media_type != "image/png" or not isinstance(raw, bytes) or len(raw) < 33 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        raise ValueError("Only PNG screenshot artifacts are supported")
    width, height = struct.unpack(">II", raw[16:24])
    if not 10 < width <= 16000 or not 10 < height <= 16000 or width * height > 40_000_000:
        raise ValueError("Image dimensions exceed screenshot limits")
    offset, has_pixels = 8, False
    while offset + 12 <= len(raw):
        size = struct.unpack(">I", raw[offset:offset + 4])[0]
        kind = raw[offset + 4:offset + 8]
        end = offset + 12 + size
        if end > len(raw) or zlib.crc32(raw[offset + 4:end - 4]) != struct.unpack(">I", raw[end - 4:end])[0]:
            raise ValueError("PNG chunk integrity failure")
        has_pixels = has_pixels or kind == b"IDAT"
        if kind == b"IEND":
            if end != len(raw) or not has_pixels or size:
                raise ValueError("Incomplete PNG image")
            return width, height
        offset = end
    raise ValueError("Incomplete PNG image")


@dataclass(frozen=True, slots=True)
class ImagePart:
    artifact: dict
    width: int
    height: int
    evidence_id: str = "screenshot"

    def __post_init__(self):
        if not isinstance(self.artifact, dict) or not re.fullmatch(r"[0-9a-f]{64}", self.artifact.get("sha256", "")):
            raise ValueError("ImagePart requires an artifact digest")
        if self.artifact.get("media_type") != "image/png" or type(self.width) is not int or type(self.height) is not int:
            raise ValueError("ImagePart requires PNG dimensions")
        if min(self.width, self.height) <= 10 or self.width * self.height > 40_000_000:
            raise ValueError("ImagePart dimensions exceed limits")


def image_content(part, reader):
    if isinstance(part, dict):
        part = ImagePart(**part)
    raw = reader(part.artifact["sha256"])
    if len(raw) > 4_000_000 or hashlib.sha256(raw).hexdigest() != part.artifact["sha256"]:
        raise ValueError("Image artifact integrity or size check failed")
    if image_dimensions(raw) != (part.width, part.height):
        raise ValueError("Image artifact dimensions do not match its reference")
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(raw).decode()}}


def image_tokens(images, provider):
    """Conservative reservation; provider usage remains authoritative."""
    high = getattr(provider, "request_options", {}).get("vl_high_resolution_images", False)
    cap = 16384 if high else 1280
    qwen = getattr(provider, "model", "").startswith("qwen3.8-omni")
    return sum(min(cap, max(24, ((p.width + 31) // 32) * ((p.height + 31) // 32))) if qwen else 16384 for p in images)
