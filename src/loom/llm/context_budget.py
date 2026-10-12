"""Local model capacity metadata and explicit, tokenizer-independent estimates."""

import math

# Model Studio documentation, checked 2026-10-12:
# https://help.aliyun.com/zh/model-studio/text-generation-model
# https://help.aliyun.com/zh/model-studio/qwen3-8-max
# Exact IDs only: custom deployments can override their capacity in model config.
CONTEXT_WINDOWS = {
    "qwen3.8-max": 1_000_000,
    "qwen3.8-max-0902": 1_000_000,
    "qwen3.8-max-2026-09-02": 1_000_000,
    "glm-5.2": 1_000_000,
    "kimi-k2.7-code": 256_000,
}
FALLBACK_CONTEXT_TOKENS = 32_768


def model_context_window(provider):
    configured = getattr(provider, "context_window_tokens", None)
    if configured is not None:
        if type(configured) is not int or configured < 1024:
            raise ValueError("context_window_tokens must be an integer of at least 1024")
        return configured, "configured"
    known = CONTEXT_WINDOWS.get(getattr(provider, "model", None))
    return (known, "model_metadata") if known else (FALLBACK_CONTEXT_TOKENS, "fallback")


def estimate_message_tokens(*texts):
    """Estimate only, not provider usage: ASCII / 3, non-ASCII UTF-8 bytes.

    Non-ASCII is deliberately conservative for multilingual evidence. The spare
    context fraction covers tokenizer variation; no text is silently truncated.
    Includes an allowance for the two-message envelope and role/name fields.
    """
    ascii_chars = sum(sum(ord(char) < 128 for char in text) for text in texts)
    total_bytes = sum(len(text.encode("utf-8")) for text in texts)
    return math.ceil(ascii_chars / 3) + total_bytes - ascii_chars + 64
