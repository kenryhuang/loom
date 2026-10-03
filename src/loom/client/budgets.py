"""Human-readable token counts for CLI and interactive input."""

import re


def parse_token_budget(value):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KM]?)", value.strip().upper().replace("_", ""))
    if not match:
        raise ValueError("Token budget must be a positive count, such as 10000000, 10M or 500K")
    whole, _, fraction = match[1].partition(".")
    count, remainder = divmod(int(whole + fraction) * {"": 1, "K": 1000, "M": 1000000}[match[2]], 10 ** len(fraction))
    if count <= 0 or remainder:
        raise ValueError("Token budget must resolve to a positive whole number of tokens")
    return count
