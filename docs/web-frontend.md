# Browser frontend for persistent sessions

Start the service as usual; model configuration and environment variables belong
to this process:

```bash
LOOM_ENV_FILE=/absolute/path/.env uv run loom serve \
  --data-dir .loom/service --config /absolute/path/config.yaml
```

Open the printed Web frontend URL (normally `http://127.0.0.1:8765/web/`).
Paste the contents of the printed credential file, or choose that file in the
connection dialog. Credentials are held in memory for this tab and sent through
the Authorization header; reload requires connecting again. The service retains
its loopback binding and bearer authentication. Closing a tab or disconnecting
does not pause execution.

The page lists sessions and supports creation with General, Research, Coding,
or a custom JSON task specification. Templates and model aliases come from the
authenticated catalog; model secrets remain owned by the service. Coding requires
a workspace. Custom specifications use the service's installed plugin registry.
There is no browser API for uploading executable plugins or changing server
credentials.

Within a session, send guidance, answer a pending question, redirect a
clarification, pause/resume/stop execution, complete/reopen the task, or change
its token budget. Recovery questions require the same resolution JSON as the TUI
and cannot be redirected. Commands use the existing validation and safe-boundary
application rules; a command receipt means accepted, while events show application.
Failed executions display their error code and message above the conversation.
New guidance retries a failed execution from its safe checkpoint after cleanup.
An exhausted response that omitted a required routing tool is retried with a
fresh model request, retaining tool observations and token usage. Paused tasks
still require Resume; pending recovery questions remain gated by verification.

After a completed round, a new message becomes the next round's objective.
The worker consumes queued initial messages before assembling tools, completion
checks and workflow nodes. Each round starts with fresh execution observations,
decisions and workflow state. Recent user/assistant exchanges provide bounded
background for follow-up references; the full earlier conversation is retained
as a session history artifact and can be read on demand. Historical requests
are marked as already answered, and final replies focus on the current request.
Guidance received during execution still updates the unfinished current round.
Pause/resume and failed-run retries preserve the current checkpoint. Result
fallback and completion checks exclude decisions and finish observations from
previous runs.

The conversation groups each task, its execution process and its result into
one block, with horizontal separators between blocks. The header shows the
session title and status; guidance appears only in the conversation rather than
repeating the accumulated objective above it. Expand all / Fold all controls
processes, event details and results together, retaining the setting for new
events and through history loading or reconnects within the selected session.
The process is collapsed by default, with only its latest visible event in the
sticky summary. Older failures remain in the details without a persistent
failure count or warning color in the summary. Streaming chunks update a
single model row per call, and tool start/completion update a single tool row.
Model requests, stream boundaries, reasoning, output and completion/failure
update the same row; its details retain the request and response. Model-generated
tool-call names and argument chunks do not add execution rows: actual execution
is shown by tool events, while proposed calls remain in the model response.
Normal operation start/completion bookkeeping is hidden; uncertain effects
remain visible as recovery warnings. These display rules apply to both live
events and restored history; original records remain in the raw history API.
Task/session changes, run and step lifecycle events, token/time budgets and
command receipts update the sidebar and controls without adding detail rows.
Session information shows task/run state, the current objective, reasons and
the time limit; the budget and outputs panels update as changes arrive. Run
transitions still maintain round boundaries and survive history loading;
errors and recovery warnings remain visible separately. Resuming the same run
retains its token usage and renewed time window.
Consecutive text deltas are also coalesced in the display history window.
An execution-round index restores Process sections for older tasks even when
their detailed events fall outside the recent history page. Historical failures
remain visible after recovery or completion. Expanding a Process
loads its own paginated execution history; the button inside that section loads
earlier records for that round. These pages omit individual token chunks and
retain model requests/replies, tool calls and lifecycle records. Raw events are
still retained by the raw history API; Earlier events uses the same compact
renderers as live events. Completed rounds that reused a run ID
have separate Process sections, while pause/resume attempts stay in one round.
Thought and tool rows can be expanded to see their full detail; large tool
artifacts load on demand. Final assistant results are expanded by default.
JSON report envelopes are extracted and rendered as Markdown; arbitrary JSON is
formatted as JSON. Markdown supports headings, lists, links, code blocks, tables
and blockquotes. Raw HTML is displayed as text. Session information shows the
workspace on its second row with alternating row backgrounds. Workflow and
output panels support general tasks as well as coding plans.
On desktop, the session list, conversation and information sidebar occupy
separate columns. The right sidebar starts at the top of the page and keeps its
header in place while its panels scroll independently of the conversation.
Narrow mobile screens stack the columns, with bounded scrolling areas.

## Architecture

The frontend uses browser-native ES modules, HTML and CSS, packaged inside the
Python wheel. It needs no Node runtime, bundler, external CDN or separate server.
All requests use the same origin as `loom serve`. `--no-web` disables assets and
the frontend catalog while preserving session APIs.

