import pytest

from loom.core import Observation, StateLayer
from loom.llm.api import LlmMessage, LlmToolCall
from loom.runtime.checkpoints import decode, encode
from loom.runtime.control import StepControl
from loom.tasks.request import TaskRequest
from loom.tasks.runner import make_task_context


def test_checkpoint_round_trip_keeps_context_messages_and_user_keys(tmp_path):
    context = make_task_context(TaskRequest("Maintain", workspace=tmp_path)).unwrap()
    value = {
        "context": context,
        "messages": [LlmMessage("assistant", "", tool_calls=(LlmToolCall("c", "read_file", "{}"),))],
        "observation": Observation("o", "test", {"$type": "Context", "$map": "user data"}, "now"),
        "control": StepControl("paused", "user requested"),
        "state": StateLayer(),
    }
    assert decode(encode(value)) == value


def test_checkpoint_decoder_does_not_import_arbitrary_types():
    with pytest.raises(ValueError):
        decode({"$type": "os.system", "fields": {"command": "ignored"}})
    with pytest.raises(ValueError):
        decode({"$type": "Context", "fields": {"arbitrary": 1}})
    with pytest.raises(TypeError):
        encode(lambda: None)
