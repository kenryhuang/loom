import { element } from "../markdown.mjs";

const stages = {
  queued: "Waiting to start",
  crawling: "Crawling pages",
  crawl_retry: "Retrying webpage request",
  resuming_crawl: "Resuming saved website snapshot",
  crawl_saved: "Website snapshot saved",
  diffing: "Checking changes",
  checking_embedding: "Checking embedding service",
  initializing: "Initializing LightRAG",
  resuming_index: "Resuming saved document checkpoints",
  indexing: "Indexing documents",
  extracting: "Extracting entities and relationships",
  extracting_failed: "Entity extraction or merging failed",
  embedding: "Generating embeddings",
  embedding_retry: "Retrying embedding request",
  embedding_failed: "Embedding request failed",
  indexing_failed: "Indexing failed",
  cleanup_failed: "Index cleanup failed",
  failed: "Sync failed",
  cancelled: "Sync cancelled",
  checkpointed: "Document checkpoint saved",
  publishing: "Publishing index",
  completed: "Sync complete",
  partial: "Partial sync — collected pages indexed; website crawl incomplete",
  interrupted: "Service interrupted",
};

export function knowledgeStageLabel(stage) {
  return stages[stage] || stage?.replaceAll("_", " ") || "Preparing";
}

export function knowledgeJobStage(job) {
  // Older jobs retained the last active stage when they failed or were cancelled.
  if (job.state === "cancelled") return "cancelled";
  if (
    job.state === "failed" &&
    job.stage !== "interrupted" &&
    !job.stage?.endsWith("_failed")
  )
    return "failed";
  return job.stage;
}

