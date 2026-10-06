import {
  compact,
  decode,
  modelHeadline,
  modelText,
  outputValue,
  text,
} from "./content.mjs";

/** Extensions can prepend a capability presenter without editing the feed. */
export class ActivityPresenters {
  constructor() {
    this.entries = [];
  }
  register(matches, present) {
    this.entries.unshift({ matches, present });
    return this;
  }
  present(descriptor, context = {}) {
    const data = descriptor.details || {};
    const input = decode(data.input || data.arguments) || {};
    const output = outputValue(data.output);
    const name = data.tool_id || data.tool_name || "Tool";
    const args = { descriptor, data, input, output, name, ...context };
    const custom = this.entries.find((entry) => entry.matches(args));
    return custom ? custom.present(args) : generic(args);
  }
}
function relative(value, workspace) {
  const path = text(value);
  return workspace && path.startsWith(`${workspace.replace(/\/$/, "")}/`)
    ? path.slice(workspace.replace(/\/$/, "").length + 1)
    : path;
}
function base({ descriptor, data, input, output, name, workspace }) {
  const error = data.error || data.output?.error || output?.error;
  const failed =
    descriptor.status === "failed" ||
    data.output?.ok === false ||
    output?.ok === false ||
    output?.accepted === false ||
    output?.timed_out === true ||
    (output?.exit_code != null &&
      output.exit_code !== 0 &&
      output.status !== "no_match");
  const state = failed
    ? "failed"
    : descriptor.status === "running"
      ? "running"
      : "completed";
  const subject = relative(
    input.path ||
      input.file_path ||
      output?.path ||
      input.url ||
      input.query ||
      input.pattern,
    workspace,
  );
  const note = compact(
    text(error) || error?.message || (failed ? output?.stderr : ""),
  );
  return {
    kind: descriptor.kind || "event",
    toolName: descriptor.kind === "tool" ? name : "",
    title: name.replace(/[_.-]+/g, " "),
    subject,
    state,
    outcome:
      note ||
      (descriptor.artifact && !descriptor.hydrated
        ? "Result pending load"
        : failed
          ? "Failed"
          : state === "running"
            ? "Running"
            : "Done"),
    sections: [],
    pending: !!descriptor.artifact && !descriptor.hydrated,
  };
}
const section = (label, value, kind = "text") => ({ label, value, kind });
function usefulFields(value) {
  if (!value || typeof value !== "object") return [];
  return Object.entries(value)
    .filter(
      ([key, val]) =>
        !/^(?:id|.*_id|metadata|usage|revision|artifact|schema|messages|tools)$/.test(
          key,
        ) && ["string", "number", "boolean"].includes(typeof val),
    )
    .slice(0, 6)
    .map(([label, val]) => section(label.replace(/_/g, " "), String(val)));
}
function generic(args) {
  const { descriptor: d, data, input, output } = args;
  const result = base(args);
  if (d.kind !== "tool") {
    result.title = d.summary || "Activity";
    result.outcome = "";
    result.sections =
      typeof data === "string"
        ? [section("", data, "markdown")]
        : usefulFields(data.error || data);
  } else {
    result.sections = [
      ...usefulFields(input),
      ...(typeof output === "string"
        ? [section("Result", output, "markdown")]
        : usefulFields(output)),
    ];
    if (!result.sections.length && output != null)
      result.sections.push(
        section("", "Structured result available in raw records."),
      );
  }
  return result;
}
export function builtinPresenters() {
  return new ActivityPresenters()
    .register(
      ({ descriptor }) => ["model", "thought"].includes(descriptor.kind),
      (args) => {
        const result = base(args),
          { data, descriptor } = args;
        const prose = typeof data === "string" ? data : modelText(data);
        result.title = result.state === "failed" ? "Model failed" : "Working";
        result.subject = modelHeadline(data, result.state === "running");
        result.outcome =
          result.state === "failed"
            ? compact(data.error?.message)
            : result.state === "running"
              ? ""
              : "Done";
        result.sections = prose
          ? [section("", prose, "markdown")]
          : [
              section(
                "",
                descriptor.status === "running"
                  ? "Preparing the next action…"
                  : "Action prepared. Execution appears separately below.",
              ),
            ];
        if (data.error) result.sections.push(...usefulFields(data.error));
        return result;
      },
    )
    .register(
      ({ name }) =>
        /^(shell_execute|process_execute|exec_command|run_command)$/.test(name),
      (args) => {
        const result = base(args),
          { input, output, workspace } = args;
        result.kind = "command";
        result.title = "Run command";
        result.subject = compact(
          Array.isArray(input.command)
            ? input.command.join(" ")
            : input.command || input.cmd,
        );
        if (output?.exit_code != null)
          result.outcome = `${result.state === "failed" ? "Failed · " : ""}exit ${output.exit_code}`;
        result.subject ||= compact(
          output?.command ||
            (Array.isArray(output?.argv) ? output.argv.join(" ") : ""),
        );
        result.sections = [
          section(
            "Command",
            Array.isArray(input.command)
              ? input.command.join(" ")
              : input.command || input.cmd || output?.command,
            "code",
          ),
        ];
        if (input.cwd || output?.cwd)
          result.sections.push(
            section("Directory", relative(input.cwd || output.cwd, workspace)),
          );
        for (const key of ["stdout", "stderr"])
          if (output?.[key])
            result.sections.push(section(key, output[key], "code"));
        if (typeof output === "string")
          result.sections.push(section("Output", output, "code"));
        if (output?.timed_out)
          result.sections.push(section("Status", "Timed out"));
        if (output?.output_truncated)
          result.sections.push(
            section("Notice", "Recorded output was truncated."),
          );
        if (output?.exit_code != null)
          result.sections.push(section("Exit code", String(output.exit_code)));
        return result;
      },
    )
    .register(
      ({ name }) => /^(read_file|read_text_file)$/.test(name),
      (args) => {
        const result = base(args),
          { input, output } = args;
        result.kind = "read";
        result.title = "Read file";
        const content = typeof output === "string" ? output : output?.content;
        if (typeof content === "string" && result.state === "completed")
          result.outcome = `${content.split("\n").length} lines${output?.truncated ? " · truncated" : ""}`;
        result.sections = [section("File", result.subject)];
        if (content != null)
          result.sections.push({
            ...section("Content", content, "lines"),
            start: output?.start_line || input.start_line || 1,
          });
        return result;
      },
    )
    .register(
      ({ name }) => /^(edit_file|write_file|apply_patch)$/.test(name),
      (args) => {
        const result = base(args),
          { input, output } = args;
        result.kind = "edit";
        result.title = args.name === "write_file" ? "Write file" : "Edit file";
        if (output?.replacements != null && result.state === "completed")
          result.outcome = `${output.replacements} replacements`;
        result.sections = [section("File", result.subject)];
        if (output?.diff || input.patch)
          result.sections.push(
            section("Changes", output?.diff || input.patch, "diff"),
          );
        else if (input.old_text != null && input.new_text != null) {
          result.sections.push(
            section("Replace", input.old_text, "code"),
            section("With", input.new_text, "code"),
          );
        } else if (input.content != null)
          result.sections.push(section("Content", input.content, "code"));
        if (output?.diff_truncated)
          result.sections.push(
            section("Notice", "Recorded diff was truncated."),
          );
        if (output?.first_changed_line)
          result.sections.push(
            section("First changed line", String(output.first_changed_line)),
          );
        return result;
      },
    )
    .register(
      ({ name }) =>
        /^(search_files|search_code|grep|glob|file_search|web_search|search)$/.test(
          name,
        ),
      (args) => {
        const result = base(args),
          { input, output } = args;
        result.kind = "search";
        result.title = "Search";
        result.subject = compact(input.query || input.pattern || input.path);
        const hits =
          output?.matches ||
          output?.results ||
          (Array.isArray(output) ? output : null);
        if (Array.isArray(hits)) {
          result.outcome = `${hits.length} recorded matches${output?.truncated ? " · truncated" : ""}`;
          result.sections = [section("Matches", hits, "hits")];
        } else
          result.sections = [
            section(
              "Result",
              text(output) || output?.content || output?.stdout,
              "code",
            ),
          ];
        if (output?.status === "no_match") result.outcome = "No matches";
        return result;
      },
    )
    .register(
      ({ name }) => /^(fetch_url|web_fetch|browse_url)$/.test(name),
      (args) => {
        const result = base(args),
          { input, output } = args;
        result.title = "Read page";
        result.sections = [
          section("Source", input.url, "link"),
          section(
            "",
            typeof output === "string"
              ? output
              : output?.content || output?.text || output?.markdown,
            "markdown",
          ),
        ];
        if (output?.title) result.subject = output.title;
        return result;
      },
    )
    .register(
      ({ name }) => name === "finish",
      (args) => {
        const result = base(args);
        result.title = "Prepare answer";
        result.subject = "";
        result.sections = [
          section("", "The final answer is displayed in the conversation."),
        ];
        return result;
      },
    )
    .register(
      ({ descriptor, name }) =>
        descriptor.kind === "plan" ||
        /^(enter_plan|submit_plan|update_plan)$/.test(name),
      (args) => {
        const result = base(args),
          { data, output, input } = args;
        const plan = output?.plan || data.plan || input.plan || output || input;
        result.kind = "plan";
        result.title =
          args.descriptor.kind === "plan" ? "Current plan" : "Update plan";
        const items = plan?.items || data.items || input.items || [];
        result.subject = items.length
          ? `${items.filter((item) => item.status === "completed").length}/${items.length} steps completed`
          : "";
        result.sections = [
          section(
            "Plan",
            plan?.items || data.items || input.items || [],
            "plan",
          ),
        ];
        return result;
      },
    );
}
