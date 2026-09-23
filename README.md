# 🐋 Docker MCP Server

A production-grade, harness-agnostic [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for managing and inspecting Docker containers, images, volumes, and networks.

Compatible with any MCP-compliant client: **Google Antigravity**, **Claude Desktop**, **Cursor**, **VS Code Copilot**, **Gemini CLI**, and more.

---

## 🌟 Key Features & Hardening

- **Harness-Agnostic Stdio Compliance**: Strict separation of communication channels — all internal logging and diagnostics route to `stderr`, keeping `stdout` purely for JSON-RPC MCP messages to prevent client crashes.
- **Defensive Docker Socket Auto-Detection**:
  1. `DOCKER_SOCKET_PATH` environment variable
  2. `DOCKER_HOST` environment variable (supporting `unix://`, `tcp://`, etc.)
  3. `/var/run/docker.sock`
  4. `~/.docker/run/docker.sock` (standard Docker Desktop on macOS)
  5. `$XDG_RUNTIME_DIR/docker.sock` (Linux rootless Docker)
- **Resilient Error Handling**: Never crashes the stdio process when Docker Desktop is stopped or socket permissions are missing. Returns clean, human-readable errors with `isError: true`.
- **Stream Demultiplexing**: Cleanly separates stdout and stderr in `docker_container_logs` and `docker_exec`, stripping raw 8-byte Docker multiplexing headers (`\x01\x00\x00...`) so binary frame headers never corrupt log output.
- **Null-Guarded Data Mapping**: Eliminates crashes when containers have null or empty port lists (internal networks, host-networked, or stopped containers).

---

## 🚀 Quickstart

### Prerequisites
- Docker Engine or Docker Desktop installed and running
- Python 3.12+ and [uv](https://docs.astral.sh/uv/) installed

---

## ⚙️ Client Configurations

### 1. Google Antigravity (`~/.gemini/config/mcp_config.json`)

To use this local server repository directly with Antigravity:

```json
{
  "mcpServers": {
    "docker": {
      "command": "uv",
      "args": [
        "--directory",
        "/Users/dachyan/dev/repos/mcp-server-docker",
        "run",
        "mcp-server-docker"
      ],
      "env": {
        "DOCKER_SOCKET_PATH": "/Users/dachyan/.docker/run/docker.sock"
      }
    }
  }
}
```

Or using `uvx` directly:

```json
{
  "mcpServers": {
    "docker": {
      "command": "uvx",
      "args": ["mcp-server-docker"]
    }
  }
}
```

---

### 2. Claude Desktop

Configuration file path:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "docker": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/mcp-server-docker",
        "run",
        "mcp-server-docker"
      ]
    }
  }
}
```

---

### 3. Cursor (`.cursor/mcp.json` or Cursor Settings)

Add to `.cursor/mcp.json` in your workspace or global Cursor settings:

```json
{
  "mcpServers": {
    "docker": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/mcp-server-docker",
        "run",
        "mcp-server-docker"
      ]
    }
  }
}
```

---

### 4. Running with Docker Container

You can also run the MCP server inside a lightweight Docker container, sharing the host Docker socket:

```bash
# Build the image
docker build -t mcp-server-docker .
```

Then configure your MCP client:

```json
{
  "mcpServers": {
    "docker": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "mcp-server-docker:latest"
      ]
    }
  }
}
```

---

## 🛠️ Tool Reference

### Standardized Tools

| Tool | Parameters | Description |
| :--- | :--- | :--- |
| **`docker_list_containers`** | `all` (boolean, optional, default: false) | Formatted markdown table listing container ID, names, image, state, status, and ports. |
| **`docker_container_logs`** | `id` (string, required)<br>`tail` (number, default: 100)<br>`timestamps` (boolean, default: false) | Fetches container logs with demultiplexed stdout and stderr, cleanly stripped of Docker 8-byte framing headers. |
| **`docker_container_action`** | `id` (string, required)<br>`action` (`start` \| `stop` \| `restart` \| `remove`)<br>`timeout` (number, optional)<br>`force` (boolean, default: false) | Executes container lifecycle actions safely. |
| **`docker_exec`** | `id` (string, required)<br>`command` (array of strings, e.g. `["ls", "-la"]`)<br>`working_dir` (string, optional) | Executes commands non-interactively inside a running container, returning stdout, stderr, and exit code. |
| **`docker_list_images`** | `all` (boolean, optional, default: false) | Lists Docker images with ID, repository, tag, human-readable size, and created timestamp. |
| **`docker_system_info`** | None | Quick healthcheck returning Docker engine version, API version, OS, Arch, CPUs, memory, and container status counts. |

### Container Lifecycle & Management Tools

| Tool | Description |
| :--- | :--- |
| `start_container` | Start an existing container by ID or name. |
| `stop_container` | Stop a running container by ID or name. |
| `create_container` | Create a container with custom networks, ports, environments, and volumes. |
| `run_container` | Create and start a container in one step. |
| `recreate_container` | Stop, remove, and recreate a container with updated configurations. |
| `remove_container` | Remove a container (with optional force). |
| `fetch_container_logs` | Structured line-by-line container logs. |

### Images, Networks, & Volumes

- **Images**: `list_images`, `pull_image`, `push_image`, `build_image`, `remove_image`
- **Networks**: `list_networks`, `create_network`, `remove_network`
- **Volumes**: `list_volumes`, `create_volume`, `remove_volume`

### Prompts

- **`docker_compose`**: Natural language Docker Compose manager implementing a safe `plan + apply` loop for container infrastructure orchestration.

---

## 🔧 Development & Testing

```bash
# Install dependencies
uv sync

# Run the test suite
uv run pytest

# Check code formatting & linting
uv run ruff check

# Run the MCP server over stdio
uv run mcp-server-docker
```

---

## 🩺 Troubleshooting

### Docker Daemon Unreachable
If the server reports `Docker daemon is unreachable`:
1. Ensure Docker Desktop or the Docker daemon is running:
   ```bash
   docker info
   ```
2. If using Docker Desktop on macOS, ensure the socket is linked or set `DOCKER_SOCKET_PATH`:
   ```bash
   export DOCKER_SOCKET_PATH="${HOME}/.docker/run/docker.sock"
   ```

### Permission Denied
If the server reports `Docker permission denied`:
- On Linux: Add your user to the `docker` group:
  ```bash
  sudo usermod -aG docker $USER
  ```
- Check socket permissions:
  ```bash
  ls -la /var/run/docker.sock ~/.docker/run/docker.sock
  ```

---

## 📄 License

GPL-3.0 License. See [LICENSE](LICENSE) for details.
