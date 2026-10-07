import { element } from "../markdown.mjs";

export class SessionListView {
  constructor(root, search, select, remove = null) {
    this.root = root;
    this.search = search;
    this.select = select;
    this.remove = remove;
    this.sessions = [];
    this.selectedId = null;
    this.signature = "";
    search.addEventListener("input", () => this.render());
  }
  update(sessions, selectedId) {
    this.sessions = sessions;
    this.selectedId = selectedId;
    this.render();
  }
  render() {
    const query = this.search.value.toLowerCase();
    const sessions = this.sessions.filter((session) =>
      `${session.title} ${session.session_id}`.toLowerCase().includes(query),
    );
    const signature = JSON.stringify([
      this.selectedId,
      sessions.map((session) => [
        session.session_id,
        session.title,
        session.task.state,
      ]),
    ]);
    if (signature === this.signature) return;
    this.signature = signature;
    const scroll = this.root.scrollTop;
    this.root.replaceChildren();
    for (const session of sessions) {
      const button = element(
        "button",
        `session-link${session.session_id === this.selectedId ? " selected" : ""}`,
      );
      button.dataset.sessionId = session.session_id;
      button.setAttribute(
        "aria-current",
        session.session_id === this.selectedId ? "true" : "false",
      );
      button.append(
        element("strong", "", session.title),
        element("small", "", session.task.state),
      );
      button.addEventListener("click", () => this.select(session.session_id));
      if (this.remove) {
        const row = element("div", "session-list-row");
        const remove = element("button", "quiet session-delete", "×");
        remove.type = "button";
        remove.setAttribute("aria-label", `Delete ${session.title}`);
        remove.title = "Delete session";
        remove.onclick = () => this.remove(session);
        row.append(button, remove);
        this.root.append(row);
      } else this.root.append(button);
    }
    if (!sessions.length)
      this.root.append(
        element(
          "p",
          "muted",
          query ? "No matching sessions." : "No sessions yet. Start a new one.",
        ),
      );
    this.root.scrollTop = scroll;
  }
}
