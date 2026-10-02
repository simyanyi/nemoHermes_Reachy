import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from reachy_bridge.config import Settings
from reachy_bridge.service import create_app


def test_registered_hermes_tools_to_bridge_dry_run(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "reachy_plugin", Path(__file__).parents[1] / "hermes-plugin" / "__init__.py"
    )
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    registered = {}

    class Context:
        def register_tool(self, **kw):
            registered[kw["name"]] = kw

    plugin.register(Context())
    assert set(registered) == {
        "reachy_status",
        "reachy_report",
        "reachy_event_status",
        "reachy_stop",
    }
    token = tmp_path / "token"
    token.write_text("b" * 40)
    settings = Settings(_env_file=None, token_file=token, dry_run=True)
    config = json.dumps({"base_url": "http://test", "token_file": str(token)})
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        return config if path.name == "settings.json" else original_read(path, *args, **kwargs)

    with TestClient(create_app(settings)) as client:

        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return self.body

        class Opener:
            def open(self, req, timeout):
                response = client.request(
                    req.method, req.full_url, headers=dict(req.header_items()), content=req.data
                )
                assert response.status_code < 400
                return Response(response.content)

        with (
            patch.object(Path, "read_text", read),
            patch.object(plugin, "build_opener", return_value=Opener()),
        ):
            assert json.loads(registered["reachy_status"]["handler"]({}))["robot"]["simulated"]
            payload = {"event_id": "demo-1", "severity": "warning", "summary": "Synthetic demo"}
            assert json.loads(registered["reachy_report"]["handler"](payload))["simulated"]
            # TestClient runs its own event loop; a status call gives the worker a chance to run.
            result = json.loads(
                registered["reachy_event_status"]["handler"]({"event_id": "demo-1"})
            )
            assert result["state"] == "completed"
            assert result["result"]["delivery"] == "simulated"
