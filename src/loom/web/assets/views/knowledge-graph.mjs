import { element } from "../markdown.mjs";

function button(text) {
  const node = element("button", "quiet", text);
  node.type = "button";
  return node;
}
function field(label, value, type = "text") {
  const wrap = element("label", "knowledge-field", label),
    input = element("input");
  input.type = type;
  input.value = value;
  wrap.append(input);
  return [wrap, input];
}

export async function renderKnowledgeGraph(view, base) {
  const id = base.id,
    root = element("section", "knowledge-graph-section");
  view.basePanel.prepend(root);
  const stats = base.graph_stats;
  root.append(element("h3", "", "Website sources & knowledge graph"));
  root.append(
    element(
      "p",
      "muted",
      stats
        ? `${stats.entities}${stats.graph_truncated ? "+" : ""} entities · ${stats.relations} visible relationships · ${stats.chunks} chunks · ${base.indexing_model}`
        : `No published graph yet · indexing model: ${base.indexing_model}`,
    ),
  );
  const sourceList = element("div");
  root.append(sourceList);
  const invoke = async (action, control) => {
    control.disabled = true;
    try {
      await action();
    } catch (error) {
      view.error(error);
    } finally {
      control.disabled = false;
    }
  };
  const details = element("section", "knowledge-url-import");
  details.append(element("h3", "", "Import URL"));
  const form = element("form", "knowledge-form knowledge-url-form");
  const [urlField, url] = field("Website URL", "", "url");
  url.required = true;
  url.placeholder = "https://docs.example.com/";
  const [pathField, path] = field("Path scope", "/");
  const [pagesField, pages] = field("Maximum pages (1–1000)", 200, "number");
  pages.min = 1;
  pages.max = 1000;
  const [depthField, depth] = field("Maximum link depth (0–10)", 3, "number");
  depth.min = 0;
  depth.max = 10;
  const [intervalField, interval] = field(
    "Sync interval in hours (0 = manual)",
    0,
    "number",
  );
  interval.min = 0;
  interval.max = 720;
  const removeWrap = element("label", "knowledge-choice"),
    remove = element("input");
  remove.type = "checkbox";
  removeWrap.append(
    remove,
    document.createTextNode(
      "Remove missing pages only after a complete, successful crawl",
    ),
  );
  const submit = element("button", "primary", "Import URL");
  submit.type = "submit";
  const advanced = element("details", "knowledge-url-options");
  advanced.append(
    element("summary", "", "Crawl settings"),
    pathField,
    pagesField,
    depthField,
    intervalField,
    removeWrap,
  );
  form.append(
    urlField,
    advanced,
    element(
      "p",
      "muted small",
      "Crawls public HTML/Markdown within this origin and path, respecting robots.txt. Login-only or JavaScript-only content must be exported first. Indexing sends page text to the selected LLM and embedding services; graph and documents remain local.",
    ),
    submit,
  );
  form.onsubmit = (event) => {
    event.preventDefault();
    invoke(async () => {
      const source = await view.request(`/${id}/sources`, {
        url: url.value.trim(),
        path_prefix: path.value.trim(),
        max_pages: Number(pages.value),
        max_depth: Number(depth.value),
        interval_hours: Number(interval.value),
        delete_missing: remove.checked,
      });
      try {
        await view.request(`/${id}/sources/${source.id}/sync`, {});
      } finally {
        await view.renderBase();
      }
    }, submit);
  };
  details.append(form);
  root.insertBefore(details, sourceList);

  const data = await view.request(`/${id}/sources`);
  if (view.baseId !== id || !root.isConnected) return;
  for (const source of data.sources) {
    const row = element("article", "knowledge-source");
    const link = element("a", "", source.url);
    link.href = source.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    row.append(
      link,
      element(
        "p",
        "muted small",
        `${source.max_pages} page limit · depth ${source.max_depth} · scope ${source.path_prefix} · ${source.enabled && source.interval_hours ? `every ${source.interval_hours} hours` : "manual sync"}`,
      ),
    );
    if (source.last_result) {
      const r = source.last_result;
      row.append(
        element(
          "p",
          "small",
          `${source.last_sync_at}: ${r.pages} pages · ${r.updated} updated · ${r.unchanged} unchanged · ${r.removed} removed · ${r.complete ? "crawl complete" : "partial crawl; old pages retained"}`,
        ),
      );
      if (r.missing_count || r.incomplete_reasons?.length || r.pending)
        row.append(
          element(
            "p",
            "small",
            [
              r.missing_count ? `${r.missing_count} URLs returned 404/410` : "",
              ...(r.incomplete_reasons || []),
              r.pending ? `${r.pending} URLs still queued` : "",
            ]
              .filter(Boolean)
              .join(" · "),
          ),
        );
      for (const error of r.errors || [])
        row.append(element("p", "error small", `${error.url}: ${error.error}`));
      if (r.skipped?.length)
        row.append(
          element(
            "p",
            "muted small",
            `${r.skipped.length} pages excluded by robots.txt.`,
          ),
        );
    }
    const sync = button("Sync now"),
      scheduled = button(source.enabled ? "Pause source" : "Enable source");
    const progress = element("p", "knowledge-source-progress");
    progress.setAttribute("role", "status");
    progress.setAttribute("aria-live", "polite");
    const cancel = button("Cancel sync");
    cancel.hidden = true;
    const updateProgress = (job) => {
      if (job.source_id !== source.id) return;
      const active = ["queued", "running"].includes(job.state);
      sync.disabled = active;
      sync.textContent = active ? "Syncing…" : "Sync now";
      cancel.hidden = !active;
      const stages = {
        queued: "Waiting to start",
        crawling: "Crawling pages",
        crawl_retry: "Retrying webpage request",
        partial:
          "Partial sync — collected pages indexed; website crawl incomplete",
        diffing: "Checking changes",
        checking_embedding: "Checking embedding service",
        initializing: "Initializing LightRAG",
        indexing: "Indexing documents",
        extracting: "Extracting entities and relationships",
        embedding: "Generating embeddings",
        embedding_retry: "Retrying embedding request",
        embedding_failed: "Embedding request failed",
        publishing: "Publishing index",
        completed: "Sync complete",
      };
      const partial =
        job.state === "completed" && job.result?.complete === false;
      const parts = [
        partial ? "partial" : job.state,
        partial
          ? stages.partial
          : stages[job.stage] || job.stage || "Starting sync",
      ];
      if (job.visited !== undefined) parts.push(`${job.visited} URLs visited`);
      if (job.pages !== undefined) parts.push(`${job.pages} pages collected`);
      if (active && job.missing_count)
        parts.push(`${job.missing_count} URLs returned 404/410`);
      if (active && job.request_errors)
        parts.push(`${job.request_errors} request errors`);
      if (job.indexed !== undefined)
        parts.push(`${job.indexed}/${job.total} documents indexed`);
      if (job.llm_calls)
        parts.push(
          `${job.llm_calls} LLM calls`,
          `${job.reported_tokens || 0} reported tokens`,
        );
      if (job.embedding_inputs)
        parts.push(`${job.embedding_inputs} embedding inputs`);
      if (job.elapsed_seconds !== undefined)
        parts.push(`${Math.round(job.elapsed_seconds)}s elapsed`);
      if (active && (job.current_document || job.current_url))
        parts.push(job.current_document || job.current_url);
      if (job.error) parts.push(job.error);
      if (
        active &&
        job.detail &&
        ["embedding_retry", "embedding_failed"].includes(job.stage)
      )
        parts.push(job.detail);
      if (job.state === "completed" && job.result)
        parts.push(
          `${job.result.updated || 0} updated`,
          `${job.result.unchanged || 0} unchanged`,
          `${job.result.removed || 0} removed`,
        );
      if (job.result?.incomplete_reasons?.length)
        parts.push(job.result.incomplete_reasons.join(", "));
      if (job.result?.errors?.length)
        parts.push(`${job.result.errors.length} request errors`);
      if (job.result?.missing_count)
        parts.push(`${job.result.missing_count} URLs returned 404/410`);
      if (job.result?.pending)
        parts.push(`${job.result.pending} URLs still queued`);
      progress.textContent = parts.join(" · ");
      cancel.onclick = async () => {
        cancel.disabled = true;
        try {
          await view.request(`/${id}/jobs/${job.id}/cancel`, {});
          progress.textContent += " · Cancellation requested…";
        } catch (error) {
          progress.textContent = error.message;
          cancel.disabled = false;
        }
      };
    };
    view.knowledgeJobListeners.add(updateProgress);
    const latest =
      view.latestKnowledgeJob?.source_id === source.id
        ? view.latestKnowledgeJob
        : base.jobs?.find((job) => job.source_id === source.id);
    if (latest) updateProgress(latest);
    row.append(progress, cancel);
    sync.onclick = () =>
      invoke(async () => {
        progress.textContent = "Starting sync…";
        try {
          const job = await view.request(
            `/${id}/sources/${source.id}/sync`,
            {},
          );
          updateProgress(job);
          await view.renderBase();
        } catch (error) {
          progress.textContent = `Sync failed: ${error.message}`;
          throw error;
        }
      }, sync);
    scheduled.onclick = () =>
      invoke(async () => {
        await view.request(`/${id}/sources/${source.id}`, {
          enabled: !source.enabled,
        });
        await view.renderBase();
      }, scheduled);
    row.append(sync, scheduled);
    const settings = element("details");
    settings.append(element("summary", "", "Sync settings"));
    const edit = element("form", "knowledge-form");
    const controls = {};
    for (const [key, title, min, max] of [
      ["max_pages", "Maximum pages", 1, 1000],
      ["max_depth", "Maximum depth", 0, 10],
      ["interval_hours", "Sync interval (hours; 0 = manual)", 0, 720],
      ["delay_seconds", "Request delay (seconds)", 0.1, 60],
    ]) {
      const [wrap, input] = field(title, source[key], "number");
      input.min = min;
      input.max = max;
      input.required = true;
      input.step = key === "delay_seconds" ? "0.1" : "1";
      controls[key] = input;
      edit.append(wrap);
    }
    const save = element("button", "quiet", "Save settings");
    save.type = "submit";
    edit.append(save);
    settings.append(edit);
    row.append(settings);
    edit.onsubmit = (event) => {
      event.preventDefault();
      invoke(async () => {
        await view.request(
          `/${id}/sources/${source.id}`,
          Object.fromEntries(
            Object.entries(controls).map(([key, input]) => [
              key,
              Number(input.value),
            ]),
          ),
        );
        await view.renderBase();
      }, save);
    };
    sourceList.append(row);
  }

  const graphSection = element("details", "knowledge-create");
  graphSection.append(element("summary", "", "Explore knowledge graph"));
  const [labelField, label] = field("Entity name (empty = graph overview)", "");
  const load = button("Load graph"),
    exportButton = button("Export JSON"),
    canvas = element("div", "knowledge-graph"),
    inspector = element("div");
  exportButton.disabled = true;
  graphSection.append(labelField, load, exportButton, canvas, inspector);
  root.append(graphSection);
  let graph;
  load.onclick = () =>
    invoke(async () => {
      graph = await view.request(`/${id}/graph`, {
        label: label.value.trim(),
        limit: 150,
      });
      exportButton.disabled = false;
      drawGraph(canvas, inspector, graph);
    }, load);
  exportButton.onclick = () => {
    const href = URL.createObjectURL(
      new Blob([JSON.stringify(graph, null, 2)], { type: "application/json" }),
    );
    const anchor = element("a");
    anchor.href = href;
    anchor.download = `${base.name}-graph.json`;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(href), 1000);
  };
}

