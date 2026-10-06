import { element } from "../markdown.mjs";

export class KnowledgeListView {
  constructor(root, search, { api, select, error }) {
    Object.assign(this, { root, search, api, select, error });
    this.bases = [];
    this.connected = false;
    search.addEventListener("input", () => this.render());
  }
  reset() {
    this.abort?.abort();
    this.connected = false;
    this.bases = [];
    this.selectedId = null;
    this.render();
  }
  async refresh() {
    this.abort?.abort();
    const abort = new AbortController();
    this.abort = abort;
    const api = this.api();
    if (!api) return this.reset();
    this.connected = true;
    if (!this.bases.length) this.root.textContent = "Loading knowledge bases…";
    try {
      const data = await api.json("/v1/knowledge-bases", {
        signal: abort.signal,
      });
      if (this.abort !== abort || abort.signal.aborted) return;
      this.bases = data.knowledge_bases;
      this.render();
    } catch (error) {
      if (this.abort !== abort || abort.signal.aborted) return;
      this.root.replaceChildren(
        element(
          "p",
          "error",
          "Could not load knowledge bases. Use refresh to retry.",
        ),
      );
      this.error(error);
    }
  }
  render() {
    const query = this.search.value.trim().toLowerCase();
    const bases = this.bases.filter((base) =>
      `${base.name} ${base.engine}`.toLowerCase().includes(query),
    );
    const scroll = this.root.scrollTop;
    this.root.replaceChildren();
    for (const base of bases) {
      const row = element(
        "button",
        `session-link knowledge-link${base.id === this.selectedId ? " selected" : ""}`,
      );
      row.dataset.knowledgeId = base.id;
      row.setAttribute("aria-current", String(base.id === this.selectedId));
      row.append(
        element("strong", "", base.name),
        element("small", "knowledge-engine", base.engine),
        element(
          "small",
          "",
          `${base.documents.length} documents · ${base.chunk_count} passages`,
        ),
      );
      row.onclick = () => {
        this.selectedId = base.id;
        this.render();
        this.select(base.id);
      };
      this.root.append(row);
    }
    if (!bases.length)
      this.root.append(
        element(
          "p",
          "muted",
          !this.connected
            ? "Connect to load knowledge bases."
            : query
              ? "No matching knowledge bases."
              : "No knowledge bases yet. Click New to create one.",
        ),
      );
    this.root.scrollTop = scroll;
  }
}
