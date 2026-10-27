import socket
from unittest.mock import patch

import launcher


def test_resources_are_workspace_when_not_frozen():
    assert (launcher.resource_root() / "app.py").is_file()


def test_resources_are_bundle_directory_when_frozen(tmp_path):
    with patch.object(launcher.sys, "_MEIPASS", str(tmp_path), create=True):
        assert launcher.resource_root() == tmp_path


def test_occupied_port_is_not_reused():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        occupied = listener.getsockname()[1]
        assert launcher.available_port(occupied) != occupied


def test_available_port_is_retained():
    port = launcher.available_port(0)
    assert launcher.available_port(port) == port