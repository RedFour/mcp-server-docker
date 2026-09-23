import struct
from typing import Any, ClassVar

import docker
import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError

import mcp_server_docker.server as server_module
from mcp_server_docker.output_schemas import (
    demux_docker_stream,
    format_ports,
)
from mcp_server_docker.server import app, format_docker_error, resolve_docker_client


class ExecResult:
    def __init__(self, exit_code: int = 0, output: Any = (b"output\n", b"")):
        self.exit_code = exit_code
        self.output = output


class Object:
    id = "object-id"
    name = "object-name"
    short_id = "object-id"
    status = "running"
    image = None
    ports: ClassVar = {}
    attrs: ClassVar = {
        "Config": {"Image": "alpine:latest"},
        "State": {"Status": "running"},
        "NetworkSettings": {"Ports": {}},
    }

    def __init__(self, calls: list[tuple[str, Any]]):
        self.calls = calls

    def logs(self, **kwargs):
        self.calls.append(("object.logs", kwargs))
        return b"line\n"

    def stats(self, **kwargs):
        self.calls.append(("object.stats", kwargs))
        return {"cpu": 1}

    def start(self):
        self.calls.append(("object.start", None))

    def stop(self, **kwargs):
        self.calls.append(("object.stop", kwargs))

    def restart(self, **kwargs):
        self.calls.append(("object.restart", kwargs))

    def remove(self, **kwargs):
        self.calls.append(("object.remove", kwargs))

    def exec_run(self, **kwargs):
        self.calls.append(("object.exec_run", kwargs))
        return ExecResult(exit_code=0, output=(b"hello from container\n", b""))


class Collection:
    def __init__(self, calls: list[tuple[str, Any]], name: str):
        self.calls, self.name = calls, name

    def list(self, **kwargs):
        self.calls.append((f"{self.name}.list", kwargs))
        return []

    def get(self, name):
        self.calls.append((f"{self.name}.get", name))
        return Object(self.calls)

    def create(self, **kwargs):
        self.calls.append((f"{self.name}.create", kwargs))
        return Object(self.calls)

    def run(self, **kwargs):
        self.calls.append((f"{self.name}.run", kwargs))
        return Object(self.calls)


class Images(Collection):
    def pull(self, repository, **kwargs):
        self.calls.append(("images.pull", (repository, kwargs)))
        return Object(self.calls)

    def push(self, repository, **kwargs):
        self.calls.append(("images.push", (repository, kwargs)))
        return []

    def build(self, **kwargs):
        self.calls.append(("images.build", kwargs))
        return Object(self.calls), [{"stream": "built"}]

    def remove(self, **kwargs):
        self.calls.append(("images.remove", kwargs))


class Docker:
    def __init__(self):
        self.calls: list[tuple[str, Any]] = []
        self.containers = Collection(self.calls, "containers")
        self.images = Images(self.calls, "images")
        self.networks = Collection(self.calls, "networks")
        self.volumes = Collection(self.calls, "volumes")
        self.closed = False

    def close(self):
        self.closed = True

    def version(self):
        self.calls.append(("docker.version", None))
        return {
            "Version": "27.0.0",
            "ApiVersion": "1.46",
            "Os": "linux",
            "Arch": "arm64",
            "KernelVersion": "6.6.0",
        }

    def info(self):
        self.calls.append(("docker.info", None))
        return {
            "Containers": 5,
            "ContainersRunning": 2,
            "ContainersPaused": 0,
            "ContainersStopped": 3,
            "Images": 10,
            "NCPU": 8,
            "MemTotal": 8589934592,
            "OperatingSystem": "Docker Desktop",
            "Architecture": "aarch64",
        }

    def call(self, name: str) -> Any:
        return next(value for key, value in reversed(self.calls) if key == name)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def docker_client(monkeypatch):
    client = Docker()
    monkeypatch.setattr(
        server_module,
        "docker_to_dict",
        lambda _obj, overrides=None: {
            "id": "object-id",
            "short_id": "object-id",
            "name": "object-name",
            "image_name": "alpine:latest",
            "state": "running",
            "status": "Up 2 hours",
            "ports_formatted": "none",
            "size_formatted": "5.2 MB",
            "created": "2026-09-01",
            **(overrides or {}),
        },
    )
    monkeypatch.setattr(server_module.docker, "from_env", lambda: client)
    yield client
    assert client.closed