function drawGraph(root, inspector, graph) {
  root.replaceChildren();
  inspector.replaceChildren();
  root.append(
    element(
      "p",
      "muted small",
      `${graph.nodes.length} nodes · ${graph.edges.length} edges${graph.is_truncated ? " · partial graph; search an entity to narrow the view" : ""}. Click a node or relationship to inspect its sources.`,
    ),
  );
  if (!graph.nodes.length) {
    root.append(element("p", "muted", "No entities found."));
    return;
  }
  const ns = "http://www.w3.org/2000/svg",
    svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 800 600");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Knowledge graph");
  const nodes = graph.nodes,
    coords = new Map(
      nodes.map((node, i) => [
        node.id,
        [
          400 + 250 * Math.cos((2 * Math.PI * i) / nodes.length),
          300 + 250 * Math.sin((2 * Math.PI * i) / nodes.length),
        ],
      ]),
    );
  const inspect = (item) => {
    inspector.replaceChildren(
      element("h4", "", item.labels?.join(", ") || item.type || item.id),
    );
    for (const [key, value] of Object.entries(item.properties || {})) {
      const row = element("p", "small");
      row.append(element("strong", "", `${key}: `));
      if (key === "file_path") {
        for (const path of String(value).split("<SEP>")) {
          if (/^https?:\/\//i.test(path)) {
            const link = element("a", "", path);
            link.href = path;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            row.append(link, document.createTextNode(" "));
          } else row.append(document.createTextNode(path));
        }
      } else row.append(document.createTextNode(String(value)));
      inspector.append(row);
    }
  };
  for (const edge of graph.edges) {
    const a = coords.get(edge.source),
      b = coords.get(edge.target);
    if (!a || !b) continue;
    const line = document.createElementNS(ns, "line");
    for (const [k, v] of Object.entries({
      x1: a[0],
      y1: a[1],
      x2: b[0],
      y2: b[1],
    }))
      line.setAttribute(k, v);
    line.setAttribute("stroke", "#456675");
    line.setAttribute("stroke-width", "2");
    line.onclick = () => inspect(edge);
    svg.append(line);
  }
  const choices = element("select");
  choices.setAttribute("aria-label", "Inspect graph entity");
  choices.append(element("option", "", "Select an entity"));
  nodes.forEach((node) => {
    const [x, y] = coords.get(node.id),
      g = document.createElementNS(ns, "g"),
      dot = document.createElementNS(ns, "circle"),
      title = document.createElementNS(ns, "title");
    dot.setAttribute("cx", x);
    dot.setAttribute("cy", y);
    dot.setAttribute("r", "7");
    dot.setAttribute("fill", "#61cdb6");
    title.textContent = node.labels?.join(", ") || node.id;
    g.append(dot, title);
    g.onclick = () => inspect(node);
    svg.append(g);
    const option = element("option", "", node.labels?.join(", ") || node.id);
    option.value = node.id;
    choices.append(option);
  });
  choices.onchange = () => {
    const node = nodes.find((n) => n.id === choices.value);
    if (node) inspect(node);
  };
  root.append(choices, svg);
}
