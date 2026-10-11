import { element } from "../markdown.mjs";

export const number = value => value == null ? "—" : Number(value).toLocaleString(undefined, {maximumFractionDigits: 1});
export const seconds = value => {
  if (value == null) return "—";
  if (value > 0 && value < 1000) return `${number(value)} ms`;
  if (value >= 3600000) return `${Math.floor(value / 3600000)} h ${Math.floor(value % 3600000 / 60000)} m`;
  if (value >= 60000) return `${Math.floor(value / 60000)} m ${Math.floor(value % 60000 / 1000)} s`;
  return `${number(value / 1000)} s`;
};
export const label = value => String(value || "unknown").replaceAll("_", " ");
export function badge(text, state = "") {
  return element("span", `dashboard-badge ${["passed", "completed", "success"].includes(state) ? "positive" : ["failed", "blocked"].includes(state) ? "negative" : ""}`, text);
}
export function panel(root, title, wide = false) {
  const node = element("section", `dashboard-panel${wide ? " dashboard-wide" : ""}`);
  const header = element("header", "dashboard-panel-heading");
  header.append(element("h3", "", title)); node.append(header); root.append(node); return node;
}
export function cards(root, values) {
  const grid = element("div", "dashboard-stats");
  for (const [title, value] of values) {
    const card = element("div", "dashboard-stat");
    card.append(element("span", "muted", title), element("strong", "", value)); grid.append(card);
  }
  root.append(grid);
}
export function table(root, headers, rows) {
  const container = element("div", "base-table-scroll"), node = element("table", "base-metrics-table");
  const head = element("thead"), tr = element("tr"), body = element("tbody");
  for (const name of headers) { const th = element("th", "", name); th.scope = "col"; tr.append(th); }
  head.append(tr);
  for (const values of rows) {
    const row = element("tr");
    for (const value of values) {
      const cell = element("td");
      if (value?.nodeType) cell.append(value); else cell.textContent = value == null ? "—" : String(value);
      row.append(cell);
    }
    body.append(row);
  }
  if (!rows.length) { const row = element("tr"), cell = element("td", "muted", "No records"); cell.colSpan = headers.length; row.append(cell); body.append(row); }
  node.append(head, body); container.append(node); root.append(container); return node;
}
export function disclosure(root, title) {
  const node = element("details", "dashboard-disclosure"); node.append(element("summary", "", title)); root.append(node); return node;
}
export function distribution(root, title, entries, format = number) {
  const chart = element("figure", "dashboard-chart");
  chart.append(element("figcaption", "", title));
  const total = entries.reduce((sum, [, value]) => sum + Math.max(0, value || 0), 0);
  const bar = element("div", "dashboard-stacked-bar"), legend = element("div", "dashboard-legend");
  bar.setAttribute("role", "img");
  bar.setAttribute("aria-label", `${title}: ${entries.map(([name, value]) => `${name} ${format(value)}`).join(", ")}`);
  entries.forEach(([name, value], index) => {
    const color = `var(--chart-${index % 6})`;
    if (value > 0 && total > 0) {
      const slice = element("span"); slice.style.width = `${value / total * 100}%`; slice.style.background = color;
      slice.title = `${name}: ${format(value)} · ${number(value / total * 100)}%`; bar.append(slice);
    }
    const row = element("div"), dot = element("i"); dot.style.background = color; dot.setAttribute("aria-hidden", "true");
    row.append(dot, element("span", "", name), element("strong", "", format(value))); legend.append(row);
  });
  chart.append(bar, legend); root.append(chart); return chart;
}

export function resultPanel(root, title, count, wide = false, key = title) {
  if (count > 0) return panel(root, title, wide);
  const node = disclosure(root, title);
  node.classList.add("dashboard-panel");
  if (wide) node.classList.add("dashboard-wide");
  node.dataset.detail = key;
  return node;
}
