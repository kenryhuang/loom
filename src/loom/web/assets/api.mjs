/** HTTP/SSE transport. Credentials never enter URLs or persistent browser storage. */
export class ApiError extends Error {
  constructor(message, status = 0, code = "HTTP_ERROR") {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export class SseParser {
  constructor() {
    this.buffer = "";
    this.data = [];
  }
  push(chunk) {
    this.buffer += chunk;
    const events = [];
    let newline;
    while ((newline = this.buffer.indexOf("\n")) !== -1) {
      const line = this.buffer.slice(0, newline).replace(/\r$/, "");
      this.buffer = this.buffer.slice(newline + 1);
      if (!line) {
        if (this.data.length) events.push(JSON.parse(this.data.join("\n")));
        this.data = [];
      } else if (line.startsWith("data:"))
        this.data.push(line.slice(5).replace(/^ /, ""));
    }
    return events;
  }
}

export class SessionApi {
  constructor({
    token,
    fetchImpl = globalThis.fetch.bind(globalThis),
    baseUrl = "",
  }) {
    this.token = token;
    this.fetch = fetchImpl;
    this.baseUrl = baseUrl;
  }
  async open(path, { signal, body, headers = {} } = {}) {
    const response = await this.fetch(this.baseUrl + path, {
      method: body === undefined ? "GET" : "POST",
      signal,
      headers: {
        Authorization: `Bearer ${this.token}`,
        "Content-Type": "application/json",
        ...headers,
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      cache: "no-store",
    });
    if (!response.ok) {
      let error;
      try {
        error = (await response.json()).error;
      } catch {
        /* Non-JSON transport error. */
      }
      throw new ApiError(
        error?.message || `HTTP ${response.status}`,
        response.status,
        error?.code,
      );
    }
    return response;
  }
  async json(path, options) {
    return (await this.open(path, options)).json();
  }
  path(id, route) {
    return `/v1/sessions/${encodeURIComponent(id)}/${route}`;
  }
  catalog(signal) {
    return this.json("/v1/web/catalog", { signal });
  }
  async sessions(signal) {
    const values = [];
    let offset = 0;
    do {
      const page = await this.json(`/v1/sessions?offset=${offset}&limit=200`, {
        signal,
      });
      values.push(...page.sessions);
      offset = page.next_offset;
    } while (offset !== null);
    return values;
  }
  snapshot(id, signal) {
    return this.json(this.path(id, "snapshot"), { signal });
  }
  processes(id, signal) {
    return this.json(this.path(id, "processes"), { signal });
  }
  startTrajectory(id, signal) {
    return this.json(this.path(id, "trajectory"), { body: {}, signal });
  }
  trajectory(id, analysisId, signal) {
    return this.json(
      this.path(id, `trajectory/${encodeURIComponent(analysisId)}`),
      { signal },
    );
  }
  trajectoryRound(id, analysisId, roundId, signal) {
    return this.json(
      `${this.path(id, `trajectory/${encodeURIComponent(analysisId)}/round`)}?${new URLSearchParams({ round_id: roundId })}`,
      { signal },
    );
  }
  trajectoryEvidence(id, analysisId, ref, start = 0, signal) {
    const query = new URLSearchParams({ line: ref.line_number, start });
    if (ref.field_path) query.set("field", ref.field_path);
    return this.json(
      `${this.path(id, `trajectory/${encodeURIComponent(analysisId)}/evidence`)}?${query}`,
      { signal },
    );
  }
  history(id, { before, after, runId, view, limit = 200, signal } = {}) {
    const query = new URLSearchParams({ limit });
    if (before != null) query.set("before", before);
    if (after != null) query.set("after", after);
    if (runId != null) query.set("run_id", runId);
    if (view != null) query.set("view", view);
    return this.json(`${this.path(id, "history")}?${query}`, { signal });
  }
  artifact(id, digest, signal) {
    return this.json(this.path(id, `artifacts/${encodeURIComponent(digest)}`), {
      signal,
    });
  }
  create(payload, { commandId = crypto.randomUUID(), signal } = {}) {
    return this.json("/v1/sessions", {
      body: { command_id: commandId, payload },
      signal,
    });
  }
  command(
    id,
    type,
    payload = {},
    { commandId = crypto.randomUUID(), signal, revision } = {},
  ) {
    const body = { command_id: commandId, type, payload };
    if (revision != null) body.expected_task_revision = revision;
    return this.json(this.path(id, "commands"), { body, signal });
  }
  async *events(id, cursor, signal) {
    const response = await this.open(
      `${this.path(id, "events")}?after=${cursor}`,
      {
        signal,
        headers: {
          "Last-Event-ID": String(cursor),
          Accept: "text/event-stream",
        },
      },
    );
    if (!response.body) throw new ApiError("Streaming response unavailable");
    const reader = response.body.getReader(),
      decoder = new TextDecoder(),
      parser = new SseParser();
    try {
      while (!signal?.aborted) {
        const { done, value } = await reader.read();
        if (done) break;
        for (const event of parser.push(
          decoder.decode(value, { stream: true }),
        ))
          yield event;
      }
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
}

export function delay(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", abort);
      resolve();
    }, ms);
    function abort() {
      clearTimeout(timer);
      signal.removeEventListener("abort", abort);
      reject(new DOMException("Aborted", "AbortError"));
    }
    signal?.addEventListener("abort", abort, { once: true });
  });
}
