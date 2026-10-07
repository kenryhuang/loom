# Recommended session setup

The new-session main panel supports a review step before creating or starting a session:

1. Enter the initial task. You can configure the session manually or request suggestions.
2. Click **Recommend with LLM**. One tool-free model request selects a task type, registered tool collections, context/workflow plugins, and relevant existing knowledge bases. It returns a rationale and reasons for the selected capabilities and bindings.
3. Review the type, add or remove collections, supply a workspace if needed, and adjust the recommended plugin and knowledge base selections.
4. Click **Create session** to save the reviewed configuration and start the initial task.

Changing the description or selected model marks the previous recommendation as outdated without blocking creation. Manual edits to tool collections and task type remain valid after a recommendation. Clicking **Recommend with LLM** again replaces the suggestions. **Create session** always submits the current form and never triggers a recommendation. Empty sessions remain supported without a recommendation. Recommendation failures preserve the form and offer retry/manual configuration; they do not create sessions.

Task types remain the service's existing templates (General, Research, Coding by default). A type supplies workflow, context and output defaults; collections can be adjusted independently. The Custom specification option retains advanced JSON configuration. File or command tools require an explicit existing directory; paths are never inferred or approved by the model. Files currently includes read/create/edit capabilities; the collection description shows this even if the task only requests reading. Knowledge retrieval is scoped to explicitly selected bases.

The capability catalog comes from the installed plugin registry without instantiating tools. External tools are currently an empty catalog and an unavailable UI section; the model cannot recommend imaginary external integrations. Unknown collection IDs, task types, external tool selections or malformed recommendations are rejected.

## API

`GET /v1/web/catalog` adds `tool_collections`, `plugins`, `external_tools` and `setup_recommendation` alongside existing templates and model choices.

Authenticated `POST /v1/session-setup/recommend` accepts:

```json
{"objective":"Read the project's indexing implementation and explain whether it uses embeddings","model":"main"}
```

`model` is optional and defaults to the service model. The response contains `task_type`, `tool_collections`, `plugins`, `external_tools`, `rationale`, `collection_reasons`, `plugins`, `plugin_reasons`, `knowledge_base_ids`, `knowledge_reasons`, the available `knowledge_bases`, model ID, elapsed seconds and reported token usage. This is a draft recommendation, not a created session or executable task specification. Clients still construct and submit the reviewed `task_spec` through normal session creation, which performs the usual resource/plugin validation.

Recommendations send the description, capability catalog, and knowledge base metadata (names, descriptions, and up to 50 document names per base; not document contents) to the selected configured model, make no tool calls, and have a 60-second timeout, a 4,096-token response cap, and at most two concurrent requests per service process. They do not read project files, import tools, install packages, bind paths, or create sessions. Configured `enable_thinking` is disabled and configured `reasoning_effort` is lowered for this short classification call; task model settings remain unchanged. Their model usage is returned separately and is not part of a task's usage. Leaving the creation panel discards/aborts the client request; the server call may continue until completion or timeout. No recommendation is cached or persisted automatically.

After upgrading an already-running service, restart it when execution/evaluation is idle to expose the new catalog and endpoint. Older services keep manual creation available rather than offering a nonfunctional recommendation button.
