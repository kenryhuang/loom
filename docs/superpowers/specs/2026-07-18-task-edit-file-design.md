# Loom Task `edit_file` Tool Design

## Status

Approved design for adding a precise existing-file editing tool to
`loom.tasks`.

## Context

Generic Loom tasks currently expose `read_file`, `write_file`,
`shell_execute`, and `finish`. `write_file` is appropriate for creating a file
or replacing its complete contents, but it makes small source edits expensive
and error-prone because the model must reproduce the whole file.

The reference implementation is Pi's edit tool under
`~/workspace/pi/packages/coding-agent/src/core/tools/edit.ts` and
`edit-diff.ts`. Loom will reuse its useful contract ideas—batched replacements,
original-file matching, overlap rejection, newline/BOM preservation, and diff
feedback—while deliberately omitting fuzzy text matching.

## Goals

- Add an `edit_file` tool to generic Loom task runs.
- Make one or more exact text replacements in an existing UTF-8 file.
- Allow a caller to select one repeated string by a 1-based occurrence index.
- Validate a complete edit batch before changing the file.
- Keep every path inside the configured task workspace.
- Preserve the file's UTF-8 BOM, newline convention, and permission bits.
- Return compact evidence describing the applied edit.
- Use only the Python standard library.

## Non-Goals

- Creating files; callers continue to use `write_file`.
- Fuzzy whitespace, punctuation, Unicode, or semantic matching.
- Line-number, byte-offset, regular-expression, or syntax-tree edits.
- Patching binary or non-UTF-8 files.
- Replacing `write_file` or adding a general mutation framework.
- Providing cross-process locking for multiple Loom processes editing the same
  workspace. This has the same concurrency boundary as the existing file tools.

## Selected Architecture

Add a focused `src/loom/tasks/edit_file.py` module. It owns input validation,
match planning, overlap detection, replacement application, unified diff
generation, and atomic file replacement. Its matching and replacement planner
is expressed as pure functions so behavior can be tested without a runtime or
model.

`src/loom/tasks/tools.py` continues to own task-bound handler construction and
workspace path resolution. It registers an async `edit_file` handler that
delegates to the new module and converts its result into a Loom `Observation`.

`src/loom/tasks/runner.py` adds the corresponding `ToolRef` and JSON schema.
No changes are required in `TaskRequest`, profiles, runtime execution, or the
permission model.

Alternatives rejected:

1. Keeping all logic in `tasks/tools.py` would minimize file count but mix text
   matching, filesystem mutation, shell execution, and tool registration.
2. A generic patch or mutation subsystem would exceed the scope of one task
   tool and risk coupling runtime editing to Evolution's separate mutation
   concepts.

## Tool Contract

The tool name is `edit_file`. Input fields use Loom's snake_case convention:

```json
{
  "path": "src/app.py",
  "edits": [
    {
      "old_text": "status = \"pending\"",
      "new_text": "status = \"ready\"",
      "occurrence": 2
    }
  ]
}
```

The JSON schema requires `path` and a non-empty `edits` array. Every edit
requires string `old_text` and `new_text`. `occurrence` is optional and, when
present, must be an integer of at least 1. Unknown fields are rejected at both
the top level and edit-entry level.

### Exact Match Semantics

- Matching is case-sensitive and character-exact after newline normalization.
- The file and edit strings normalize CRLF and bare CR to LF before matching.
  No other character or whitespace normalization occurs.
- Occurrences are non-overlapping matches scanned from left to right.
- If `occurrence` is omitted, `old_text` must have exactly one occurrence.
- If `occurrence` is provided, it selects that 1-based occurrence; an index
  larger than the number of matches is invalid.
- `old_text` cannot be empty. `new_text` may be empty to delete text.
- All entries match the same original normalized content, not content produced
  by earlier entries in the batch.
- Target ranges must be disjoint. Duplicate, nested, or partially overlapping
  targets invalidate the complete batch.
- A batch whose final content equals the original content is invalid.

## Execution Flow

