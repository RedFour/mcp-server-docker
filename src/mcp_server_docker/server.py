"""MCPServer v2 implementation for Docker with hardened, harness-agnostic support."""

import functools
import json
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import docker
import docker.errors
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from mcp_server_docker._version import __version__
from mcp_server_docker.output_schemas import (
    demux_docker_stream,
    docker_to_dict,
    format_size,
)

# Configure logging strictly to stderr. Never write to stdout.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("mcp_server_docker")


def format_docker_error(error: Exception, socket_path: str | None = None) -> str:
    """Format Docker connection, socket, or API errors into clear, human-readable messages."""
    err_str = str(error)
    sock_info = f" (attempted socket: {socket_path})" if socket_path else ""

    if (
        isinstance(error, PermissionError)
        or "permission denied" in err_str.lower()
        or "errno 13" in err_str.lower()
    ):
        return (
            f"Docker permission denied{sock_info}. "
            "Please verify that your user has permission to access the Docker socket "
            "(e.g. check socket permissions, or ensure the MCP client process has socket access)."
        )

    if (
        isinstance(error, (FileNotFoundError, ConnectionError))
        or "connect enoent" in err_str.lower()
        or "no such file or directory" in err_str.lower()
        or "connection refused" in err_str.lower()
        or "error while fetching server api version" in err_str.lower()
        or "errno 2" in err_str.lower()
        or "errno 61" in err_str.lower()
        or "errno 111" in err_str.lower()
    ):
        return (
            f"Docker daemon is unreachable{sock_info}. "
            "Please verify that Docker Desktop (or the Docker engine) is running."
        )

    return f"Docker error: {err_str}"


def resolve_docker_client() -> tuple[docker.DockerClient | None, str, str | None]:
    """
    Auto-detect Docker socket and return (client, socket_or_host_path, error_message).
    Priority:
    1. DOCKER_SOCKET_PATH environment variable
    2. docker.from_env() (picks up DOCKER_HOST, standard env, or test monkeypatch)
    3. Auto-detected candidates:
       - /var/run/docker.sock
       - ~/.docker/run/docker.sock (macOS Docker Desktop)
       - $XDG_RUNTIME_DIR/docker.sock (Linux rootless)
    """
    # 1. Custom explicit socket path
    socket_path = os.environ.get("DOCKER_SOCKET_PATH")
    if socket_path:
        try:
            client = docker.DockerClient(base_url=f"unix://{socket_path}")
            return client, socket_path, None
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to connect to DOCKER_SOCKET_PATH %s: %s", socket_path, e)
            return None, socket_path, format_docker_error(e, socket_path)

    # 2. Try docker.from_env() (supports DOCKER_HOST and test fixtures)
    try:
        client = docker.from_env()
        # Derive socket path or host for diagnostics
        host = os.environ.get("DOCKER_HOST") or "/var/run/docker.sock"
        return client, host, None
    except Exception as env_err:  # noqa: BLE001
        logger.debug("docker.from_env() failed: %s, checking fallback sockets", env_err)

    # 3. Candidate sockets on macOS / Linux
    home = os.path.expanduser("~")
    candidates = [
        "/var/run/docker.sock",
        os.path.join(home, ".docker", "run", "docker.sock"),
    ]
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        candidates.append(os.path.join(xdg_runtime, "docker.sock"))

    chosen_candidate = None
    for candidate in candidates:
        if os.path.exists(candidate):
            try:
                client = docker.DockerClient(base_url=f"unix://{candidate}")
                return client, candidate, None
            except Exception as e:  # noqa: BLE001
                chosen_candidate = candidate
                logger.debug("Failed candidate socket %s: %s", candidate, e)

    fallback_path = chosen_candidate or "/var/run/docker.sock"
    error_msg = format_docker_error(
        FileNotFoundError(f"No active Docker socket found. Attempted {fallback_path}"),
        fallback_path,
    )
    return None, fallback_path, error_msg


@dataclass
class AppContext:
    """State made available to handlers for one running server."""

    docker: docker.DockerClient | None
    socket_path: str = "/var/run/docker.sock"
    error: str | None = None


