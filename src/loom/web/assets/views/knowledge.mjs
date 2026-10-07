import { element } from "../markdown.mjs";
import { renderKnowledgeGraph } from "./knowledge-graph.mjs";
import { KnowledgeProgress } from "./knowledge-progress.mjs";

function field(label, input) {
  const node = element("label", "knowledge-field");
  node.append(element("span", "", label), input);
  return node;
}
function input(placeholder, type = "text") {
  const node = element("input");
  node.type = type;
  node.placeholder = placeholder;
  return node;
}
function button(label) {
  const node = element("button", "quiet", label);
  node.type = "button";
  return node;
}

export class KnowledgeView {
  constructor({
    api,
    state,
    bind,
    root,
    selected = () => {},
    changed = () => {},
  }) {
    this.root = root;
    this.onSelected = selected;
    this.api = api;
    this.state = state;
    this.bind = bind;
    this.changed = changed;
    this.dialog = root || element("dialog", "knowledge-dialog");
    this.dialog.setAttribute("aria-label", "Knowledge bases");
    if (!root) document.body.append(this.dialog);
    this.dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      this.close();
    });
  }
  close() {
    this.abort?.abort();
    clearTimeout(this.timer);
    if (!this.root && this.dialog.open) this.dialog.close();
  }
  async open(bindSession = false, { baseId, create = false } = {}) {
    this.close();
    this.abort = new AbortController();
    this.client = this.api();
    if (!this.client) return;
    this.session = bindSession ? this.state() : null;
    this.selected = new Set(this.session?.task.knowledge_base_ids || []);
    this.dialog.replaceChildren();
    const heading = element("div", "dialog-heading");
    const close = button("Close");
    close.onclick = () => this.close();
    heading.append(element("h2", "", "Knowledge bases"));
    if (!this.root) heading.append(close);
    this.dialog.append(heading);
    this.notice = element("p", "muted");
    this.notice.setAttribute("role", "status");
    this.dialog.append(this.notice);
    this.content = element("div");
    this.dialog.append(this.content);
    if (!this.root) this.dialog.showModal();
    try {
      await this.refresh(baseId ?? this.baseId);
      if (create) {
        this.content.querySelector(".knowledge-create").open = true;
        this.content.querySelector(".knowledge-create input").focus();
      }
    } catch (error) {
      this.error(error);
    }
  }
  error(error) {
    if (error.name !== "AbortError") this.notice.textContent = error.message;
  }
  async request(path = "", body) {
    const abort = this.abort;
    const result = await this.client.json(`/v1/knowledge-bases${path}`, {
      signal: abort.signal,
      ...(body === undefined ? {} : { body }),
    });
    if (abort !== this.abort || abort.signal.aborted)
      throw new DOMException("Closed", "AbortError");
    if (body !== undefined) this.changed();
    return result;
  }
  async refresh(selectedId = this.baseId) {
    const catalog = await this.request();
    if (this.abort.signal.aborted) return;
    this.content.replaceChildren();
    if (this.session) this.renderBinding(catalog.knowledge_bases);
    this.renderCreate(catalog);
    const select = element("select");
    this.baseSelect = select;
    select.setAttribute("aria-label", "Knowledge base");
    for (const base of catalog.knowledge_bases) {
      const option = element(
        "option",
        "",
        `${base.name} · ${base.engine} · ${base.documents.length} documents`,
      );
      option.value = base.id;
      select.append(option);
    }
    if (!this.root) this.content.append(field("Manage knowledge base", select));
    this.basePanel = element("section", "knowledge-base-panel");
    this.content.append(this.basePanel);
    this.baseId = catalog.knowledge_bases.some((base) => base.id === selectedId)
      ? selectedId
      : catalog.knowledge_bases[0]?.id;
    select.value = this.baseId || "";
    this.onSelected(this.baseId);
    select.onchange = () => {
      this.baseId = select.value;
      this.renderBase().catch((error) => this.error(error));
    };
    if (this.baseId) await this.renderBase();
    else
      this.basePanel.textContent =
        "Create a knowledge base, then import text or Markdown documents.";
  }
  renderBinding(bases) {
    const section = element("section", "knowledge-binding");
    section.append(
      element("h3", "", `Session · ${this.session.title}`),
      element(
        "p",
        "muted",
        "The model decides when to search attached knowledge bases. Change attachments between tasks.",
      ),
    );
    const editable =
      ["idle", "completed"].includes(this.session.task.state) &&
      (!this.session.run ||
        ["completed", "stopped"].includes(this.session.run.state));
    for (const base of bases) {
      const check = input("", "checkbox");
      check.value = base.id;
      check.checked = this.selected.has(base.id);
      check.disabled = !editable;
      check.onchange = () =>
        check.checked
          ? this.selected.add(base.id)
          : this.selected.delete(base.id);
      const label = element("label", "knowledge-choice");
      label.append(check, document.createTextNode(base.name));
      section.append(label);
    }
    const save = button("Save session knowledge bases");
    save.disabled = !editable;
    save.onclick = async () => {
      save.disabled = true;
      try {
        await this.bind(
          [...this.selected],
          this.session.session_id,
          this.session.task.revision,
        );
        this.notice.textContent =
          "Knowledge bases saved. Retrieval tools will be available on the next task.";
      } catch (error) {
        this.error(error);
        save.disabled = !editable;
      }
    };
    section.append(save);
    this.content.append(section);
  }
  renderCreate(catalog) {
    const details = element("details", "knowledge-create");
    details.append(element("summary", "", "Create a knowledge base"));
    const form = element("form", "knowledge-form"),
      name = input("Knowledge base name"),
      description = input("What information does this contain?");
    name.required = true;
    const engine = element("select");
    for (const [id, label] of [
      ["sqlite_fts", "Local keyword search · no embedding required"],
      ["sqlite_hybrid", "Local hybrid search · embedding + keywords"],
      ["yakdb_local", "YakDB local · PDF / Office / text"],
      ["lightrag_local", "LightRAG local · knowledge graph + website sources"],
    ]) {
      const option = element("option", "", label);
      option.value = id;
      if (!catalog.engines.includes(id)) {
        option.disabled = true;
        option.textContent += " · service update required";
      }
      engine.append(option);
    }
    const profile = element("select");
    profile.append(element("option", "", "Choose embedding profile"));
    profile.firstChild.value = "";
    for (const item of catalog.embedding_profiles) {
      const option = element("option", "", `${item.name} · ${item.model}`);
      option.value = item.id;
      profile.append(option);
    }
    const indexingModel = element("select");
    indexingModel.append(element("option", "", "Choose indexing model"));
    indexingModel.firstChild.value = "";
    for (const item of catalog.models || []) {
      const option = element("option", "", item.label);
      option.value = item.id;
      indexingModel.append(option);
    }
    indexingModel.disabled = true;
    profile.disabled = true;
    engine.onchange = () => {
      profile.disabled = !["sqlite_hybrid", "lightrag_local"].includes(
        engine.value,
      );
      profile.required = !profile.disabled;
      indexingModel.disabled = engine.value !== "lightrag_local";
      indexingModel.required = !indexingModel.disabled;
    };
    const createError = element("p", "notice error knowledge-form-error");
    createError.setAttribute("role", "alert");
    createError.tabIndex = -1;
    createError.hidden = true;
    const submit = element("button", "primary", "Create");
    submit.type = "submit";
    form.append(
      field("Name", name),
      field("Description", description),
      field("Engine", engine),
      field("Embedding profile", profile),
      field("Indexing LLM (LightRAG)", indexingModel),
      createError,
      submit,
    );
    form.onsubmit = async (event) => {
      event.preventDefault();
      submit.disabled = true;
      submit.textContent = "Creating…";
      createError.hidden = true;
      try {
        const base = await this.request("", {
          name: name.value,
          description: description.value,
          engine: engine.value,
          ...(profile.disabled ? {} : { embedding_profile_id: profile.value }),
          ...(indexingModel.disabled
            ? {}
            : { indexing_model: indexingModel.value }),
        });
        await this.refresh(base.id);
        this.notice.textContent = "Knowledge base created.";
      } catch (error) {
        if (error.name !== "AbortError") {
          createError.textContent = `Could not create knowledge base: ${error.message}${/unknown knowledge engine/i.test(error.message) ? ". The running service has not loaded this engine. Restart the service and refresh this page." : ""}`;
          createError.hidden = false;
          createError.focus();
          createError.scrollIntoView?.({ block: "nearest" });
        }
        submit.disabled = false;
        submit.textContent = "Create";
      }
    };
    details.append(form);
    this.content.append(details);
    const profiles = element("details", "knowledge-create");
    profiles.append(element("summary", "", "Configure embedding service"));
    const config = element("form", "knowledge-form"),
      profileName = input("Local embedding"),
      endpoint = input("http://localhost:11434/v1/embeddings", "url"),
      model = input("Embedding model name"),
      env = input("Optional API key environment variable");
    profileName.required = endpoint.required = model.required = true;
    const preset = button("Use DashScope · text-embedding-v4");
    preset.onclick = () => {
      profileName.value = "DashScope text-embedding-v4";
      endpoint.value =
        "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings";
      model.value = "text-embedding-v4";
      env.value = "LOOM_LLM_API_KEY";
      endpoint.focus();
    };
    const profileError = element("p", "notice error knowledge-form-error");
    profileError.setAttribute("role", "alert");
    profileError.tabIndex = -1;
    profileError.hidden = true;
    const add = element("button", "", "Add embedding profile");
    add.type = "submit";
    config.append(
      preset,
      field("Profile name", profileName),
      field("Full embeddings endpoint", endpoint),
      field("Model", model),
      field("API key environment variable", env),
      element(
        "p",
        "muted",
        "OpenAI-compatible embeddings API. Text passages and queries are sent to this endpoint; documents and indexes stay local. Credentials are read from the service environment or its configured .env file; enter the variable name, not the secret. Each knowledge base keeps its chosen profile; changing models requires a new index.",
      ),
      profileError,
      add,
    );
    config.onsubmit = async (event) => {
      event.preventDefault();
      add.disabled = true;
      profileError.hidden = true;
      try {
        await this.request("/embedding-profiles", {
          name: profileName.value,
          endpoint: endpoint.value,
          model: model.value,
          api_key_env: env.value,
        });
        await this.refresh();
        this.notice.textContent = "Embedding profile saved.";
      } catch (error) {
        if (error.name !== "AbortError") {
          profileError.textContent = `Could not save embedding profile: ${error.message}`;
          profileError.hidden = false;
          profileError.focus();
          profileError.scrollIntoView?.({ block: "nearest" });
        }
        add.disabled = false;
      }
    };
    profiles.append(config);
    this.content.append(profiles);
  }
  async renderBase() {
    clearTimeout(this.timer);
    const id = this.baseId,
      base = await this.request(`/${id}`);
    if (id !== this.baseId || this.abort.signal.aborted) return;
    const option = [...this.baseSelect.options].find(
      (item) => item.value === id,
    );
    if (option)
      option.textContent = `${base.name} · ${base.engine} · ${base.documents.length} documents`;
    this.knowledgeJobListeners = new Set();
    this.latestKnowledgeJob = null;
    this.basePanel.replaceChildren(
      element("h3", "", base.name),
      element(
        "p",
        "muted",
        `${base.chunk_count} indexed passages · ${base.engine}${base.embedding_dimension ? ` · ${base.embedding_dimension} dimensions` : ""}`,
      ),
    );
    if (base.description)
      this.basePanel.append(element("p", "", base.description));
    const documents = element("ul", "knowledge-documents");
    for (const doc of base.documents) {
      const row = element("li");
      const remove = button("Remove");
      remove.onclick = async () => {
        remove.disabled = true;
        try {
          await this.request(`/${id}/documents/${doc.id}/delete`, {});
          await this.renderBase();
        } catch (error) {
          this.error(error);
          remove.disabled = false;
        }
      };
      row.append(element("span", "", doc.name), remove);
      documents.append(row);
    }
    this.basePanel.append(documents);
    const form = element("form", "knowledge-form"),
      file = input("", "file"),
      name = input("Document name"),
      content = element("textarea");
    const yakdb = base.engine === "yakdb_local";
    let fileBase64 = null;
    file.accept =
      ".txt,.md,.markdown,.rst,.csv,.json,.py,.js,.ts,.yaml,.yml,.toml,.html,.log";
    if (yakdb) file.accept += ",.pdf,.docx,.pptx,.xlsx";
    name.required = true;
    content.rows = 4;
    content.placeholder = "Paste text or select a UTF-8 file";
    content.required = true;
    file.onchange = async () => {
      const selected = file.files[0];
      fileBase64 = null;
      content.disabled = false;
      content.required = true;
      if (!selected) return;
      content.value = "";
      name.value = "";
      if (selected.size > (yakdb ? 20 * 1024 * 1024 : 500000)) {
        this.notice.textContent = yakdb
          ? "Import files up to 20 MiB."
          : "Import files up to 500 KB.";
        return;
      }
      try {
        if (yakdb) {
          const bytes = new Uint8Array(await selected.arrayBuffer());
          if (file.files[0] !== selected) return;
          const parts = [];
          for (let offset = 0; offset < bytes.length; offset += 32768)
            parts.push(
              String.fromCharCode(...bytes.subarray(offset, offset + 32768)),
            );
          fileBase64 = btoa(parts.join(""));
          name.value = selected.name;
          content.disabled = true;
          content.required = false;
          return;
        }
        content.value = new TextDecoder("utf-8", { fatal: true }).decode(
          await selected.arrayBuffer(),
        );
        name.value = selected.name;
      } catch {
        this.notice.textContent =
          "Import UTF-8 text or Markdown; binary/PDF documents are not supported yet.";
      }
    };
    const add = element("button", "primary", "Import / replace document");
    add.type = "submit";
    const status = element("p", "muted");
    status.setAttribute("role", "status");
    form.append(
      field(
        yakdb
          ? "PDF / Office / text file (up to 20 MiB)"
          : "UTF-8 text / Markdown file (up to 500 KB)",
        file,
      ),
      field("Document name (same name replaces its previous index)", name),
      field("Content", content),
      add,
      status,
    );
    const pending = base.jobs?.find((job) =>
      ["queued", "running"].includes(job.state),
    );
    const cancel = button("Cancel indexing / sync");
    cancel.hidden = true;
    let progress;
    if (base.engine === "lightrag_local") {
      status.hidden = true;
      progress = new KnowledgeProgress({
        cancel,
        resume: async (job) => {
          await this.request(`/${id}/sources/${job.source_id}/sync`, {});
          await this.renderBase();
        },
        error: (error) => this.error(error),
      });
      this.knowledgeJobListeners.add((job) => progress.update(job));
      this.basePanel.insertBefore(progress.root, documents);
    } else form.append(cancel);
    const abort = this.abort;
    const current = () =>
      id === this.baseId &&
      abort === this.abort &&
      !abort.signal.aborted &&
      status.isConnected;
    const poll = async (job) => {
      if (!current()) return;
      this.latestKnowledgeJob = job;
      for (const listener of this.knowledgeJobListeners) listener(job);
      status.textContent = `${job.document} · ${job.state} · ${job.stage || "indexing"}${job.pages !== undefined ? ` · ${job.pages} pages` : ""}${job.indexed !== undefined ? ` · ${job.indexed}/${job.total} documents` : ""}${job.llm_calls ? ` · ${job.llm_calls} LLM calls · ${job.reported_tokens || 0} tokens` : ""}${job.embedding_inputs ? ` · ${job.embedding_inputs} embedding inputs` : ""}${job.current_document ? ` · ${job.current_document}` : ""}${job.current_url ? ` · ${job.current_url}` : ""}${job.error ? ` · ${job.error}` : ""}`;
      add.disabled = ["queued", "running"].includes(job.state);
      cancel.hidden = !add.disabled || base.engine !== "lightrag_local";
      cancel.onclick = async () => {
        cancel.disabled = true;
        try {
          const receipt = await this.request(
            `/${id}/jobs/${job.id}/cancel`,
            {},
          );
          this.latestKnowledgeJob = receipt;
          for (const listener of this.knowledgeJobListeners) listener(receipt);
          status.textContent = "Cancellation requested…";
          progress?.cancellationRequested();
        } catch (error) {
          this.error(error);
          cancel.disabled = false;
        }
      };
      if (add.disabled) {
        const follow = async () => {
          if (!current()) return;
          try {
            await poll(await this.request(`/${id}/jobs/${job.id}`));
          } catch (error) {
            if (!current()) return;
            if (progress) progress.connectionError(error);
            else this.error(error);
            this.timer = setTimeout(follow, 2000);
          }
        };
        this.timer = setTimeout(follow, 800);
      } else if (job.state === "completed") {
        this.changed();
        this.notice.textContent =
          job.result?.complete === false
            ? `Partial sync: ${job.result.pages} collected pages indexed; website crawl incomplete. See source details.`
            : `Indexed ${job.result.chunks} passages from ${job.document}.`;
        await this.renderBase();
      }
    };
    form.onsubmit = async (event) => {
      event.preventDefault();
      add.disabled = true;
      try {
        await poll(
          await this.request(`/${id}/documents`, {
            name: name.value,
            ...(fileBase64 !== null
              ? { file_base64: fileBase64 }
              : { content: content.value }),
          }),
        );
      } catch (error) {
        this.error(error);
        add.disabled = false;
      }
    };
    this.basePanel.append(form);
    if (pending) poll(pending);
    else if (base.jobs?.[0]) {
      const job = base.jobs[0];
      this.latestKnowledgeJob = job;
      progress?.update(job);
      status.textContent = `${job.result?.complete === false ? "Partial sync — website crawl incomplete" : job.state}: ${job.error || `${job.result?.chunks || 0} indexed chunks`}`;
    }
    const search = element("form", "knowledge-search"),
      query = input("Test a query"),
      submit = element("button", "", "Search"),
      results = element("div");
    submit.type = "submit";
    query.required = true;
    search.append(field("Search this knowledge base", query), submit);
    search.onsubmit = async (event) => {
      event.preventDefault();
      submit.disabled = true;
      try {
        const data = await this.request(`/${id}/search`, {
          query: query.value,
          limit: 5,
        });
        results.replaceChildren();
        for (const hit of data.matches) {
          const item = element("article", "knowledge-hit");
          item.append(
            element(
              "strong",
              "",
              hit.source_url
                ? hit.source_url
                : hit.page_number
                  ? `${hit.document} · page ${hit.page_number}`
                  : `${hit.document}:${hit.start_line}–${hit.end_line}`,
            ),
            element("small", "muted", hit.source_id),
            element("p", "", hit.text),
          );
          results.append(item);
        }
        if (!data.matches.length) results.textContent = "No matching passages.";
      } catch (error) {
        this.error(error);
      } finally {
        submit.disabled = false;
      }
    };
    this.basePanel.append(search, results);
    if (base.engine === "lightrag_local")
      await renderKnowledgeGraph(this, base);
  }
}