@pytest.fixture
def server(docker_client):
    return app


@pytest.mark.anyio
async def test_tools_have_flat_schemas_and_container_call_semantics(
    server, docker_client
):
    async with Client(server, raise_exceptions=True) as client:
        tools = await client.list_tools()
        # 19 legacy tools + 6 standardized tools = 25 tools
        assert len(tools.tools) == 25
        create = next(tool for tool in tools.tools if tool.name == "create_container")
        assert create.input_schema["required"] == ["image"]
        assert "ctx" not in create.input_schema["properties"]
        assert "input" not in create.input_schema["properties"]
        assert (
            create.input_schema["properties"]["image"]["description"]
            == "Docker image name"
        )
        annotations = next(
            tool for tool in tools.tools if tool.name == "list_containers"
        ).annotations
        assert annotations.read_only_hint and annotations.idempotent_hint
        assert annotations.open_world_hint is False
        remove = next(tool for tool in tools.tools if tool.name == "remove_container")
        assert remove.annotations.destructive_hint
        assert remove.annotations.open_world_hint is False

        await client.call_tool("list_containers", {})
        assert docker_client.call("containers.list") == {"all": False, "filters": None}
        await client.call_tool(
            "list_containers", {"all": True, "filters": {"label": ["a=b"]}}
        )
        assert docker_client.call("containers.list") == {
            "all": True,
            "filters": {"label": ["a=b"]},
        }
        await client.call_tool(
            "create_container",
            {"image": "alpine", "name": "new", "environment": {"X": "1"}},
        )
        assert docker_client.call("containers.create")["image"] == "alpine"
        assert docker_client.call("containers.create")["environment"] == {"X": "1"}
        await client.call_tool("run_container", {"image": "alpine", "detach": False})
        assert docker_client.call("containers.run")["detach"] is False
        await client.call_tool("recreate_container", {"image": "alpine", "name": "old"})
        assert docker_client.call("containers.get") == "old"
        assert [name for name, _ in docker_client.calls[-4:]] == [
            "containers.get",
            "object.stop",
            "object.remove",
            "containers.run",
        ]
        recreated = docker_client.call("containers.run")
        assert (
            "container_id" not in recreated and "resolved_container_id" not in recreated
        )
        await client.call_tool("start_container", {"container_id": "x"})
        assert docker_client.calls[-2:] == [
            ("containers.get", "x"),
            ("object.start", None),
        ]
        await client.call_tool("fetch_container_logs", {"container_id": "x"})
        assert docker_client.call("object.logs") == {"tail": 100}
        await client.call_tool(
            "fetch_container_logs", {"container_id": "x", "tail": "all"}
        )
        assert docker_client.call("object.logs") == {"tail": "all"}
        await client.call_tool("stop_container", {"container_id": "x"})
        assert docker_client.calls[-2:] == [
            ("containers.get", "x"),
            ("object.stop", {}),
        ]
        await client.call_tool("remove_container", {"container_id": "x", "force": True})
        assert docker_client.call("object.remove") == {"force": True}