def _client(ctx: Context[AppContext]) -> docker.DockerClient:
    app_ctx = ctx.request_context.lifespan_context
    if app_ctx.error or app_ctx.docker is None:
        raise ToolError(
            app_ctx.error
            or f"Docker daemon is unreachable (socket: {app_ctx.socket_path}). "
            "Please ensure Docker Desktop is running."
        )
    return app_ctx.docker


def handle_docker_errors(fn):
    """Decorator to catch Docker API errors and format them as clean ToolErrors."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        ctx: Context[AppContext] | None = None
        for arg in args:
            if isinstance(arg, Context):
                ctx = arg
                break
        socket_path = (
            ctx.request_context.lifespan_context.socket_path
            if ctx
            and hasattr(ctx.request_context, "lifespan_context")
            and ctx.request_context.lifespan_context
            else None
        )
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except docker.errors.NotFound as e:
            raise ToolError(f"Resource not found: {e.explanation or str(e)}") from e
        except docker.errors.APIError as e:
            raise ToolError(f"Docker API error: {e.explanation or str(e)}") from e
        except docker.errors.DockerException as e:
            raise ToolError(format_docker_error(e, socket_path)) from e
        except Exception as e:
            raise ToolError(format_docker_error(e, socket_path)) from e

    return wrapper


class ListContainersFilters(BaseModel):
    label: list[str] | None = Field(
        None, description="Filter by label, either `key` or `key=value` format"
    )


class ListImagesFilters(BaseModel):
    dangling: bool | None = Field(None, description="Show dangling images")
    label: list[str] | None = Field(
        None, description="Filter by label, either `key` or `key=value` format"
    )


class ListNetworksFilter(BaseModel):
    label: list[str] | None = Field(
        None, description="Filter by label, either `key` or `key=value` format"
    )


ContainerID = Annotated[str, Field(description="Container ID or name")]
ImageName = Annotated[str, Field(description="Docker image name")]
Detach = Annotated[bool, Field(description="Run container in the background")]
Entrypoint = Annotated[str | None, Field(description="Entrypoint to run in container")]
ContainerCommand = Annotated[
    str | None, Field(description="Command to run in container")
]
NetworkName = Annotated[
    str | None, Field(description="Network to attach the container to")
]
Environment = Annotated[
    dict[str, str] | None, Field(description="Environment variables dictionary")
]
PortBindings = Annotated[
    dict[str, int | list[int] | tuple[str, int] | None] | None,
    Field(description="Container-to-host port bindings"),
]
VolumeMappings = Annotated[
    dict[str, dict[str, str]] | list[str] | None, Field(description="Volume mappings")
]
ContainerLabels = Annotated[
    dict[str, str] | list[str] | None, Field(description="Container labels")
]
AutoRemove = Annotated[bool, Field(description="Automatically remove the container")]


@asynccontextmanager
async def lifespan(_: MCPServer[AppContext]) -> AsyncIterator[AppContext]:
    """Create and manage the Docker client with defensive error handling."""
    client, socket_path, err = resolve_docker_client()
    app_ctx = AppContext(docker=client, socket_path=socket_path, error=err)
    try:
        yield app_ctx
    finally:
        if app_ctx.docker:
            try:
                app_ctx.docker.close()
            except Exception as close_err:  # noqa: BLE001
                logger.debug("Error closing Docker client: %s", close_err)


app = MCPServer("docker-server", version=__version__, lifespan=lifespan)


# ---------------------------------------------------------------------------
# Resources & Prompts
# ---------------------------------------------------------------------------


@app.resource(
    "docker://containers/{container_id}/logs",
    name="Container logs",
    description="Live logs for a container",
    mime_type="text/plain",
)
def container_logs(container_id: str, ctx: Context) -> str:
    container = _client(ctx).containers.get(container_id)
    raw = container.logs(tail=100)
    stdout, stderr = demux_docker_stream(raw)
    return stdout or stderr or ""


@app.resource(
    "docker://containers/{container_id}/stats",
    name="Container stats",
    description="Live resource usage stats for a container",
    mime_type="application/json",
)
def container_stats(container_id: str, ctx: Context) -> dict[str, Any]:
    container = _client(ctx).containers.get(container_id)
    return container.stats(stream=False)


@app.prompt(
    name="docker_compose", description="Treat the LLM like a Docker Compose manager"
)
def docker_compose(ctx: Context, name: str, containers: str) -> str:
    client = _client(ctx)
    project_label = f"mcp-server-docker.project={name}"
    existing_containers = client.containers.list(filters={"label": project_label})
    volumes = client.volumes.list(filters={"label": project_label})
    networks = client.networks.list(filters={"label": project_label})
    return f"""
