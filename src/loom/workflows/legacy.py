"""Compatibility adapter for existing planning and route checkpoints."""

from dataclasses import replace

from loom.runtime import PlanningRuntime, plan_state_dict, workflow_route_state_dict
from loom.runtime.plugin_contracts import PluginManifest


class LegacyPlanningWorkflow(PlanningRuntime):
    manifest = PluginManifest("legacy_planning", "workflow")

    def __init__(self, config):
        if set(config) - {"mode"}:
            raise ValueError("Unknown legacy workflow configuration")
        self.config = dict(config)
        super().__init__(config.get("mode", "auto"))

    def plugin_snapshot(self):
        return self.manifest.state(self.config, self.snapshot())

    def restore_plugin(self, snapshot):
        self.restore(self.manifest.restore(self.config, snapshot)).unwrap()

    def project_state(self, context):
        scratch = dict(context.state.scratch or {})
        scratch.update(plan=plan_state_dict(self.controller.state), workflowRoute=workflow_route_state_dict(self.route.state))
        return replace(context, state=replace(context.state, scratch=scratch))

    def unfinished(self):
        return self.controller.state.phase.value in {"planning", "executing"}
