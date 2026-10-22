import {toolChoices, selectedTools} from "./external-tools.mjs";
import { element } from "../markdown.mjs";

export class SessionSetupView {
  constructor(root, { api, templateChanged }) {
    this.root = root;
    this.api = api;
    this.templateChanged = templateChanged;
    this.get = (id) => root.querySelector(`#${id}`);
    this.recommendation = null;
    for (const id of ["create-objective", "create-model"])
      this.get(id).addEventListener("input", () => {
        this.abort?.abort();
        this.renderAcceptance(this.catalog?.acceptance);
        this.get("setup-recommend").disabled = !(
          this.catalog?.setup_recommendation && this.catalog.models.length
        );
        if (this.recommendation || this.abort)
          this.get("setup-status").textContent =
            "Task or model changed. Click Recommend with LLM to update the suggestions, or adjust the configuration manually.";
      });
    this.get("setup-recommend").onclick = () => this.recommend();
    this.get("create-knowledge").addEventListener("change", () => {
      const check = this.get("create-collections").querySelector(
        '[value="knowledge"]',
      );
      if (check)
        check.checked = this.get("create-knowledge").selectedOptions.length > 0;
    });
  }
  configure(catalog) {
    this.abort?.abort(); this.abort = null;
    this.get("setup-recommend").textContent = "Recommend with LLM";
    this.get("create-submit").disabled = false;
    this.catalog = catalog;
    const available = Boolean(
      catalog.setup_recommendation && catalog.models.length,
    );
    this.get("setup-recommend").disabled = !available;
    this.get("setup-status").textContent = available
      ? "Click Recommend with LLM to fill in the configuration below, then review it and create your session."
      : "Configure manually. LLM recommendations require a configured model and service support.";
    this.recommendation = null;
    const external = this.get('create-external-tools');
    if (external) {
      toolChoices(external, catalog.external_tools || []);
      external.onchange = () => {const check = this.get('create-collections').querySelector('[value="external_tools"]');
        if (check) check.checked = selectedTools(external).length > 0;};
    }
    this.renderAcceptance(catalog.acceptance);
  }
  loadTask(task, bases) {
    this.editing = true;
    this.get("create-spec").value = JSON.stringify(task.task_spec, null, 2);
    for (const key of ["objective", "title", "workspace", "model"]) {
      const input = this.get(`create-${key}`);
      const value = key === "model" ? (this.catalog?.model_aliases?.[task.model] || task.model) : task[key];
      if (key === "model" && value && ![...input.options].some(o => o.value === value)) {
        const option = element("option", "", `${value} (unavailable)`); option.value = value; input.append(option);
      }
      input.value = value || "";
    }
    const knowledge = this.get("create-knowledge"); knowledge.replaceChildren();
    const ids = task.knowledge_base_ids || [];
    for (const base of [...bases, ...ids.filter(id => !bases.some(b => b.id === id)).map(id => ({id, name: `${id} (unavailable)`}))]) {
      const option = element("option", "", base.name); option.value = base.id; option.selected = ids.includes(base.id); knowledge.append(option);
    }
    const selected = task.external_tool_ids || [];
    const tools = this.catalog.external_tools || [];
    const unavailable = selected.filter(id => !tools.some(t => t.id === id)).map(id => ({id, connector_name: "Unavailable", name: id, description: "Detach this tool or enable it before restarting.", kind: "mcp"}));
    toolChoices(this.get("create-external-tools"), [...tools, ...unavailable], selected);
    this.get("restart-plan-mode").value = task.plan_mode;
    for (const [key, value] of Object.entries(task.limits)) this.get(`restart-${key}`).value = value;
    const cap = task.task_spec?.context?.max_window_chars;
    const effective = Math.min(task.limits.max_window_chars, cap ?? task.limits.max_window_chars);
    this.get("restart-context-note").textContent = `Currently effective: ${effective.toLocaleString()} characters${cap < task.limits.max_window_chars ? " (limited by the context plugin)" : ""}. Save & restart applies the value above to both task and built-in context plugin limits.`;
    this.get("setup-status").textContent = "Review the current task configuration. Save & restart builds a fresh plan and acceptance checks; previous messages and results remain background.";
    this.get("setup-acceptance-status").textContent = "A new acceptance plan will be prepared after restart using the updated task and workspace.";
  }
  restartChanges(payload) {
    payload.model = this.get("create-model").value || null;
    payload.workspace = this.get("create-workspace").value.trim() || null;
    payload.plan_mode = this.get("restart-plan-mode").value;
    payload.limits = {};
    for (const input of this.get("restart-settings").querySelectorAll("input")) {
      const value = Number(input.value);
      if (!Number.isSafeInteger(value) || value <= 0) throw new Error("Execution limits must be positive integers.");
      payload.limits[input.id.replace("restart-", "")] = value;
    }
    if (!payload.objective) throw new Error("Enter the task to restart.");
    if (["bounded_context", "research_context"].includes(payload.task_spec?.context?.plugin))
      payload.task_spec.context.max_window_chars = payload.limits.max_window_chars;
  }
  renderAcceptance(acceptance, recommended = false) {
    this.get("setup-acceptance-status").textContent = (recommended
      ? "This recommended setup includes automatic task acceptance. " : "") +
      (this.editing ? "The acceptance plan is prepared after restart from the updated task and workspace."
        : "The acceptance plan is prepared after creation from the task and bound workspace. No workspace has been inspected or checks run yet.");
    const limits = acceptance?.defaults;
    this.get("setup-acceptance-budget").textContent = limits
      ? `Default limits: ${limits.max_repairs} repair rounds · ${limits.max_model_calls} planning/review calls · ${limits.max_seconds}s acceptance time · Input budget: ${limits.context_window_percent ?? 60}% of the verification model context (estimated tokens). Visual plans scale the default allowance for capture and repair; explicit limits are retained. Calls share the task budget.`
      : "Acceptance applies to every workflow and does not require an optional tool collection.";
  }
  fingerprint() {
    return JSON.stringify([
      this.get("create-objective").value.trim(),
      this.get("create-model").value,
    ]);
  }
  template(template) {
    this.currentTemplate = template;
    const custom = this.get("create-template").value === "custom";
    this.get("collections-field").hidden =
      custom || !this.catalog?.tool_collections;
    const defaults =
      template?.task_spec?.tools?.collections ||
      (template?.requires_workspace
        ? ["filesystem", "shell", "task_control"]
        : ["task_control"]);
    this.renderCollections(defaults);
    this.renderPlugins(template);
    this.updateWorkspace();
  }
  renderPlugins(template) {
    const custom = this.get("create-template").value === "custom";
    this.get("setup-plugins").hidden = custom || !this.catalog?.plugins;
    for (const kind of ["context", "workflow"]) {
      const select = this.get(`setup-${kind}`);
      select.replaceChildren();
      for (const plugin of this.catalog?.plugins?.[kind] || []) {
        const option = element("option", "", plugin.id === 'adaptive_dag' ? 'adaptive_dag · reuse context, independent read branches' : plugin.id);
        option.value = plugin.id;
        select.append(option);
      }
      const defaultId =
        template?.task_spec?.[kind]?.plugin ||
        (kind === "context" ? "bounded_context" : "legacy_planning");
      if ([...select.options].some((option) => option.value === defaultId))
        select.value = defaultId;
      else if (template?.task_spec?.[kind]?.plugin) {
        const option = element("option", "", `${defaultId} (unavailable)`); option.value = defaultId; select.append(option); select.value = defaultId;
      }
      this.get(`setup-${kind}-reason`).textContent = "";
      if (kind === 'workflow') select.onchange = () => {
        this.get('setup-workflow-reason').textContent = select.value === 'adaptive_dag'
          ? 'Reuses the working context. Only clearly independent work with stable inputs and registered read-only tools can use parallel agents. Writes and integration stay on the main lane.' : '';
      };
    }
  }
  renderCollections(selected, reasons = {}) {
    const root = this.get("create-collections");
    root.replaceChildren();
    const available = this.catalog?.tool_collections || [];
    for (const item of [...available, ...selected.filter(id => !available.some(c => c.id === id)).map(id => ({id, label: id, description: "Unavailable collection; detach or install before restarting."}))]) {
      const label = element("label", "setup-collection"),
        check = element("input");
      check.type = "checkbox";
      check.value = item.id;
      check.checked = selected.includes(item.id) || item.id === "task_control";
      check.disabled = item.id === "task_control";
      check.onchange = () => {
        if (item.id === 'external_tools' && !check.checked)
          this.get('create-external-tools')?.querySelectorAll('input').forEach(input => input.checked = false);
        this.updateWorkspace();
      };
      const description = element("span");
      description.append(
        element("strong", "", item.label),
        element("small", "muted", item.description),
      );
      if (reasons[item.id])
        description.append(element("small", "setup-reason", reasons[item.id]));
      label.append(check, description);
      root.append(label);
    }
  }
  collections() {
    return [
      ...this.get("create-collections").querySelectorAll("input:checked"),
    ].map((input) => input.value);
  }
  updateWorkspace() {
    const ids = this.collections();
    const visual = this.get('setup-visual-status');
    if (visual) {
      visual.hidden = !ids.includes('browser');
      const model = this.catalog?.visual_observation?.verification_model;
      visual.textContent = model
        ? `Browser screenshots and visual acceptance use ${model}. Local previews also need a workspace and Commands.`
        : 'Browser captures are available after setup. Select an image-capable Visual acceptance model in Settings for independent visual review.';
    }
    this.requiresWorkspace = Boolean(
      this.currentTemplate?.requires_workspace ||
      this.catalog?.tool_collections?.some(
        (item) => ids.includes(item.id) && item.requires_workspace,
      ),
    );
    if (this.get("create-template").value === "custom")
      this.requiresWorkspace = false;
    this.get("workspace-field").hidden = false;
    this.get("create-workspace").required = false; // Validate only after the recommendation has been reviewed.
  }
  async recommend() {
    if (!this.catalog?.setup_recommendation || !this.catalog.models.length)
      return;
    const objective = this.get("create-objective").value.trim();
    if (!objective) {
      this.get("create-error").textContent =
        "Enter an initial task to recommend a setup, or create an empty session manually.";
      return;
    }
    this.abort?.abort();
    const abort = new AbortController();
    this.abort = abort;
    const fingerprint = this.fingerprint(),
      api = this.api();
    this.get("setup-recommend").disabled = true;
    this.get("setup-recommend").textContent = "Recommending…";
    this.get("create-submit").disabled = true;
    this.get("create-error").textContent = "";
    this.get("setup-status").textContent =
      "Analyzing task and selecting available tools… No task is running yet.";
    try {
      const result = await api.json("/v1/session-setup/recommend", {
        body: { objective, model: this.get("create-model").value || null },
        signal: abort.signal,
      });
      if (
        abort.signal.aborted ||
        api !== this.api() ||
        fingerprint !== this.fingerprint() ||
        this.root.hidden
      )
        return;
      this.get("create-template").value = result.task_type;
      this.templateChanged();
      this.renderCollections(
        result.tool_collections,
        result.collection_reasons,
      );
      this.updateWorkspace();
      for (const kind of ["context", "workflow"]) {
        if (result.plugins?.[kind])
          this.get(`setup-${kind}`).value = result.plugins[kind];
        this.get(`setup-${kind}-reason`).textContent =
          result.plugin_reasons?.[kind] || "";
      }
      if (result.knowledge_bases) {
        const select = this.get("create-knowledge");
        select.replaceChildren();
        for (const base of result.knowledge_bases) {
          const option = element("option", "", base.name);
          option.value = base.id;
          option.selected =
            result.knowledge_base_ids?.includes(base.id) || false;
          select.append(option);
        }
        this.get("setup-knowledge-reasons").textContent = result.knowledge_bases
          .filter((base) => result.knowledge_base_ids?.includes(base.id))
          .map(
            (base) =>
              `${base.name}: ${result.knowledge_reasons?.[base.id] || "Recommended"}`,
          )
          .join("\n");
      }
      if (this.get('create-external-tools')) toolChoices(this.get('create-external-tools'), this.catalog.external_tools || [], result.external_tools || [], result.external_tool_reasons || {});
      this.recommendation = { fingerprint, result };
      this.renderAcceptance(result.acceptance || this.catalog.acceptance, true);
      this.get("setup-status").textContent =
        `${result.rationale}\nReview the configuration below before ${this.editing ? "saving and restarting" : "creating"}. Setup model: ${result.model} · ${result.usage.total_tokens || 0} reported tokens.`;
      if (
        result.tool_collections.includes("knowledge") &&
        !this.get("create-knowledge").selectedOptions.length
      )
        this.get("setup-status").textContent +=
          "\nSelect at least one knowledge base, or uncheck Knowledge bases.";
      this.get("create-template").focus();
    } catch (error) {
      if (abort.signal.aborted || api !== this.api()) return;
      this.get("create-error").textContent =
        `${error.message} You can retry or configure manually.`;
      this.get("setup-status").textContent =
        this.editing ? "Recommendation did not complete. The task has not changed." : "Recommendation did not complete. No session was created.";
    } finally {
      if (this.abort === abort) {
        this.get("setup-recommend").disabled = false;
        this.get("setup-recommend").textContent = "Recommend with LLM";
        this.get("create-submit").disabled = false;
      }
    }
  }
  apply(payload, template) {
    payload.external_tool_ids = this.get('create-external-tools') ? selectedTools(this.get('create-external-tools')) : [];
    if (this.collections().includes('external_tools') && !payload.external_tool_ids.length)
      throw new Error('Select external tools, or uncheck External tools.');
    const editableCollections =
      this.get("create-template").value !== "custom" &&
      Boolean(this.catalog?.tool_collections);
    const workspace = this.get("create-workspace").value.trim();
    if (!editableCollections && !workspace && !this.requiresWorkspace) return;
    if (!editableCollections && !payload.task_spec && !template?.task_spec) {
      if (!workspace && this.requiresWorkspace)
        throw new Error("Enter an existing workspace directory.");
      if (workspace) payload.workspace = workspace;
      return;
    }
    const collections = editableCollections ? this.collections() : [];
    if (collections.includes("knowledge") && !payload.knowledge_base_ids.length)
      throw new Error("Select a knowledge base, or uncheck Knowledge bases.");
    let spec = structuredClone(
      payload.task_spec ||
        template?.task_spec || {
          session_environment: { plugin: "session" },
          context: { plugin: "bounded_context" },
          workflow: { plugin: "legacy_planning", mode: "auto" },
        },
    );
    if (editableCollections) {
      spec.tools = { ...spec.tools, collections };
      for (const kind of ["context", "workflow"]) {
        const plugin = this.get(`setup-${kind}`).value;
        if (plugin)
          spec[kind] = spec[kind]?.plugin === plugin ? spec[kind] : { plugin };
      }
    }
    if (this.requiresWorkspace || workspace) {
      if (!workspace)
        throw new Error(
          "Choose an existing workspace directory for the selected file or command tools.",
        );
      payload.workspace = workspace;
      spec.session_environment ||= { plugin: "session" };
      const resources = spec.session_environment.resources || [];
      const resourceId = spec.execution_runtime?.workspace_resource || "workspace";
      const existing = resources.find(r => r.id === resourceId);
      spec.session_environment.resources = [
        ...resources.filter((r) => r.id !== resourceId),
        {
          id: resourceId,
          kind: "directory",
          uri: workspace,
          access: existing?.access || "read-write",
        },
      ];
      spec.execution_runtime = {
        ...spec.execution_runtime,
        plugin: spec.execution_runtime?.plugin || "native_os",
        workspace_resource: resourceId,
      };
    } else if (this.editing && spec.execution_runtime?.workspace_resource) {
      spec.session_environment.resources = (spec.session_environment.resources || []).filter(r => r.id !== spec.execution_runtime.workspace_resource);
      delete spec.execution_runtime.workspace_resource;
    }
    payload.task_spec = spec;
  }
}