1. Resolve `path` through the existing workspace containment check.
2. Require an existing regular file and read its bytes exactly once.
3. Decode strict UTF-8. Separate a leading UTF-8 BOM when present.
4. Detect the original newline convention and normalize text to LF.
5. Parse and validate every edit, enumerate exact match locations, select the
   requested ranges, and reject overlaps.
6. Apply replacements from highest offset to lowest so original offsets remain
   stable.
7. Restore the original newline convention and BOM.
8. Generate a unified diff and first changed line from normalized before/after
   content.
9. Write a temporary file in the target directory, copy the target permission
   bits, flush and fsync it, and replace the target with `os.replace`.
10. Remove an uncommitted temporary file on every failure path.

All semantic validation completes before the temporary file is created. An
invalid entry therefore cannot partially apply earlier entries. Same-directory
`os.replace` prevents readers from observing a torn file during the final
commit.

## Result Contract

On success, the handler returns an `Observation` with tool ID `edit_file` and a
value shaped as:

```json
{
  "path": "src/app.py",
  "replacements": 2,
  "bytes_written": 1432,
  "first_changed_line": 18,
  "diff": "--- src/app.py\n+++ src/app.py\n@@ ...",
  "diff_truncated": false
}
```

The path is workspace-relative. The unified diff uses three context lines. Its
returned UTF-8 representation is capped at 20,000 bytes; truncation preserves a
valid Unicode string and sets `diff_truncated` to true. The edit itself is never
truncated.

## Error Model

Return `VALIDATION_FAILED` for:

- missing or malformed `path` or `edits` input;
- an empty `old_text`;
- a missing, ambiguous, or out-of-range match;
- duplicate or overlapping target ranges;
- a no-op result;
- a path outside the workspace.

Return `TOOL_FAILED` for:

- a missing or non-regular target file;
- UTF-8 decode failure;
- permission, read, temporary-write, fsync, chmod, or replace failure.

Errors include the workspace-relative path when safely available, the edit
index for semantic failures, and occurrence counts where useful. They do not
include full file contents.

## Tool Description

The ToolRef description tells the model to use `edit_file` for precise changes
to existing text files and `write_file` only for new files or intentional full
replacement. It explains that omitted `occurrence` requires a unique match and
that every edit is matched against the original file.

Adding the tool does not grant a new capability: it operates under the same
read-write workspace resource and path boundary as `write_file`.

## Test Strategy

### Pure Planner Tests

- one unique exact replacement;
- first, middle, and last repeated match selected by `occurrence`;
- omitted occurrence rejects repeated text;
- occurrence zero, negative, wrong type, and out of range;
- multiple disjoint edits applied from original-file offsets;
- later edits do not match text introduced by earlier edits;
- empty old text, not found, overlapping targets, duplicate targets, and no-op;
- deleting text with empty new text;
- LF-normalized matching against CRLF input.

### Filesystem and Handler Tests

- edits an existing workspace-relative file;
- rejects workspace traversal and missing/non-regular targets;
- rejects invalid UTF-8;
- preserves LF, CRLF, UTF-8 BOM, and permission bits;
- leaves the original byte-for-byte unchanged when any edit fails;
- cleans temporary files after a failed write/replace;
- returns replacement count, byte count, first changed line, unified diff, and
  deterministic diff truncation metadata.

### Integration Tests

- `make_task_context` exposes the `edit_file` ToolRef and exact JSON schema;
- `make_task_tools` registers the handler;
- the generic task loop can call `edit_file` and observe the resulting file;
- trace records identify the completed tool as `edit_file`;
- real-project smoke expectations include the new built-in tool without
  changing existing read, write, shell, or finish behavior.

## Acceptance Criteria

- A task can make one or multiple precise edits to an existing UTF-8 file.
- Repeated text can be targeted only through a valid 1-based `occurrence`.
- An unspecified repeated match never changes a file.
- A failed batch leaves the target unchanged.
- Paths cannot escape the task workspace.
- CRLF/LF, BOM, and permission behavior is covered by passing tests.
- The success Observation provides bounded, traceable edit evidence.
- Existing task, integration, lint, and format checks remain green.
