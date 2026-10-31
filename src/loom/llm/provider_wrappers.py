"""Adapt model configuration without dropping accounting or admission wrappers."""


def unwrap_provider(provider):
    inner = getattr(provider, "wrapped_provider", None)
    return unwrap_provider(inner) if inner is not None else provider


def map_provider(provider, transform):
    """Wrappers expose wrapped_provider and with_provider to retain their policy."""
    inner = getattr(provider, "wrapped_provider", None)
    if inner is not None:
        return provider.with_provider(map_provider(inner, transform))
    return transform(provider)