You are going to act as a Docker Compose manager, using the Docker Tools
available to you. Instead of being provided a `docker-compose.yml` file,
you will be given instructions in plain language, and interact with the
user through a plan+apply loop, akin to how Terraform operates.

Every Docker resource you create must be assigned the following label:

{project_label}

You should use this label to filter resources when possible.

Every Docker resource you create must also be prefixed with the project name, followed by a dash (`-`):

{name}-{{ResourceName}}

Here are the resources currently present in the project, based on the presence of the above label:

<BEGIN CONTAINERS>
{json.dumps([docker_to_dict(c) for c in existing_containers], indent=2)}
<END CONTAINERS>
<BEGIN VOLUMES>
{json.dumps([docker_to_dict(v) for v in volumes], indent=2)}
<END VOLUMES>
<BEGIN NETWORKS>
{json.dumps([docker_to_dict(n) for n in networks], indent=2)}
<END NETWORKS>

Do not retry the same failed action more than once. Prefer terminating your output
when presented with 3 errors in a row, and ask a clarifying question to
form better inputs or address the error.

For container images, always prefer using the `latest` image tag, unless the user specifies a tag specifically.
So if a user asks to deploy Nginx, you should pull `nginx:latest`.

Below is a description of the state of the Docker resources which the user would like you to manage:

<BEGIN DOCKER-RESOURCES>
{containers}
<END DOCKER-RESOURCES>

Respond to this message with a plan of what you will do, in the EXACT format below:

<BEGIN FORMAT>
## Introduction

I will be assisting with deploying Docker containers for project: `{name}`.

### Plan+Apply Loop

I will run in a plan+apply loop when you request changes to the project. This is
to ensure that you are aware of the changes I am about to make, and to give you
the opportunity to ask questions or make tweaks.
Instruct me to apply immediately (without confirming the plan with you) when you desire to do so.

## Commands

Instruct me with the following commands at any point:

- `help`: print this list of commands
- `apply`: apply a given plan
- `down`: stop containers in the project
- `ps`: list containers in the project
- `quiet`: turn on quiet mode (default)
- `verbose`: turn on verbose mode (I will explain a lot!)
- `destroy`: produce a plan to destroy all resources in the project

## Plan

I plan to take the following actions:

1. CREATE ...
2. READ ...
3. UPDATE ...
4. DESTROY ...
5. RECREATE ...
...
N. ...

Respond `apply` to apply this plan. Otherwise, provide feedback and I will present you with an updated plan.
<END FORMAT>