| Layer | Module | Responsibility |
| --- | --- | --- |
| Asset host and catalog | `loom.web.frontend.WebFrontend` | Packaged resources, templates, safe model labels and command descriptors |
| Transport | `assets/api.mjs` | Bearer HTTP, command IDs, authenticated streaming fetch and SSE parsing |
| Projection | `assets/state.mjs` | Session sequence, Unicode text offsets, bounded stream tails and display state |
| Lifecycle | `assets/controller.mjs` | Snapshot/history, subscriptions, reconnect, input routing and cancellation |
| Rendering | `assets/renderers.mjs`, `markdown.mjs` | Event descriptors, report extraction and safe DOM rendering |
| Views | `assets/views/` | Session list, event feed, extensible sidebar panels |
| Composition | `assets/app.mjs` | Connects the modules to the HTML shell and UI actions |

The browser never executes task tools or owns workflow decisions. It uses the
same `/v1/sessions` commands, snapshots, history, SSE and session-scoped artifacts
as the terminal client. `/v1/web/catalog` adds UI descriptors, not an execution
API. The unauthenticated routes expose only static assets.

The lifecycle controller loads a snapshot and history, filters history against
the snapshot cursor, then subscribes after that cursor. Duplicate events are
ignored. Missing sequence numbers or text offsets trigger a fresh snapshot.
Transport failures reconnect with bounded backoff. Session changes abort the
previous subscription; generation guards reject late responses. SSE uses
streaming `fetch` so credentials can stay in headers rather than URLs.

The event feed retains up to 1,000 events; the history button pages backwards
through durable service history. Display compaction preserves snapshot messages
and current reasoning. This display limit does not affect stored events.

## Extension points

### Event renderers

Register a renderer and pass it to `FeedView` in the composition module.
Renderers return data descriptors; the feed owns disclosure, grouping and lazy
artifact loading. Higher priority entries can override a built-in renderer.

```javascript
import { builtinRenderers } from "./renderers.mjs";
import { FeedView } from "./views/feed.mjs";

const renderers = builtinRenderers().register(
  "research-source",
  event => event.type === "research.source.checked",
  event => ({
    key: `source:${event.payload.url}`,
    summary: `Source verified · ${event.payload.url}`,
    details: event.payload,
    artifact: event.payload.artifact,
  }),
  10,
);
const feed = new FeedView(document.getElementById("event-feed"), {
  renderers,
  loadArtifact: digest => controller.api.artifact(controller.selectedId, digest),
});
```

Unknown event types already have a generic expandable JSON renderer. New domain
events therefore remain visible before a dedicated renderer is added.

### Information panels

`PanelRegistry.register(id, factory)` adds or replaces a panel. A factory receives
the shared actions and returns `{ element, update(snapshot) }`. This separates
domain UI from the session lifecycle and transport.

```javascript
import { builtinPanels, PanelsView } from "./views/panels.mjs";
import { element } from "./markdown.mjs";

const panels = builtinPanels().register("domain-summary", actions => {
  const root = element("section", "panel");
  return {
    element: root,
    update(snapshot) {
      root.textContent = `Outputs: ${(snapshot.output_artifacts || []).length}`;
    },
  };
});
new PanelsView(document.getElementById("session-panels"), panels, actions);
```

### Templates and alternative frontends

Embed a replaceable frontend through `ServiceHTTPServer(web_frontend=...)`.
The host interface is `asset(path) -> (bytes, content_type) | None` and
`catalog(service) -> JSON object`. `WebFrontend` implements both; a subclass can
override them. `package` names an installed Python package containing an `assets`
directory, served under `/web/assets/`; its `index.html` is served at `/web/`.
Asset traversal and unsupported file types are rejected. Custom pages should
use same-origin modules and styles to match the service's Content Security Policy.

```python
from loom.service.api import ServiceHTTPServer
from loom.web.frontend import WebFrontend, builtin_templates

frontend = WebFrontend(
    package="my_extension.web",
    templates=[*builtin_templates(), {
        "id": "analysis",
        "label": "Analysis",
        "description": "Analyze sources with the installed domain tools",
        "task_spec": {
            "tools": {"collections": ["my_analysis_tools", "task_control"]},
            "workflow": {"plugin": "dynamic"},
        },
    }],
)
server = ServiceHTTPServer(("127.0.0.1", 8765), service, token, web_frontend=frontend)
```

Register task plugins in the service/worker assembly as documented by the
general task runtime; frontend templates only select installed capabilities.
Keep business validation in that layer. Presentation-only event types need no
protocol projection change; new persistent state fields require updating the
projection and its tests.

## Verification

Python API tests cover public assets, authenticated catalog/session data, path
traversal rejection, disabling the frontend, custom catalogs and model-secret
exclusion. Build a wheel to verify that assets remain installed:

```bash
uv run pytest -q tests/service/test_api.py tests/test_package_structure.py
uv build --wheel
```

Frontend tests use Node's built-in test runner. `jsdom` is a development-only
dependency for DOM interaction tests and is not part of the shipped frontend:

```bash
npm --prefix tests/web ci
npm --prefix tests/web test
```

The app test starts a temporary service with a deterministic provider and tests
connection, input answers, session creation/switching, task controls, token
budgets, JSON/Markdown results, artifacts and disconnect. DOM dialog methods are
stubbed because jsdom does not implement native dialogs; this does not replace
real-browser layout and native-dialog checks.

For manual browser validation, run the isolated fixture and use its printed URL
and temporary credential. It does not use model credentials or existing service
data:

```bash
PYTHONPATH=src:. uv run python tests/web/smoke.py
```

Stop it with Ctrl+C to remove its temporary data.
