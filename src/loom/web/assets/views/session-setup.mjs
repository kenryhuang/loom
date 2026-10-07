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
    this.catalog = catalog;
    const available = Boolean(
      catalog.setup_recommendation && catalog.models.length,
    );
    this.get("setup-recommend").disabled = !available;
    this.get("setup-status").textContent = available
      ? "Click Recommend with LLM to fill in the configuration below, then review it and create your session."
      : "Configure manually. LLM recommendations require a configured model and service support.";
    this.recommendation = null;
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
        const option = element("option", "", plugin.id);
        option.value = plugin.id;
        select.append(option);
      }
      const defaultId =
        template?.task_spec?.[kind]?.plugin ||
        (kind === "context" ? "bounded_context" : "legacy_planning");
      if ([...select.options].some((option) => option.value === defaultId))
        select.value = defaultId;
      this.get(`setup-${kind}-reason`).textContent = "";
    }
  }
  renderCollections(selected, reasons = {}) {
    const root = this.get("create-collections");
    root.replaceChildren();
    for (const item of this.catalog?.tool_collections || []) {
      const label = element("label", "setup-collection"),
        check = element("input");
      check.type = "checkbox";
      check.value = item.id;
      check.checked = selected.includes(item.id) || item.id === "task_control";
      check.disabled = item.id === "task_control";
      check.onchange = () => this.updateWorkspace();
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
      this.recommendation = { fingerprint, result };
      this.get("setup-status").textContent =
        `${result.rationale}\nReview the configuration below and make any changes before creating. Setup model: ${result.model} · ${result.usage.total_tokens || 0} reported tokens.`;
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
        "Recommendation did not complete. No session was created.";
    } finally {
      if (this.abort === abort) {
        this.get("setup-recommend").disabled = false;
        this.get("setup-recommend").textContent = "Recommend with LLM";
        this.get("create-submit").disabled = false;
      }
    }
  }
  apply(payload, template) {
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
      spec.session_environment.resources = [
        ...resources.filter((r) => r.id !== "workspace"),
        {
          id: "workspace",
          kind: "directory",
          uri: workspace,
          access: "read-write",
        },
      ];
      spec.execution_runtime = {
        ...spec.execution_runtime,
        plugin: spec.execution_runtime?.plugin || "native_os",
        workspace_resource: "workspace",
      };
    }
    payload.task_spec = spec;
  }
}