Always apply a plan in dependency order.
"""


# ---------------------------------------------------------------------------
# Standardized Docker MCP Tools
# ---------------------------------------------------------------------------


@app.tool(
    description="List all Docker containers with ID, names, image, state, status, and ports",
    annotations=ToolAnnotations(
        read_only_hint=True, idempotent_hint=True, open_world_hint=False
    ),
)
@handle_docker_errors
def docker_list_containers(
    ctx: Context[AppContext],
    all: Annotated[
        bool, Field(description="Show all containers (default shows just running)")
    ] = False,
) -> str:
    containers = _client(ctx).containers.list(all=all)
    if not containers:
        return "No containers found." + ("" if all else " (Use all=True to view stopped containers.)")

    headers = ["CONTAINER ID", "NAMES", "IMAGE", "STATE", "STATUS", "PORTS"]
    rows: list[list[str]] = []
    for c in containers:
        d = docker_to_dict(c)
        rows.append([
            d["short_id"],
            d["name"],
            d["image_name"],
            d["state"],
            d["status"],
            d["ports_formatted"],
        ])

    col_widths = [len(h) for h in headers]
    for r in rows:
        for i, val in enumerate(r):
            col_widths[i] = max(col_widths[i], len(val))

    header_line = " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    separator_line = "-|-".join("-" * col_widths[i] for i in range(len(headers)))
    row_lines = [" | ".join(r[i].ljust(col_widths[i]) for i in range(len(headers))) for r in rows]

    return f"| {header_line} |\n| {separator_line} |\n" + "\n".join(f"| {l} |" for l in row_lines)


@app.tool(
    description="Fetch and demultiplex stdout and stderr logs for a Docker container",
    annotations=ToolAnnotations(
        read_only_hint=True, idempotent_hint=True, open_world_hint=False
    ),
)
@handle_docker_errors
def docker_container_logs(
    ctx: Context[AppContext],
    id: Annotated[str, Field(description="Container ID or name")],
    tail: Annotated[
        int | Literal["all"], Field(description="Number of lines to show from the end")
    ] = 100,
    timestamps: Annotated[
        bool, Field(description="Show timestamps in logs")
    ] = False,
) -> str:
    container = _client(ctx).containers.get(id)
    raw = container.logs(
        stdout=True,
        stderr=True,
        stream=False,
        tail=tail,
        timestamps=timestamps,
    )
    stdout, stderr = demux_docker_stream(raw)
    if stdout and stderr:
        return f"=== STDOUT ===\n{stdout}\n=== STDERR ===\n{stderr}".strip()
    elif stderr and not stdout:
        return f"=== STDERR ===\n{stderr}".strip()
    elif stdout:
        return stdout.strip()
    return "(No log output)"


@app.tool(
    description="Perform a lifecycle action on a container: start, stop, restart, or remove",
    annotations=ToolAnnotations(
        destructive_hint=True, idempotent_hint=False, open_world_hint=False
    ),
)
@handle_docker_errors
def docker_container_action(
    ctx: Context[AppContext],
    id: Annotated[str, Field(description="Container ID or name")],
    action: Annotated[
        Literal["start", "stop", "restart", "remove"],
        Field(description="Action to perform: 'start', 'stop', 'restart', or 'remove'"),
    ],
    timeout: Annotated[
        int | None,
        Field(description="Timeout in seconds before stopping/restarting"),
    ] = None,
    force: Annotated[
        bool, Field(description="Force remove container (applicable to remove action)")
    ] = False,
) -> dict[str, Any]:
    container = _client(ctx).containers.get(id)
    if action == "start":
        container.start()
        return {"status": "started", "id": id}
    elif action == "stop":
        if timeout is not None:
            container.stop(timeout=timeout)
        else:
            container.stop()
        return {"status": "stopped", "id": id}
    elif action == "restart":
        if timeout is not None:
            container.restart(timeout=timeout)
        else:
            container.restart()
        return {"status": "restarted", "id": id}
    elif action == "remove":
        container.remove(force=force)
        return {"status": "removed", "id": id}
    else:
        raise ToolError(f"Unsupported action '{action}'")


@app.tool(
    description="Execute a command non-interactively inside a running container",
    annotations=ToolAnnotations(
        destructive_hint=False, idempotent_hint=False, open_world_hint=False
    ),
)
@handle_docker_errors
def docker_exec(
    ctx: Context[AppContext],
    id: Annotated[str, Field(description="Container ID or name")],
    command: Annotated[
        list[str],
        Field(description="Command and arguments to execute, e.g. ['ls', '-la']"),
    ],
    working_dir: Annotated[
        str | None,
        Field(description="Working directory inside the container"),
    ] = None,
) -> dict[str, Any]:
    container = _client(ctx).containers.get(id)
    exec_result = container.exec_run(
        cmd=command,
        workdir=working_dir,
        demux=True,
    )
    stdout, stderr = demux_docker_stream(exec_result.output)
    output_parts = [f"Exit Code: {exec_result.exit_code}"]
    if stdout:
        output_parts.append(f"STDOUT:\n{stdout}")
    if stderr:
        output_parts.append(f"STDERR:\n{stderr}")

    return {
        "exit_code": exec_result.exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "output": "\n\n".join(output_parts).strip(),
    }


@app.tool(
    description="List Docker images with ID, repository, tag, size, and created date",
    annotations=ToolAnnotations(
        read_only_hint=True, idempotent_hint=True, open_world_hint=False
    ),
)
@handle_docker_errors
def docker_list_images(
    ctx: Context[AppContext],
    all: Annotated[
        bool, Field(description="Show all images (default hides intermediate)")
    ] = False,
) -> str:
    images = _client(ctx).images.list(all=all)
    if not images:
        return "No images found."

    headers = ["IMAGE ID", "REPOSITORY", "TAG", "SIZE", "CREATED"]
    rows: list[list[str]] = []
    for img in images:
        d = docker_to_dict(img)
        tags = d.get("repo_tags") or d.get("tags") or []
        if tags:
            for t in tags:
                parts = t.rsplit(":", 1)
                repo = parts[0]
                tag = parts[1] if len(parts) > 1 else "<none>"
                rows.append([d["short_id"], repo, tag, d["size_formatted"], str(d["created"])[:19]])
        else:
            rows.append([d["short_id"], "<none>", "<none>", d["size_formatted"], str(d["created"])[:19]])

    col_widths = [len(h) for h in headers]
    for r in rows:
        for i, val in enumerate(r):
            col_widths[i] = max(col_widths[i], len(val))

    header_line = " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    separator_line = "-|-".join("-" * col_widths[i] for i in range(len(headers)))
    row_lines = [" | ".join(r[i].ljust(col_widths[i]) for i in range(len(headers))) for r in rows]

    return f"| {header_line} |\n| {separator_line} |\n" + "\n".join(f"| {l} |" for l in row_lines)


@app.tool(
    description="Quick healthcheck returning Docker version, OS, container counts, and daemon status",
    annotations=ToolAnnotations(
        read_only_hint=True, idempotent_hint=True, open_world_hint=False
    ),
)
@handle_docker_errors
def docker_system_info(ctx: Context[AppContext]) -> dict[str, Any]:
    client = _client(ctx)
    app_ctx = ctx.request_context.lifespan_context
    v = client.version() or {}
    info = client.info() or {}

    total_c = info.get("Containers", 0)
    running_c = info.get("ContainersRunning", 0)
    paused_c = info.get("ContainersPaused", 0)
    stopped_c = info.get("ContainersStopped", 0)

    return {
        "status": "connected",
        "socket_path": app_ctx.socket_path,
        "docker_version": v.get("Version", "unknown"),
        "api_version": v.get("ApiVersion", "unknown"),
        "os": f"{info.get('OperatingSystem', v.get('Os', 'unknown'))} ({info.get('Architecture', v.get('Arch', 'unknown'))})",
        "kernel_version": v.get("KernelVersion", "unknown"),
        "containers": {
            "total": total_c,
            "running": running_c,
            "paused": paused_c,
            "stopped": stopped_c,
        },
        "images": info.get("Images", 0),
        "cpus": info.get("NCPU", 0),
        "memory": format_size(info.get("MemTotal")),
    }


# ---------------------------------------------------------------------------
# Backward-Compatible Legacy Tools
# ---------------------------------------------------------------------------


@app.tool(
    description="List all Docker containers (structured output)",
    annotations=ToolAnnotations(
        read_only_hint=True, idempotent_hint=True, open_world_hint=False
    ),
)
@handle_docker_errors
def list_containers(
    ctx: Context[AppContext],
    all: Annotated[
        bool, Field(description="Show all containers (default shows just running)")
    ] = False,
    filters: Annotated[
        ListContainersFilters | None, Field(description="Filter containers")
    ] = None,
) -> list[dict[str, Any]]:
    return [
        docker_to_dict(container)
        for container in _client(ctx).containers.list(
            all=all, filters=filters.model_dump() if filters else None
        )
    ]


@app.tool(
    description="Create a new Docker container",
    annotations=ToolAnnotations(
        destructive_hint=False, idempotent_hint=False, open_world_hint=False
    ),
)
@handle_docker_errors
def create_container(
    ctx: Context[AppContext],
    image: ImageName,
    detach: Annotated[
        bool, Field(description="Run container in the background")
    ] = True,
    name: Annotated[str | None, Field(description="Container name")] = None,
    entrypoint: Annotated[
        str | None, Field(description="Entrypoint to run in container")
    ] = None,
    command: Annotated[
        str | None, Field(description="Command to run in container")
    ] = None,
    network: Annotated[
        str | None, Field(description="Network to attach the container to")
    ] = None,
    environment: Annotated[
        dict[str, str] | None, Field(description="Environment variables dictionary")
    ] = None,
    ports: Annotated[
        dict[str, int | list[int] | tuple[str, int] | None] | None,
        Field(description="Container-to-host port bindings"),
    ] = None,
    volumes: Annotated[
        dict[str, dict[str, str]] | list[str] | None,
        Field(description="Volume mappings"),
    ] = None,
    labels: Annotated[
        dict[str, str] | list[str] | None, Field(description="Container labels")
    ] = None,
    auto_remove: Annotated[
        bool, Field(description="Automatically remove the container")
    ] = False,
) -> dict[str, Any]:
    return docker_to_dict(
        _client(ctx).containers.create(
            image=image,
            detach=detach,
            name=name,
            entrypoint=entrypoint,
            command=command,
            network=network,
            environment=environment,
            ports=ports,
            volumes=volumes,
            labels=labels,
            auto_remove=auto_remove,
        )
    )


@app.tool(
    description="Run an image in a new Docker container (preferred over `create_container` + `start_container`)",
    annotations=ToolAnnotations(
        destructive_hint=False, idempotent_hint=False, open_world_hint=False
    ),
)
@handle_docker_errors
def run_container(
    ctx: Context[AppContext],
    image: ImageName,
    detach: Annotated[
        bool, Field(description="Run container in the background")
    ] = True,
    name: Annotated[str | None, Field(description="Container name")] = None,
    entrypoint: Annotated[
        str | None, Field(description="Entrypoint to run in container")
    ] = None,
    command: Annotated[
        str | None, Field(description="Command to run in container")
    ] = None,
    network: Annotated[
        str | None, Field(description="Network to attach the container to")
    ] = None,
    environment: Annotated[
        dict[str, str] | None, Field(description="Environment variables dictionary")
    ] = None,
    ports: Annotated[
        dict[str, int | list[int] | tuple[str, int] | None] | None,
        Field(description="Container-to-host port bindings"),
    ] = None,
    volumes: Annotated[
        dict[str, dict[str, str]] | list[str] | None,
        Field(description="Volume mappings"),
    ] = None,
    labels: Annotated[
        dict[str, str] | list[str] | None, Field(description="Container labels")
    ] = None,
    auto_remove: Annotated[
        bool, Field(description="Automatically remove the container")
    ] = False,
) -> dict[str, Any]:
    return docker_to_dict(
        _client(ctx).containers.run(
            image=image,
            detach=detach,
            name=name,
            entrypoint=entrypoint,
            command=command,
            network=network,
            environment=environment,
            ports=ports,
            volumes=volumes,
            labels=labels,
            auto_remove=auto_remove,
        )
    )


@app.tool(
    description="Stop and remove a container, then run a new container. Fails if the container does not exist.",
    annotations=ToolAnnotations(
        destructive_hint=True, idempotent_hint=False, open_world_hint=False
    ),
)
@handle_docker_errors
def recreate_container(
    ctx: Context[AppContext],
    image: ImageName,
    container_id: ContainerID | None = None,
    name: Annotated[str | None, Field(description="Container name")] = None,
    detach: Detach = True,
    entrypoint: Entrypoint = None,
    command: ContainerCommand = None,
    network: NetworkName = None,
    environment: Environment = None,
    ports: PortBindings = None,
    volumes: VolumeMappings = None,
    labels: ContainerLabels = None,
    auto_remove: AutoRemove = False,
) -> dict[str, Any]:
    if container_id is None and name is None:
        raise ToolError(
            "container_id or name is required for identifying the container to stop+remove"
        )
    old = _client(ctx).containers.get(container_id or name)
    old.stop()
    old.remove()
    return docker_to_dict(
        _client(ctx).containers.run(
            image=image,
            detach=detach,
            name=name,
            entrypoint=entrypoint,
            command=command,
            network=network,
            environment=environment,
            ports=ports,
            volumes=volumes,
            labels=labels,
            auto_remove=auto_remove,
        )
    )


@app.tool(
    description="Start a Docker container",
    annotations=ToolAnnotations(
        destructive_hint=False, idempotent_hint=False, open_world_hint=False
    ),
)
@handle_docker_errors
def start_container(
    ctx: Context[AppContext], container_id: ContainerID
) -> dict[str, Any]:
    container = _client(ctx).containers.get(container_id)
    container.start()
    return docker_to_dict(container)


@app.tool(
    description="Fetch logs for a Docker container (structured line list)",
    annotations=ToolAnnotations(
        read_only_hint=True, idempotent_hint=True, open_world_hint=False
    ),
)
@handle_docker_errors
def fetch_container_logs(
    ctx: Context[AppContext],
    container_id: ContainerID,
    tail: Annotated[
        int | Literal["all"],
        Field(description="Number of lines to show from the end"),
    ] = 100,
) -> dict[str, list[str]]:
    raw = _client(ctx).containers.get(container_id).logs(tail=tail)
    stdout, stderr = demux_docker_stream(raw)
    combined = (stdout or "") + (("\n" + stderr) if stderr else "")
    return {"logs": combined.split("\n")}


@app.tool(
    description="Stop a Docker container",
    annotations=ToolAnnotations(
        destructive_hint=False, idempotent_hint=False, open_world_hint=False
    ),
)
@handle_docker_errors
def stop_container(
    ctx: Context[AppContext], container_id: ContainerID
) -> dict[str, Any]:
    container = _client(ctx).containers.get(container_id)
    container.stop()
    return docker_to_dict(container)


@app.tool(
    description="Remove a Docker container",
    annotations=ToolAnnotations(
        destructive_hint=True, idempotent_hint=False, open_world_hint=False
    ),
)
@handle_docker_errors
def remove_container(
    ctx: Context[AppContext],
    container_id: ContainerID,
    force: Annotated[bool, Field(description="Force remove the container")] = False,
) -> dict[str, Any]:
    container = _client(ctx).containers.get(container_id)
    container.remove(force=force)
    return docker_to_dict(container, {"status": "removed"})


@app.tool(
    description="List Docker images (structured output)",
    annotations=ToolAnnotations(
        read_only_hint=True, idempotent_hint=True, open_world_hint=False
    ),
)
@handle_docker_errors
def list_images(
    ctx: Context[AppContext],
    name: Annotated[
        str | None, Field(description="Filter images by repository name")
    ] = None,
    all: Annotated[
        bool, Field(description="Show all images (default hides intermediate)")
    ] = False,
    filters: Annotated[
        ListImagesFilters | None, Field(description="Filter images")
    ] = None,
) -> list[dict[str, Any]]:
    return [
        docker_to_dict(image)
        for image in _client(ctx).images.list(
            name=name, all=all, filters=filters.model_dump() if filters else None
        )
    ]


@app.tool(
    description="Pull a Docker image",
    annotations=ToolAnnotations(
        destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
)
@handle_docker_errors
def pull_image(
    ctx: Context[AppContext],
    repository: Annotated[str, Field(description="Image repository")],
    tag: Annotated[str | None, Field(description="Image tag")] = "latest",
) -> dict[str, Any]:
    return docker_to_dict(_client(ctx).images.pull(repository, tag=tag))


@app.tool(
    description="Push a Docker image",
    annotations=ToolAnnotations(
        destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
)
@handle_docker_errors
def push_image(
    ctx: Context[AppContext],
    repository: Annotated[str, Field(description="Image repository")],
    tag: Annotated[str | None, Field(description="Image tag")] = "latest",
) -> dict[str, str | None]:
    _client(ctx).images.push(repository, tag=tag)
    return {"status": "pushed", "repository": repository, "tag": tag}


@app.tool(
    description="Build a Docker image from a Dockerfile",
    annotations=ToolAnnotations(
        destructive_hint=False, idempotent_hint=False, open_world_hint=False
    ),
)
@handle_docker_errors
def build_image(
    ctx: Context[AppContext],
    path: Annotated[str, Field(description="Path to build context")],
    tag: Annotated[str, Field(description="Image tag")],
    dockerfile: Annotated[str | None, Field(description="Path to Dockerfile")] = None,
) -> dict[str, Any]:
    image, logs = _client(ctx).images.build(path=path, tag=tag, dockerfile=dockerfile)
    return {"image": docker_to_dict(image), "logs": list(logs)}


@app.tool(
    description="Remove a Docker image",
    annotations=ToolAnnotations(
        destructive_hint=True, idempotent_hint=False, open_world_hint=False
    ),
)
@handle_docker_errors
def remove_image(
    ctx: Context[AppContext],
    image: Annotated[str, Field(description="Image ID or name")],
    force: Annotated[bool, Field(description="Force remove the image")] = False,
) -> dict[str, str]:
    _client(ctx).images.remove(image=image, force=force)
    return {"status": "removed", "image": image}


@app.tool(
    description="List Docker networks",
    annotations=ToolAnnotations(
        read_only_hint=True, idempotent_hint=True, open_world_hint=False
    ),
)
@handle_docker_errors
def list_networks(
    ctx: Context[AppContext],
    filters: Annotated[
        ListNetworksFilter | None, Field(description="Filter networks")
    ] = None,
) -> list[dict[str, Any]]:
    return [
        docker_to_dict(network)
        for network in _client(ctx).networks.list(
            filters=filters.model_dump() if filters else None
        )
    ]


@app.tool(
    description="Create a Docker network",
    annotations=ToolAnnotations(
        destructive_hint=False, idempotent_hint=False, open_world_hint=False
    ),
)
@handle_docker_errors
def create_network(
    ctx: Context[AppContext],
    name: Annotated[str, Field(description="Network name")],
    driver: Annotated[str | None, Field(description="Network driver")] = "bridge",
    internal: Annotated[bool, Field(description="Create an internal network")] = False,
    labels: Annotated[
        dict[str, str] | None, Field(description="Network labels")
    ] = None,
) -> dict[str, Any]:
    return docker_to_dict(
        _client(ctx).networks.create(
            name=name, driver=driver, internal=internal, labels=labels
        )
    )


@app.tool(
    description="Remove a Docker network",
    annotations=ToolAnnotations(
        destructive_hint=True, idempotent_hint=False, open_world_hint=False
    ),
)
@handle_docker_errors
def remove_network(
    ctx: Context[AppContext],
    network_id: Annotated[str, Field(description="Network ID or name")],
) -> dict[str, Any]:
    network = _client(ctx).networks.get(network_id)
    network.remove()
    return docker_to_dict(network)


@app.tool(
    description="List Docker volumes",
    annotations=ToolAnnotations(
        read_only_hint=True, idempotent_hint=True, open_world_hint=False
    ),
)
@handle_docker_errors
def list_volumes(ctx: Context[AppContext]) -> list[dict[str, Any]]:
    return [docker_to_dict(volume) for volume in _client(ctx).volumes.list()]


@app.tool(
    description="Create a Docker volume",
    annotations=ToolAnnotations(
        destructive_hint=False, idempotent_hint=False, open_world_hint=False
    ),
)
@handle_docker_errors
def create_volume(
    ctx: Context[AppContext],
    name: Annotated[str, Field(description="Volume name")],
    driver: Annotated[str | None, Field(description="Volume driver")] = "local",
    labels: Annotated[dict[str, str] | None, Field(description="Volume labels")] = None,
) -> dict[str, Any]:
    return docker_to_dict(
        _client(ctx).volumes.create(name=name, driver=driver, labels=labels)
    )


@app.tool(
    description="Remove a Docker volume",
    annotations=ToolAnnotations(
        destructive_hint=True, idempotent_hint=False, open_world_hint=False
    ),
)
@handle_docker_errors
def remove_volume(
    ctx: Context[AppContext],
    volume_name: Annotated[str, Field(description="Volume name")],
    force: Annotated[bool, Field(description="Force remove the volume")] = False,
) -> dict[str, Any]:
    volume = _client(ctx).volumes.get(volume_name)
    volume.remove(force=force)
    return docker_to_dict(volume)