function number(value) {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.max(0, value)
    : null;
}
function count(value) {
  return number(value)?.toLocaleString() ?? "—";
}
function setText(node, value) {
  if (node.textContent !== value) node.textContent = value;
}
function duration(value) {
  const seconds = Math.floor(number(value) || 0);
  return seconds >= 3600
    ? `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
    : seconds >= 60
      ? `${Math.floor(seconds / 60)}m ${seconds % 60}s`
      : `${seconds}s`;
}
function saved(job) {
  return Boolean(
    job.checkpointed ||
    job.snapshot_saved ||
    job.snapshot_reused ||
    job.crawl_checkpoint,
  );
}
function phase(stage) {
  if (
    [
      "crawling",
      "crawl_retry",
      "resuming_crawl",
      "crawl_saved",
      "queued",
    ].includes(stage)
  )
    return 0;
  if (stage === "diffing") return 1;
  if (["publishing", "completed", "partial"].includes(stage)) return 3;
  return 2;
}

export class KnowledgeProgress {
  constructor({ cancel, resume, error }) {
    this.resume = resume;
    this.error = error;
    this.root = element("section", "knowledge-job-progress");
    this.root.setAttribute("aria-label", "Indexing and sync progress");
    const heading = element("div", "knowledge-progress-heading");
    this.title = element("h3", "", "Sync activity");
    this.state = element("span", "knowledge-progress-state", "Ready");
    const announcement = element("div", "knowledge-progress-announcement");
    announcement.setAttribute("role", "status");
    announcement.setAttribute("aria-live", "polite");
    announcement.append(this.title, this.state);
    this.actions = element("div", "knowledge-progress-actions");
    this.cancel = cancel;
    this.retry = element("button", "quiet", "Resume sync");
    this.retry.type = "button";
    this.retry.hidden = cancel.hidden = true;
    this.retry.onclick = async () => {
      const job = this.job;
      this.retry.disabled = true;
      try {
        await this.resume(job);
      } catch (error) {
        this.error(error);
      } finally {
        this.retry.disabled = false;
      }
    };
    this.actions.append(this.retry, cancel);
    heading.append(announcement, this.actions);
    this.source = element("p", "knowledge-progress-source muted");
    this.steps = element("ol", "knowledge-progress-steps");
    this.current = element("div", "knowledge-progress-current");
    this.stage = element("strong");
    this.item = element("p", "knowledge-progress-item");
    this.freshness = element("small", "muted");
    this.current.append(this.stage, this.item, this.freshness);
    this.coverage = element("div", "knowledge-progress-coverage");
    this.coverageLabel = element("span");
    this.percentage = element("strong");
    const coverageHeading = element("div", "knowledge-progress-heading");
    coverageHeading.append(this.coverageLabel, this.percentage);
    this.bar = element("progress");
    this.bar.setAttribute("aria-label", "Completed document coverage");
    this.coverageNote = element("p", "muted small");
    this.coverage.append(coverageHeading, this.bar, this.coverageNote);
    this.metrics = element("dl", "knowledge-progress-metrics");
    this.message = element("p", "knowledge-progress-message");
    this.message.setAttribute("role", "status");
    this.history = element("details", "knowledge-progress-history");
    this.log = element("ol", "knowledge-progress-log");
    this.log.setAttribute("aria-label", "Recent sync activity");
    this.history.append(element("summary", "", "Recent activity"), this.log);
    this.root.append(
      heading,
      this.source,
      this.steps,
      this.current,
      this.coverage,
      this.metrics,
      this.message,
      this.history,
    );
    this.update(null);
  }
  cancellationRequested() {
    this.cancelRequested = true;
    this.update(this.job);
  }
  connectionError(error) {
    this.root.dataset.state = "interrupted";
    setText(this.state, "Updates interrupted");
    setText(
      this.message,
      `Could not refresh progress: ${error.message}. Reconnecting automatically…`,
    );
    this.message.classList.add("error");
    this.message.hidden = false;
  }
  update(job, now = Date.now()) {
    if (job?.id !== this.job?.id) this.cancelRequested = false;
    this.job = job;
    const active = ["queued", "running"].includes(job?.state);
    if (active && job.cancel_requested) this.cancelRequested = true;
    const partial =
      job?.state === "completed" &&
      (job.stage === "partial" || job.result?.complete === false);
    const state = partial ? "partial" : job?.state || "idle";
    this.root.dataset.state = state;
    setText(
      this.title,
      job?.kind === "document" ? "Indexing activity" : "Sync activity",
    );
    setText(
      this.state,
      {
        idle: "Ready",
        queued: "Queued",
        running: "Running",
        failed: "Failed",
        cancelled: "Cancelled",
        completed: "Complete",
        partial: "Partial sync",
      }[state] || state,
    );
    this.cancel.hidden = !active;
    this.cancel.textContent =
      job?.source_id || job?.kind === "website"
        ? "Cancel sync"
        : "Cancel indexing";
    this.cancel.disabled = Boolean(this.cancelRequested);
    this.retry.hidden =
      !job?.source_id || !["failed", "cancelled"].includes(state);
    this.retry.textContent = saved(job || {}) ? "Resume sync" : "Retry sync";
    for (const node of [
      this.source,
      this.steps,
      this.coverage,
      this.metrics,
      this.history,
    ])
      node.hidden = !job;
    this.message.hidden = true;
    if (!job) {
      this.stage.textContent = "No sync is running";
      this.item.textContent =
        "Import a website or choose Sync now on a source to follow its progress here.";
      this.freshness.textContent = "";
      return;
    }
    this.source.textContent = job.document || "Knowledge base sync";
    const labels =
      job.source_id || job.kind === "website"
        ? ["Collect pages", "Check changes", "Build index", "Publish"]
        : ["Prepare", "Build index", "Publish"];
    const workStage = job.failed_stage || job.stage;
    const current =
      job.state === "completed"
        ? labels.length - 1
        : labels.length === 4
          ? phase(workStage)
          : workStage === "queued"
            ? 0
            : phase(workStage) === 3
              ? 2
              : 1;
    this.steps.replaceChildren();
    for (const [index, label] of labels.entries()) {
      const step = element(
        "li",
        index < current || job.state === "completed"
          ? "done"
          : index === current
            ? "current"
            : "",
        label,
      );
      if (index === current && job.state !== "completed")
        step.setAttribute("aria-current", "step");
      this.steps.append(step);
    }
    this.stage.textContent = partial
      ? knowledgeStageLabel("partial")
      : knowledgeStageLabel(knowledgeJobStage(job));
    const crawling = phase(workStage) === 0;
    const item = crawling ? job.current_url : job.current_document;
    this.item.textContent = item
      ? `${active ? "Working on" : "Last working on"}: ${crawling ? item : item.replace(/^web_[a-f0-9]+_[a-f0-9]+_/, "")}`
      : active
        ? "Waiting for the next progress update…"
        : "";
    this.item.hidden = !this.item.textContent;
    const lastAt =
      job.updated_at ||
      job.progress?.at(-1)?.at ||
      job.started_at ||
      job.created_at;
    const age = Number.isFinite(Date.parse(lastAt))
      ? Math.max(0, (now - Date.parse(lastAt)) / 1000)
      : null;
    const elapsed =
      active && Number.isFinite(Date.parse(job.started_at))
        ? Math.max(
            number(job.elapsed_seconds) || 0,
            (now - Date.parse(job.started_at)) / 1000,
          )
        : number(job.elapsed_seconds);
    this.freshness.textContent =
      active && age !== null
        ? `Last activity ${duration(age)} ago${age >= 30 ? ". Still checking for updates; the current request may be waiting for a response." : ""}`
        : job.finished_at
          ? "This attempt has ended."
          : "";
    const total = number(job.total);
    const indexed = number(job.indexed);
    const known = total !== null && total > 0 && indexed !== null;
    if (known) {
      this.bar.max = total;
      this.bar.value = Math.min(indexed, total);
      this.coverageLabel.textContent = `${count(indexed)} / ${count(total)} documents completed`;
      this.percentage.textContent = `${Math.floor(Math.min(indexed / total, 1) * 100)}%`;
    } else {
      this.bar.removeAttribute("value");
      this.coverageLabel.textContent = crawling
        ? `${count(job.pages ?? job.result?.pages)} pages collected · ${count(job.visited)} URLs visited`
        : job.state === "completed"
          ? `${count(job.result?.chunks)} passages indexed`
          : "Preparing document coverage";
      this.percentage.textContent = "";
    }
    this.bar.hidden = !active && !known;
    this.coverageNote.textContent = crawling
      ? "Collecting pages; the number of documents to index is determined after checking changes."
      : job.stage === "publishing"
        ? "Documents are ready. Publishing the new searchable index."
        : job.state === "completed"
          ? "The collected documents are available in the searchable index."
          : "Coverage advances after a full document finishes. The published index updates after this sync completes.";
    this.metrics.replaceChildren();
    for (const [label, value] of [
      ["Saved documents", count(job.checkpointed)],
      ["Model calls", count(job.llm_calls)],
      ["Reported tokens", count(job.reported_tokens)],
      ["Embedding inputs", count(job.embedding_inputs)],
      ["Elapsed", elapsed === null ? "—" : duration(elapsed)],
    ]) {
      const metric = element("div");
      metric.append(element("dt", "muted", label), element("dd", "", value));
      this.metrics.append(metric);
    }
    let message = "";
    if (this.cancelRequested && active)
      message =
        "Cancellation requested. Waiting for the current operation to stop.";
    else if (["failed", "cancelled"].includes(state)) {
      message = job.error || `Sync ${state}.`;
      if (job.checkpointed)
        message += ` ${count(job.checkpointed)} completed documents saved. Resume sync to continue with the unfinished document.`;
      else if (saved(job))
        message += "Saved website progress will be reused when you resume.";
    } else if (partial) {
      message =
        "Collected pages were indexed, but the website crawl is incomplete.";
      if (job.result?.pending)
        message += ` ${count(job.result.pending)} URLs still queued.`;
      if (job.result?.missing_count)
        message += ` ${count(job.result.missing_count)} URLs returned 404/410.`;
    } else if (
      active &&
      ["crawl_retry", "embedding_retry", "embedding_failed"].includes(job.stage)
    ) {
      message = `${job.detail || knowledgeStageLabel(job.stage)}${job.retry ? ` · Retry ${job.retry}` : ""}`;
    }
    this.message.classList.toggle(
      "error",
      ["failed", "cancelled"].includes(state),
    );
    setText(this.message, message);
    this.message.hidden = !message;
    const events = [];
    for (const entry of job.progress || []) {
      if (!entry.stage) continue;
      const key = JSON.stringify([
        entry.stage,
        entry.current_document,
        entry.current_url,
        entry.detail,
        entry.retry,
      ]);
      if (events.at(-1)?.key === key)
        events[events.length - 1] = { key, entry };
      else events.push({ key, entry });
    }
    this.log.replaceChildren();
    for (const { entry } of events.slice(-8).reverse()) {
      const row = element("li");
      const time = element("time", "muted");
      time.dateTime = entry.at || "";
      time.textContent = Number.isFinite(Date.parse(entry.at))
        ? new Date(entry.at).toLocaleTimeString()
        : "—";
      row.append(
        time,
        element(
          "span",
          "",
          `${knowledgeStageLabel(entry.stage)}${entry.detail ? ` · ${entry.detail}` : ""}`,
        ),
      );
      this.log.append(row);
    }
    this.history.hidden = !events.length;
  }
}
