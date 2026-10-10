"""Independent verifier fixture for tests concerned with solver/runtime behavior.

Acceptance-specific tests supply their own failing and evidence-checking verifiers.
This fixture does not consume a scripted solver's response sequence.
"""

import json

from loom.core import ok
from loom.llm import LlmResponse


class FixtureVerifier:
    model = "fixture-independent-verifier"

    async def chat(self, messages, tools=None, cancellation=None):
        if messages[0].content.startswith("You design Loom task acceptance"):
            value = {"criteria": [], "unresolved_requirements": []}
        else:
            payload = json.loads(messages[1].content)
            value = {
                "results": [
                    {"criterion_id": c["id"], "status": "passed", "reason": "Controlled fixture oracle", "evidence_ids": ["candidate"]}
                    for c in payload["criteria"]
                ]
            }
        return ok(LlmResponse(content=json.dumps(value)))
