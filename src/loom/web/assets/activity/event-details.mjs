const internal = /^(?:id|.*_id|.*_ids|.*_digest|.*_sha256|sha256|fingerprint|at|timestamp|type|schema(?:_version)?|revision|.*_revision|metadata|usage|binding|artifact|messages|tools|view|failed)$/;
const priority = ["description", "objective", "summary", "reason", "validation_error", "error", "message", "content", "text", "proposal", "criteria", "requirements", "unresolved_requirements", "limitations", "plan", "workflow", "result", "results", "scope", "path", "status", "state"];
const label = value => value.replace(/_/g, " ");

/** Include nested user-facing facts; transport identifiers belong in raw records. */
export function meaningfulFields(value) {
  const sections = [];
  let shortened = false;
  function walk(value, path = "", depth = 0) {
    if (value == null || value === "") return;
    if (sections.length >= 40 || depth > 7) {shortened = true; return;}
    if (typeof value !== "object") {
      const text = String(value);
      sections.push({label: path, value: text.slice(0, 6000)});
      if (text.length > 6000) shortened = true;
    } else if (Array.isArray(value)) {
      value.slice(0, 20).forEach((entry, i) => walk(entry, `${path} ${i + 1}`.trim(), depth + 1));
      if (value.length > 20) shortened = true;
    } else {
      const rank = key => priority.includes(key) ? priority.indexOf(key) : priority.length;
      const entries = Object.entries(value).filter(([key]) => !internal.test(key));
      entries.sort(([a], [b]) => rank(a) - rank(b));
      for (const [key, entry] of entries)
        walk(entry, [path, label(key)].filter(Boolean).join(" · "), depth + 1);
    }
  }
  walk(value);
  if (shortened) sections.push({label: "More details", value: "Preview shortened. Open raw records for the full content."});
  return sections;
}

export function eventPresentation(kind, data) {
  if (kind?.startsWith("acceptance.plan.")) {
    // A proposed plan is not yet accepted; the snapshot may still contain an older plan.
    const plan = kind === "acceptance.plan.proposed" ? data.proposal : data.acceptance?.plan;
    const titles = {proposed: "Proposed acceptance plan", accepted: "Acceptance plan ready", revised: "Acceptance plan revised",
      rejected: "Acceptance proposal rejected", repairing: "Correcting acceptance plan"};
    const criteria = Array.isArray(plan?.criteria) ? plan.criteria.filter(c => c && typeof c === "object") : [];
    return {
      title: titles[kind.split(".").at(-1)] || "Acceptance plan",
      subject: criteria.length ? `${criteria.length} acceptance criteria` : "",
      sections: [
        ...(data.validation_error ? [{label: "Problem", value: data.validation_error}] : []),
        ...criteria.map((c, i) => ({label: `Criterion ${i + 1}`, kind: "acceptance-criterion", value: c})),
        ...meaningfulFields({unresolved_requirements: plan?.unresolved_requirements, reason: plan?.revision_reason || data.acceptance?.reason}),
      ],
    };
  }
  if (kind === "workspace.probed") {
    const profile = data.profile || {};
    return {title: "Workspace inspected", subject: `${profile.files?.length || 0} discovered entries`,
      sections: meaningfulFields({workspace: profile.workspace, limitations: profile.limitations,
        configuration: profile.configuration, files: profile.files})};
  }
  if (kind?.startsWith("workflow.node.")) {
    const workflow = data.workflow || {};
    const nodes = workflow.workflow?.nodes || [];
    const active = nodes.find(n => n.id === workflow.active);
    return {title: kind.replaceAll(".", " "), subject: active?.objective || "",
      sections: meaningfulFields(active || {plan: data.plan})};
  }
  if (kind === "artifact.created") {
    const artifact = data.artifact || {};
    if (["verification_evidence", "verification"].includes(artifact.kind)) {
      const value = data.artifact_content;
      return {title: artifact.kind === "verification_evidence" ? "Verification evidence saved" : "Verification check saved",
        subject: value ? `${value.plan?.criteria?.length || (value.criterion ? 1 : 0)} checks · ${value.evidence?.length || 0} evidence items` : "Expand to view checks and supporting evidence",
        sections: value ? verificationArtifactSections(value) || meaningfulFields(value) : [{label: "", value: "Verification content loads when expanded."}]};
    }
    return {title: "Artifact saved", subject: artifact.kind || "",
      sections: meaningfulFields({path: artifact.relative_path || artifact.local_path, kind: artifact.kind, byte_size: artifact.byte_size})};
  }
  return null;
}

export function verificationArtifactSections(value) {
  if (!value || !((value.plan && Array.isArray(value.evidence)) || (value.criterion && value.result))) return null;
  const sections = [{label: "", value: value.criterion ? "Recorded verification check and supporting evidence." :
    "Evidence supplied to the acceptance review. This bundle is not the final acceptance verdict."}];
  sections.push(...meaningfulFields({goal: value.goal}));
  const criteria = value.plan?.criteria || [value.criterion];
  criteria.filter(Boolean).forEach((c, i) => sections.push({label: `Criterion ${i + 1}`, kind: "acceptance-criterion", value: c}));
  sections.push(...meaningfulFields({result: value.result, detail: value.detail, unresolved_requirements: value.plan?.unresolved_requirements}));
  for (const [i, evidence] of (value.evidence || []).entries()) {
    const label = evidence.id === "candidate" ? "Candidate answer under review" : `Evidence ${i + 1}${evidence.tool ? ` · ${evidence.tool}` : ""}`;
    if (evidence.content) sections.push({label, value: evidence.content, kind: "markdown"});
    if (evidence.input) sections.push(...meaningfulFields({[label]: {input: evidence.input}}));
    if (evidence.output != null) {
      const output = outputValue(decode(evidence.output));
      if (typeof output === "string" && /^\s*[{[]/.test(output)) {
        sections.push({label, value: "The recorded structured output is incomplete. Open raw records for the available fragment."});
      } else if (output && typeof output === "object" && ("stdout" in output || "stderr" in output)) {
        for (const key of ["stdout", "stderr"])
          if (output[key]) sections.push({label: `${label} · ${key}`, value: output[key], kind: "code"});
        if (output.exit_code != null) sections.push({label: `${label} · Exit code`, value: String(output.exit_code)});
      } else sections.push(...meaningfulFields({[label]: {output}}));
    }
    sections.push(...meaningfulFields({[label]: {result: evidence.result, detail: evidence.detail}}));
    if (evidence.truncated) sections.push({label, value: "Recorded output was truncated; omitted content was not included in this evidence."});
  }
  return sections;
}
import { decode, outputValue } from "./content.mjs";