@pytest.mark.anyio
async def test_standardized_tools(server, docker_client):
    async with Client(server, raise_exceptions=True) as client:
        # docker_list_containers
        res = await client.call_tool("docker_list_containers", {"all": True})
        assert not res.is_error
        assert docker_client.call("containers.list") == {"all": True}

        # docker_container_logs
        res = await client.call_tool(
            "docker_container_logs", {"id": "c1", "tail": 50, "timestamps": True}
        )
        assert not res.is_error
        assert docker_client.call("object.logs") == {
            "stdout": True,
            "stderr": True,
            "stream": False,
            "tail": 50,
            "timestamps": True,
        }

        # docker_container_action: start
        res = await client.call_tool(
            "docker_container_action", {"id": "c1", "action": "start"}
        )
        assert not res.is_error
        assert docker_client.calls[-1] == ("object.start", None)

        # docker_container_action: stop
        res = await client.call_tool(
            "docker_container_action", {"id": "c1", "action": "stop", "timeout": 5}
        )
        assert not res.is_error
        assert docker_client.calls[-1] == ("object.stop", {"timeout": 5})

        # docker_container_action: restart
        res = await client.call_tool(
            "docker_container_action", {"id": "c1", "action": "restart", "timeout": 10}
        )
        assert not res.is_error
        assert docker_client.calls[-1] == ("object.restart", {"timeout": 10})

        # docker_container_action: remove
        res = await client.call_tool(
            "docker_container_action", {"id": "c1", "action": "remove", "force": True}
        )
        assert not res.is_error
        assert docker_client.calls[-1] == ("object.remove", {"force": True})

        # docker_exec
        res = await client.call_tool(
            "docker_exec",
            {"id": "c1", "command": ["echo", "hi"], "working_dir": "/app"},
        )
        assert not res.is_error
        assert docker_client.call("object.exec_run") == {
            "cmd": ["echo", "hi"],
            "workdir": "/app",
            "demux": True,
        }

        # docker_list_images
        res = await client.call_tool("docker_list_images", {"all": False})
        assert not res.is_error
        assert docker_client.call("images.list") == {"all": False}

        # docker_system_info
        res = await client.call_tool("docker_system_info", {})
        assert not res.is_error
        assert docker_client.call("docker.version") is None
        assert docker_client.call("docker.info") is None


@pytest.mark.anyio
async def test_image_network_and_volume_call_semantics(server, docker_client):
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool("list_images", {})
        assert docker_client.call("images.list") == {
            "name": None,
            "all": False,
            "filters": None,
        }
        await client.call_tool(
            "list_images",
            {"name": "alpine", "all": True, "filters": {"dangling": True}},
        )
        assert docker_client.call("images.list") == {
            "name": "alpine",
            "all": True,
            "filters": {"dangling": True, "label": None},
        }
        await client.call_tool("pull_image", {"repository": "alpine", "tag": "3.20"})
        assert docker_client.call("images.pull") == ("alpine", {"tag": "3.20"})
        await client.call_tool("push_image", {"repository": "alpine"})
        assert docker_client.call("images.push") == ("alpine", {"tag": "latest"})
        build = await client.call_tool(
            "build_image", {"path": ".", "tag": "test", "dockerfile": "Dockerfile.test"}
        )
        assert build.structured_content["logs"] == [{"stream": "built"}]
        assert docker_client.call("images.build") == {
            "path": ".",
            "tag": "test",
            "dockerfile": "Dockerfile.test",
        }
        await client.call_tool("remove_image", {"image": "test", "force": True})
        assert docker_client.call("images.remove") == {"image": "test", "force": True}
        await client.call_tool("list_networks", {"filters": {"label": ["x=y"]}})
        assert docker_client.call("networks.list") == {"filters": {"label": ["x=y"]}}
        await client.call_tool("create_network", {"name": "n", "internal": True})
        assert docker_client.call("networks.create") == {
            "name": "n",
            "driver": "bridge",
            "internal": True,
            "labels": None,
        }
        await client.call_tool("remove_network", {"network_id": "n"})
        assert docker_client.calls[-2:] == [
            ("networks.get", "n"),
            ("object.remove", {}),
        ]
        await client.call_tool("list_volumes", {})
        assert docker_client.call("volumes.list") == {}
        await client.call_tool("create_volume", {"name": "v"})
        assert docker_client.call("volumes.create") == {
            "name": "v",
            "driver": "local",
            "labels": None,
        }
        await client.call_tool("remove_volume", {"volume_name": "v", "force": True})
        assert docker_client.call("object.remove") == {"force": True}


@pytest.mark.anyio
async def test_resources_prompt_validation_errors_and_lifetime(server, docker_client):
    async with Client(server, raise_exceptions=True) as client:
        templates = await client.list_resource_templates()
        assert {template.uri_template for template in templates.resource_templates} == {
            "docker://containers/{container_id}/logs",
            "docker://containers/{container_id}/stats",
        }
        logs = (await client.read_resource("docker://containers/x/logs")).contents[0]
        stats = (await client.read_resource("docker://containers/x/stats")).contents[0]
        assert (logs.mime_type, logs.text) == ("text/plain", "line\n")
        assert stats.mime_type == "application/json"
        assert '"cpu": 1' in stats.text
        before = len(docker_client.calls)
        assert (await client.call_tool("create_container", {})).is_error
        assert len(docker_client.calls) == before
        prompts = await client.list_prompts()
        assert all(argument.required for argument in prompts.prompts[0].arguments)
        with pytest.raises(MCPError, match="Missing required arguments"):
            await client.get_prompt("docker_compose", {"name": "p"})
        prompt = await client.get_prompt(
            "docker_compose", {"name": "p", "containers": "nginx"}
        )
        assert "mcp-server-docker.project=p" in prompt.messages[0].content.text
        assert "<BEGIN FORMAT>" in prompt.messages[0].content.text
    assert docker_client.closed


