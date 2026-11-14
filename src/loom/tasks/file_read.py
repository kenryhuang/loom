"""Byte-range file reads with explicit UTF-8 boundaries and source identity."""

import codecs
import hashlib


def file_read_payload(raw, path, absolute_path, *, offset_bytes=0, max_bytes=20000):
    if type(offset_bytes) is not int or not 0 <= offset_bytes <= len(raw):
        raise ValueError("offset_bytes must be a byte position within the file")
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if offset_bytes < len(raw) and raw[offset_bytes] & 0xC0 == 0x80:
        raise ValueError("offset_bytes is inside a UTF-8 character; use the previous next_offset_bytes")
    end = min(len(raw), offset_bytes + max_bytes)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    content = decoder.decode(raw[offset_bytes:end], final=end == len(raw))
    end -= len(decoder.getstate()[0])
    if end == offset_bytes and end < len(raw):
        raise ValueError("max_bytes cannot fit the next UTF-8 character")
    return {"path": path, "content": content, "bytes_read": end - offset_bytes,
            "truncated": offset_bytes > 0 or end < len(raw), "next_offset_bytes": end if end < len(raw) else None,
            "total_bytes": len(raw), "resource": {"path": absolute_path,
                "after_sha256": hashlib.sha256(raw).hexdigest(),
                "returned_sha256": hashlib.sha256(raw[offset_bytes:end]).hexdigest(), "byte_range": [offset_bytes, end]}}
