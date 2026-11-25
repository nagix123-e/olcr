import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from olcr_api import plamo_translation


def test_auto_source_language_is_sent_to_each_plamo_phase(tmp_path):
    checkpoint = tmp_path / "model"
    checkpoint.mkdir()
    (checkpoint / "config.json").write_text("{}", encoding="utf-8")
    python = tmp_path / "runtime" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    progress = []
    server = SimpleNamespace(poll=lambda: None)
    client_result = subprocess.CompletedProcess([], 0, '["translated"]', "")

    with (
        patch.object(plamo_translation, "runtime_available", return_value=True),
        patch.object(plamo_translation.subprocess, "Popen", return_value=server) as start_server,
        patch.object(plamo_translation, "_wait_for_local_server"),
        patch.object(plamo_translation.subprocess, "run", return_value=client_result) as run_client,
        patch.object(plamo_translation, "_stop_server"),
        patch.object(plamo_translation.time, "sleep"),
    ):
        outputs = plamo_translation.translate_many(
            ["私は長い間街を歩いていた。"], "auto", "en", checkpoint,
            python_path=python,
            on_progress=lambda state, index, total, output=None:
                progress.append((state, index, total, output)),
        )

    payload = json.loads(run_client.call_args.kwargs["input"])
    server_environment = start_server.call_args.kwargs["env"]
    assert payload["items"][0]["source"] == "ja"
    assert server_environment["PLAMO_TRANSLATE_CLI_REPETITION_PENALTY"] == "1.05"
    assert server_environment["PLAMO_TRANSLATE_CLI_REPETITION_CONTEXT_SIZE"] == "128"
    assert outputs == ["translated"]
    assert ("source_detected", 1, 1, "ja") in progress
