"""Service-owned background components, shared by HTTP and autonomous work."""

import threading


class ServiceBackground:
    def __init__(self, service):
        from loom.service.semantic import SessionSemantic
        from loom.service.skills import SessionSkills
        from loom.service.trajectory import SessionTrajectory

        self.service = service
        self.closed = False
        self.lock = threading.RLock()
        self.components = []
        try:
            self.trajectory = self.register(SessionTrajectory(service.store))
            self.semantic = self.register(SessionSemantic(self.trajectory, service.config_path, provider_factory=service.evaluation_provider_factory))
            self.skills = self.register(SessionSkills(self.trajectory, self.semantic))
            from loom.service.autonomy import AutonomyService

            self.autonomy = self.register(AutonomyService(service, self))
        except BaseException:
            self.close()
            raise

    def register(self, component):
        with self.lock:
            if self.closed:
                raise RuntimeError("Background services are closed")
            self.components.append(component)
        return component

    def configure(self, config_path):
        self.semantic.config_path = config_path

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            components = list(reversed(self.components))
        for component in components:
            component.close()
