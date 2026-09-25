"""Fine-tuning starts billing only after the Axolotl Jupyter port accepts connections."""
import os
import socket
from unittest.mock import Mock, patch

os.environ.setdefault("PETABYTE_API_URL", "http://localhost")
os.environ.setdefault("PETABYTE_API_KEY", "test")
os.environ.setdefault("PETABYTE_SPEC_ID", "1")
import task_fetcher as tf  # noqa: E402


with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    assert tf._wait_for_template_port(listener.getsockname()[1], "unused", timeout_s=1)

with patch("socket.create_connection", side_effect=ConnectionRefusedError), \
     patch("subprocess.run", return_value=Mock(returncode=0, stdout="false\n")) as inspect:
    assert not tf._wait_for_template_port(8888, "stopped-axolotl", timeout_s=1)
    assert inspect.call_args.args[0] == ["docker", "inspect", "-f", "{{.State.Running}}",
                                         "stopped-axolotl"]

print("OK — Jupyter readiness is checked before the fine-tuning VM becomes billable")
