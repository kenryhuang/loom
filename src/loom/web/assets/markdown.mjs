/** Safe Markdown subset built with DOM nodes: raw HTML is always literal text. */
export function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function safeHref(value) {
  try {
    const url = new URL(value, document.baseURI);
    return ["http:", "https:", "mailto:"].includes(url.protocol)
      ? url.href
      : null;
  } catch {
    return null;
  }
}
function inline(node, value) {
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^\s)]+\))/g;
  let end = 0;
  for (const match of value.matchAll(pattern)) {
    node.append(document.createTextNode(value.slice(end, match.index)));
    const token = match[0];
    if (token.startsWith("`"))
      node.append(element("code", "", token.slice(1, -1)));
    else if (token.startsWith("**"))
      node.append(element("strong", "", token.slice(2, -2)));
    else {
      const link = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(token),
        href = safeHref(link[2]);
      if (href) {
        const anchor = element("a", "", link[1]);
        anchor.href = href;
        anchor.target = "_blank";
        anchor.rel = "noopener noreferrer";
        node.append(anchor);
      } else node.append(document.createTextNode(token));
    }
    end = match.index + token.length;
  }
  node.append(document.createTextNode(value.slice(end)));
}

export function renderMarkdown(value) {
  const root = element("div", "markdown");
  const wholeFence = /^\s*```(?:markdown|md)\s*\n([\s\S]*?)\n```\s*$/i.exec(
    value,
  );
  const lines = (wholeFence ? wholeFence[1] : value)
    .replace(/\r\n/g, "\n")
    .split("\n");
  const cells = (line) =>
    line
      .trim()
      .replace(/^\||\|$/g, "")
      .split("|")
      .map((cell) => cell.trim());
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index++;
      continue;
    }
    const fence = /^\s*(`{3,}|~{3,})(\w*)/.exec(line);
    if (fence) {
      const code = [];
      index++;
      while (index < lines.length && !lines[index].trim().startsWith(fence[1]))
        code.push(lines[index++]);
      index++;
      const pre = element("pre");
      pre.append(element("code", "", code.join("\n")));
      root.append(pre);
      continue;
    }
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      const node = element(`h${heading[1].length}`);
      inline(node, heading[2]);
      root.append(node);
      index++;
      continue;
    }
    if (
      line.includes("|") &&
      lines[index + 1] &&
      cells(lines[index + 1]).every((cell) => /^:?-{3,}:?$/.test(cell))
    ) {
      const table = element("table"),
        thead = element("thead"),
        head = element("tr");
      for (const cell of cells(line)) {
        const th = element("th");
        inline(th, cell);
        head.append(th);
      }
      thead.append(head);
      table.append(thead);
      const tbody = element("tbody");
      index += 2;
      while (
        index < lines.length &&
        lines[index].trim() &&
        lines[index].includes("|")
      ) {
        const row = element("tr");
        for (const cell of cells(lines[index++])) {
          const td = element("td");
          inline(td, cell);
          row.append(td);
        }
        tbody.append(row);
      }
      table.append(tbody);
      root.append(table);
      continue;
    }
    const list = /^(?:[-*+] |\d+\. )/.exec(line);
    if (list) {
      const ordered = /^\d/.test(line),
        node = element(ordered ? "ol" : "ul");
      const pattern = ordered ? /^\d+\. / : /^[-*+] /;
      while (index < lines.length && pattern.test(lines[index])) {
        const item = element("li");
        inline(item, lines[index++].replace(pattern, ""));
        node.append(item);
      }
      root.append(node);
      continue;
    }
    if (line.startsWith("> ")) {
      const quote = element("blockquote");
      inline(quote, line.slice(2));
      root.append(quote);
      index++;
      continue;
    }
    const paragraph = element("p");
    inline(paragraph, line);
    root.append(paragraph);
    index++;
  }
  return root;
}

export function renderResult(value) {
  if (typeof value === "string") return renderMarkdown(value);
  const pre = element("pre", "json-result", JSON.stringify(value, null, 2));
  return pre;
}
