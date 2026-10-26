import { element } from "../markdown.mjs";
import { renderKnowledgeGraph } from "./knowledge-graph.mjs";
import { KnowledgeActivity } from "./knowledge-activity.mjs";
import { canUpdateTaskTools } from "../state.mjs";

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
    this.creating = create;
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
    if (this.creating) {
      this.onSelected(null);
      this.renderCreate(catalog);
      this.content.querySelector("input")?.focus();
      return;
    }
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
        "Update attachments while paused or between tasks. Resume applies changes and preserves completed work.",
      ),
    );
    const editable = canUpdateTaskTools(this.session);
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
    const details = element("section", "knowledge-create-page");
    details.append(element("h2", "", "Create a knowledge base"));
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
        this.creating = false;
        this.activeTab = "import";
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
  }
  async renderBase() {
    clearTimeout(this.timer);
    const id = this.baseId, base = await this.request(`/${id}`);
    if (id !== this.baseId || this.abort.signal.aborted) return;
    this.knowledgeJobListeners = new Set();
    this.basePanel.replaceChildren();
    const header = element('div', 'dashboard-toolbar');
    const title = element('div'); title.append(element('h2', '', base.name), element('span', 'muted', base.engine));
    const remove = button('Delete knowledge base'); remove.classList.add('danger');
    remove.onclick = async () => {
      if (!confirm(`Delete “${base.name}” and its indexes? Original local files will remain.`)) return;
      try {await this.request(`/${id}/delete`, {}); this.baseId = null; await this.refresh();}
      catch (error) {this.error(error);}
    };
    header.append(title, remove); this.basePanel.append(header);
    const tabs = element('div', 'evaluation-tabs'); tabs.setAttribute('role', 'tablist');
    this.basePanel.append(tabs);
    const panes = {};
    for (const [key, label] of [['import', 'Import'], ['view', 'View'], ['search', 'Search']]) {
      const tab = button(label), pane = element('section', 'kb-tab-pane');
      tab.setAttribute('role', 'tab'); pane.setAttribute('role', 'tabpanel');
      tab.id = `kb-tab-${key}`; pane.id = `kb-pane-${key}`;
      tab.setAttribute('aria-controls', pane.id); pane.setAttribute('aria-labelledby', tab.id);
      panes[key] = pane; tabs.append(tab); this.basePanel.append(pane);
      tab.onclick = () => {this.activeTab = key; show();};
    }
    const show = () => {
      [...tabs.children].forEach((tab, index) => {
        const key = ['import', 'view', 'search'][index], selected = key === (this.activeTab || 'import');
        tab.setAttribute('aria-selected', String(selected)); panes[key].hidden = !selected;
      });
    }; show();
    this.importPanel = panes.import; this.viewPanel = panes.view;
    const modes = element('div', 'kb-import-modes'), importPanes = {};
    modes.setAttribute('role', 'group'); modes.setAttribute('aria-label', 'Import source type');
    this.importPanel.append(modes);
    for (const [kind, label] of [['file', 'Single file'], ['folder', 'Folder'], ['web', 'Web URL']]) {
      const select = button(label), pane = element('section', 'kb-import-settings');
      pane.setAttribute('aria-label', `${label} import settings`);
      importPanes[kind] = pane; modes.append(select); this.importPanel.append(pane);
      select.onclick = () => {this.importMode = kind; displayMode();};
      if (kind === 'web') {this.urlPanel = pane; continue;}
      const form = element('form', 'knowledge-form'), path = input(kind === 'folder' ? '/absolute/path/to/folder' : '/absolute/path/to/document.md');
      path.required = true;
      const submit = element('button', 'primary', kind === 'folder' ? 'Import folder' : 'Import file'); submit.type = 'submit';
      const choose = button(kind === 'folder' ? 'Choose folder…' : 'Choose file…');
      choose.onclick = async () => {
        const abort = this.abort, original = path.value, caption = choose.textContent;
        choose.disabled = true; choose.textContent = 'Choosing…';
        try {
          const result = await this.client.json(`/v1/knowledge-bases/${id}/pick-path`, {body: {kind, path: original.trim()}, signal: abort.signal});
          if (abort === this.abort && !abort.signal.aborted && path.isConnected && path.value === original && result.path) path.value = result.path;
        } catch (error) {if (abort === this.abort && !abort.signal.aborted && path.isConnected) this.error(error);}
        finally {choose.disabled = false; choose.textContent = caption;}
      };
      form.append(field(kind === 'folder' ? 'Folder path on the Loom service machine' : 'File path on the Loom service machine', path),
        choose, element('p', 'muted', 'Choose a local path or enter it manually. Index in place; original files stay at their paths.'), submit);
      form.onsubmit = async event => {
        event.preventDefault(); submit.disabled = true;
        try {await this.request(`/${id}/imports`, {kind, path: path.value.trim()}); await this.renderBase();}
        catch (error) {this.error(error); submit.disabled = false;}
      };
      pane.append(form);
    }
    const displayMode = () => {
      [...modes.children].forEach((tab, index) => {
        const key = ['file', 'folder', 'web'][index], selected = key === (this.importMode || 'file');
        tab.setAttribute('aria-pressed', String(selected)); importPanes[key].hidden = !selected;
      });
    }; displayMode();
    const jobsRoot = element('section', 'kb-import-jobs');
    this.importPanel.append(element('h3', '', 'Import activity'), jobsRoot);
    const stats = element('div', 'kb-stats');
    for (const [label, value] of [['Documents', base.documents.length], ['Passages', base.chunk_count], ['Dimensions', base.embedding_dimension || '—'],
      ['Entities', base.graph_stats?.entities ?? '—'], ['Relationships', base.graph_stats?.relations ?? '—']]) {
      const cell = element('div'); cell.append(element('strong', '', String(value)), element('span', 'muted', label)); stats.append(cell);
    }
    panes.view.append(stats);
    if (base.description) panes.view.append(element('p', 'muted', base.description));
    const documents = element('div', 'knowledge-documents');
    const groups = new Map();
    for (const doc of base.documents) {
      const groupName = doc.source_path ? doc.source_path.slice(0, doc.source_path.lastIndexOf('/')) : doc.source_url ? new URL(doc.source_url).origin : 'Documents';
      if (!groups.has(groupName)) {
        const group = element('details', 'kb-source-group'); group.open = true;
        group.append(element('summary', '', groupName)); documents.append(group); groups.set(groupName, group);
      }
      const row = element('article', 'kb-document'), info = element('div');
      info.append(element('strong', '', doc.source_path || doc.name), element('small', 'muted', `${doc.at || ''} · ${(doc.digest || '').slice(0, 12)}`));
      const removeDoc = button('Remove index');
      removeDoc.onclick = async () => {try {await this.request(`/${id}/documents/${doc.id}/delete`, {}); await this.renderBase();} catch (e) {this.error(e);}};
      row.append(info, removeDoc); groups.get(groupName).append(row);
    }
    panes.view.append(element('h3', '', 'Indexed sources'), documents);
    const search = element('form', 'knowledge-search'), query = input('Search indexed knowledge'), submit = element('button', 'primary', 'Search'), results = element('div');
    submit.type = 'submit'; query.required = true; search.append(field('Query', query), submit);
    search.onsubmit = async event => {
      event.preventDefault(); submit.disabled = true;
      try {
        const data = await this.request(`/${id}/search`, {query: query.value, limit: 5}); results.replaceChildren();
        for (const hit of data.matches) {
          const row = element('article', 'knowledge-hit');
          row.append(element('strong', '', hit.source_url || (hit.page_number ? `${hit.document} · page ${hit.page_number}` : `${hit.document}:${hit.start_line}–${hit.end_line}`)), element('small', 'muted', hit.source_id), element('p', '', hit.text)); results.append(row);
        }
        if (!data.matches.length) results.textContent = 'No matching passages.';
      } catch (error) {this.error(error);} finally {submit.disabled = false;}
    };
    panes.search.append(search, results);
    await renderKnowledgeGraph(this, base);
    const activity = new KnowledgeActivity(jobsRoot, base, {
      act: async path => {await this.request(`/${id}${path}`, {}); await this.renderBase();},
      notify: job => {this.latestKnowledgeJob = job; for (const listener of this.knowledgeJobListeners) listener(job);},
      error: error => this.error(error),
    });
    this.knowledgeJobListeners.add(job => activity.update([job], false));
    const abort = this.abort;
    let currentJobs = activity.latestJobs;
    const poll = async () => {
      if (abort !== this.abort || abort.signal.aborted || id !== this.baseId || !jobsRoot.isConnected) return;
      try {
        currentJobs = await Promise.all(currentJobs.map(job => ['queued', 'running'].includes(job.state)
          ? this.request(`/${id}/jobs/${job.id}`) : job));
        activity.update(currentJobs);
        if (!currentJobs.some(job => ['queued', 'running'].includes(job.state))) {
          this.changed();
          if (currentJobs.some(job => job.state === 'completed')) await this.renderBase();
          return;
        }
        this.timer = setTimeout(poll, 800);
      } catch (error) {
        if (abort.signal.aborted) return;
        activity.connectionError(error);
        this.timer = setTimeout(poll, 2000);
      }
    };
    if (currentJobs.some(job => ['queued', 'running'].includes(job.state))) this.timer = setTimeout(poll, 800);
  }
}
