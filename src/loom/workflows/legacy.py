"""Compatibility adapter for existing planning and route checkpoints."""

from dataclasses import replace

from loom.runtime import PlanningRuntime, plan_state_dict, workflow_route_state_dict
from loom.runtime.plugin_contracts import PluginManifest
from loom.runtime.workflow_routing import WorkflowRoutePolicy


class LegacyPlanningWorkflow(PlanningRuntime):
    manifest = PluginManifest("legacy_planning", "workflow")

    def __init__(self, config):
        if set(config) - {"mode", "progress_review"}:
            raise ValueError("Unknown legacy workflow configuration")
        self.config = dict(config)
        super().__init__(config.get("mode", "auto"), route_policy=WorkflowRoutePolicy(**config.get("progress_review", {})))
        self._feedback_applications = {}

    def snapshot(self):
        return {**super().snapshot(), "feedback_applications": dict(self._feedback_applications)}

    def restore(self, value):
        result = super().restore(value)
        if result.ok:
            self._feedback_applications = dict(value.get("feedback_applications", {}))
        return result

    def apply_task_feedback(self, feedback):
        from loom.tasks.acceptance_feedback import feedback_objective

        identifier = feedback["id"]
        if identifier in self._feedback_applications:
            return self._feedback_applications[identifier]
        self.controller.reopen_for_acceptance(feedback_objective(feedback), "repair-" + identifier.split(":")[-1][:20])
        state = self.controller.state
        application = {"feedback_id": identifier, "revision": state.revision,
                       "step_ids": [i.id for i in state.items if i.status.value in {"pending", "in_progress"}]}
        self._feedback_applications[identifier] = application
        return application

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

    def reopen_for_acceptance(self):
        self.controller.reopen_for_acceptance()