@pytest.mark.anyio
async def test_docker_exception_is_a_model_visible_tool_error(server, docker_client):
    def fail(**_kwargs):
        raise docker.errors.APIError("daemon unavailable")

    docker_client.containers.list = fail
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_containers", {})
    assert result.is_error
    assert "daemon unavailable" in result.content[0].text


@pytest.mark.anyio
async def test_internally_created_client_is_closed(monkeypatch):
    client = Docker()
    monkeypatch.setattr(server_module.docker, "from_env", lambda: client)
    async with Client(app, raise_exceptions=True) as mcp_client:
        await mcp_client.list_tools()
    assert client.closed


@pytest.mark.anyio
async def test_production_stdio_command_lists_tools():
    params = StdioServerParameters(command="uv", args=["run", "mcp-server-docker"])
    async with Client(stdio_client(params), raise_exceptions=True) as client:
        tools = await client.list_tools()
    assert "list_containers" in {tool.name for tool in tools.tools}
    assert "docker_list_containers" in {tool.name for tool in tools.tools}


# ---------------------------------------------------------------------------
# Unit tests for format_ports, demux_docker_stream, format_docker_error
# ---------------------------------------------------------------------------


def test_format_ports_null_and_empty():
    assert format_ports(None) == "none"
    assert format_ports([]) == "none"
    assert format_ports({}) == "none"
    assert format_ports({"80/tcp": None}) == "80/tcp"
    assert format_ports({"80/tcp": []}) == "80/tcp"


def test_format_ports_dict_and_list():
    port_dict = {
        "80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}],
        "443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8443"}],
    }
    res = format_ports(port_dict)
    assert "8080->80/tcp" in res
    assert "127.0.0.1:8443->443/tcp" in res

    port_list = [
        {"PrivatePort": 80, "PublicPort": 8080, "Type": "tcp", "IP": "0.0.0.0"},
        {"PrivatePort": 3000, "Type": "tcp"},
    ]
    res_list = format_ports(port_list)
    assert "8080->80/tcp" in res_list
    assert "3000/tcp" in res_list


def test_demux_docker_stream_plain_and_tuple():
    assert demux_docker_stream(b"hello world") == ("hello world", "")
    assert demux_docker_stream((b"stdout", b"stderr")) == ("stdout", "stderr")
    assert demux_docker_stream(None) == ("", "")


def test_demux_docker_stream_multiplexed_frames():
    # Stream type 1 = stdout, stream type 2 = stderr
    stdout_payload = b"hello stdout\n"
    stderr_payload = b"hello stderr error\n"

    frame1 = struct.pack(">BxxxI", 1, len(stdout_payload)) + stdout_payload
    frame2 = struct.pack(">BxxxI", 2, len(stderr_payload)) + stderr_payload
    multiplexed_bytes = frame1 + frame2

    out, err = demux_docker_stream(multiplexed_bytes)
    assert out == "hello stdout\n"
    assert err == "hello stderr error\n"


def test_format_docker_error():
    perm_err = PermissionError("Permission denied: '/var/run/docker.sock'")
    msg = format_docker_error(perm_err, "/var/run/docker.sock")
    assert "permission denied" in msg.lower()
    assert "Docker socket" in msg

    conn_err = ConnectionError("connect ENOENT /var/run/docker.sock")
    msg2 = format_docker_error(conn_err, "/var/run/docker.sock")
    assert "unreachable" in msg2.lower()
    assert "Docker Desktop" in msg2


def test_resolve_docker_client_env(monkeypatch):
    monkeypatch.setenv("DOCKER_SOCKET_PATH", "/tmp/fake-socket.sock")
    _client, path, _err = resolve_docker_client()
    assert path == "/tmp/fake-socket.sock"
