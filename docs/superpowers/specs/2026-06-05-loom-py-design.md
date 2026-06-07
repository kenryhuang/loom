# Loom Python Port Design Notes

This document captures design decisions for the Loom Python port.

## Guiding principles

- Keep compatibility with Loom TypeScript semantics where it matters for traces and composition, but adopt Python idioms for data modeling and async control flow.
- Keep the public API small and well-typed using `dataclasses` and clear factory functions.
- Favor explicitness over magic; prefer immutable dataclasses for trace artifacts and context layers.

## High-level Architecture

- `core`: serializable contracts and context helpers (IDs, Results, Errors, Context layer manipulation).
- `runtime`: loop execution primitives (create, step, done, cancellation tokens, pool executor).
- `observability`: in-memory and JSONL trace stores, readers, sampling and archive manifest generation.
- `llm`: prompt builder, token budgeting, OpenAI provider adapter, structured decision parsing.
- `composition`: chain, nest, fork, helper utilities to assemble loops into higher-order programs.
- `evolution`: versioned registries, mutation policies, shadow evaluation, strategy orchestration.

## Implementation notes

- Use `pathlib.Path` for filesystem operations and JSONL stores.
- Tests use `pytest-asyncio` for async tests and standard `pytest` for sync.
- Keep the initial implementation modular but compact: one `__init__.py` per package to start, splitting files if they grow.

## Operational concerns

- Document the reproducible development steps in `README.md`.
- Ship `pyproject.toml` with `pytest` extras to facilitate local testing.


