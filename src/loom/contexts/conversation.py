"""Opt-in persistent working conversation; continuation still belongs to a step."""


class ConversationContext:
    def __init__(self, manager, workflow):
        self.manager, self.workflow = manager, workflow

    def __getattr__(self, name):
        return getattr(self.manager, name)

    def project(self, context, **options):
        return self.workflow.project_messages(context, self.manager.project(context, **options))

    def compact(self, *args, **kwargs):
        messages, event = self.manager.compact(*args, **kwargs)
        self.workflow.capture_messages(messages)
        return messages, event
