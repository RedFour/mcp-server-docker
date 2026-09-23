"""Data schemas and formatting utilities for Docker MCP server."""

import struct
from typing import Any

from docker.models.containers import Container
from docker.models.images import Image
from docker.models.networks import Network
from docker.models.volumes import Volume


def format_size(size_bytes: float | None) -> str:
    """Format bytes into human-readable size string (e.g. '120.5 MB')."""
    if size_bytes is None or size_bytes < 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size_bytes)
    unit_index = 0
    while size >= 1024.0 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1
    if unit_index == 0:
        return f"{int(size)} B"
    return f"{size:.1f} {units[unit_index]}"


def format_ports(ports: dict[str, Any] | list[Any] | None) -> str:
    """
    Safely format container ports into a human-readable string.
    Strictly guards against null, undefined, empty, or unexpected structures.
    Returns 'none' if no port mappings exist.
    """
    if not ports:
        return "none"

    formatted: list[str] = []

    if isinstance(ports, dict):
        for container_port, bindings in ports.items():
            if not bindings:
                formatted.append(str(container_port))
            elif isinstance(bindings, list):
                for binding in bindings:
                    if isinstance(binding, dict):
                        host_ip = binding.get("HostIp") or ""
                        host_port = binding.get("HostPort") or ""
                        if host_port:
                            prefix = (
                                f"{host_ip}:"
                                if host_ip and host_ip not in ("0.0.0.0", "::")
                                else ""
                            )
                            formatted.append(f"{prefix}{host_port}->{container_port}")
                        else:
                            formatted.append(str(container_port))
                    else:
                        formatted.append(f"{binding}->{container_port}")
            else:
                formatted.append(f"{bindings}->{container_port}")
    elif isinstance(ports, list):
        for p in ports:
            if isinstance(p, dict):
                priv = p.get("PrivatePort")
                pub = p.get("PublicPort")
                ptype = p.get("Type", "tcp")
                ip = p.get("IP")
                if pub is not None and priv is not None:
                    prefix = f"{ip}:" if ip and ip not in ("0.0.0.0", "::") else ""
                    formatted.append(f"{prefix}{pub}->{priv}/{ptype}")
                elif priv is not None:
                    formatted.append(f"{priv}/{ptype}")
            elif p:
                formatted.append(str(p))

    return ", ".join(formatted) if formatted else "none"


def demux_docker_stream(
    raw: bytes | str | tuple[bytes | None, bytes | None] | None,
) -> tuple[str, str]:
    """
    Demultiplex Docker container stdout and stderr streams.
    Handles:
    - (stdout_bytes, stderr_bytes) tuples (e.g. from demux=True)
    - Multiplexed raw bytes with Docker's 8-byte framing headers
    - Plain utf-8 text or single raw stream (TTY mode)
    Returns (stdout_str, stderr_str).
    """
    if raw is None:
        return "", ""

    if isinstance(raw, tuple):
        out = (raw[0] or b"").decode("utf-8", errors="replace")
        err = (raw[1] or b"").decode("utf-8", errors="replace")
        return out, err

    if isinstance(raw, str):
        return raw, ""

    if not isinstance(raw, (bytes, bytearray)):
        return str(raw), ""

    raw_bytes = bytes(raw)
    raw_len = len(raw_bytes)

    # Check for Docker 8-byte multiplex header:
    # byte 0: stream type (0=stdin, 1=stdout, 2=stderr)
    # bytes 1-3: zeros
    # bytes 4-7: uint32 frame size in big-endian
    is_multiplexed = False
    if raw_len >= 8:
        stream_type, b1, b2, b3 = raw_bytes[0], raw_bytes[1], raw_bytes[2], raw_bytes[3]
        if stream_type in (0, 1, 2) and b1 == 0 and b2 == 0 and b3 == 0:
            frame_size = struct.unpack(">I", raw_bytes[4:8])[0]
            if frame_size <= raw_len - 8:
                is_multiplexed = True

    if not is_multiplexed:
        return raw_bytes.decode("utf-8", errors="replace"), ""

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    offset = 0

    while offset + 8 <= raw_len:
        stream_type, b1, b2, b3 = (
            raw_bytes[offset],
            raw_bytes[offset + 1],
            raw_bytes[offset + 2],
            raw_bytes[offset + 3],
        )
        if b1 != 0 or b2 != 0 or b3 != 0:
            # Broken framing or non-multiplexed remainder
            stdout_chunks.append(raw_bytes[offset:])
            break

        frame_size = struct.unpack(">I", raw_bytes[offset + 4 : offset + 8])[0]
        payload_start = offset + 8
        payload_end = min(payload_start + frame_size, raw_len)
        payload = raw_bytes[payload_start:payload_end]

        if stream_type == 2:
            stderr_chunks.append(payload)
        else:
            stdout_chunks.append(payload)

        offset = payload_end

    return (
        b"".join(stdout_chunks).decode("utf-8", errors="replace"),
        b"".join(stderr_chunks).decode("utf-8", errors="replace"),
    )


def docker_to_dict(
    obj: Image | Container | Volume | Network | Any,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Converts a Docker SDK object to a serializable dictionary with defensive guards."""
    result: dict[str, Any] | None = None

    if isinstance(obj, Image):
        attrs = getattr(obj, "attrs", {}) or {}
        img_config: dict[str, Any] = attrs.get("Config") or {}
        tags = getattr(obj, "tags", None) or []
        repo_tags = attrs.get("RepoTags") or tags

        result = {
            "id": getattr(obj, "id", None) or "unknown",
            "tags": tags,
            "short_id": getattr(obj, "short_id", None)
            or (getattr(obj, "id", "")[:12] if getattr(obj, "id", None) else "unknown"),
            "labels": img_config.get("Labels") or {},
            "repo_tags": repo_tags,
            "repo_digests": attrs.get("RepoDigests") or [],
            "created": attrs.get("Created") or "",
            "size": attrs.get("Size") or 0,
            "size_formatted": format_size(attrs.get("Size")),
        }

    elif isinstance(obj, Container):
        attrs = getattr(obj, "attrs", {}) or {}
        config: dict[str, Any] = attrs.get("Config") or {}
        net_settings: dict[str, Any] = attrs.get("NetworkSettings") or {}

        # Safely extract and clean container name
        raw_name = getattr(obj, "name", None) or ""
        clean_name = raw_name.lstrip("/") if raw_name else ""
        short_id = getattr(obj, "short_id", None) or (
            getattr(obj, "id", "")[:12] if getattr(obj, "id", None) else "unknown"
        )
        if not clean_name:
            clean_name = short_id

        # Safely extract ports
        raw_ports = getattr(obj, "ports", None) or net_settings.get("Ports") or {}

        # Safely extract image name
        img_val = None
        if getattr(obj, "image", None):
            try:
                img_val = docker_to_dict(obj.image)
            except Exception:  # noqa: BLE001
                img_val = {"id": "unknown", "tags": []}
        image_name = (
            config.get("Image")
            or (obj.image.tags[0] if getattr(obj, "image", None) and getattr(obj.image, "tags", None) else None)
            or "<none>:<none>"
        )

        state_dict: dict[str, Any] = attrs.get("State") or {}
        state_status = state_dict.get("Status") or getattr(obj, "status", None) or "unknown"

        result = {
            "id": getattr(obj, "id", None) or "unknown",
            "name": clean_name,
            "short_id": short_id,
            "image": img_val,
            "image_name": image_name,
            "status": getattr(obj, "status", None) or "unknown",
            "state": state_status,
            "labels": config.get("Labels") or {},
            "ports": raw_ports,
            "ports_formatted": format_ports(raw_ports),
            "created": attrs.get("Created") or "",
            "restart_count": attrs.get("RestartCount") or 0,
            "networks": list((net_settings.get("Networks") or {}).keys()),
            "mounts": attrs.get("Mounts") or [],
            "config": {
                "hostname": config.get("Hostname"),
                "user": config.get("User"),
                "image": config.get("Image"),
            },
        }

    elif isinstance(obj, Network):
        attrs = getattr(obj, "attrs", {}) or {}
        result = {
            "id": getattr(obj, "id", None) or "unknown",
            "name": getattr(obj, "name", None) or "unknown",
            "short_id": getattr(obj, "short_id", None)
            or (getattr(obj, "id", "")[:12] if getattr(obj, "id", None) else "unknown"),
            "driver": attrs.get("Driver") or "bridge",
            "scope": attrs.get("Scope") or "local",
            "created": attrs.get("CreatedAt") or "",
            "labels": attrs.get("Labels") or {},
        }

    elif isinstance(obj, Volume):
        attrs = getattr(obj, "attrs", {}) or {}
        result = {
            "id": getattr(obj, "id", None) or getattr(obj, "name", None) or "unknown",
            "name": getattr(obj, "name", None) or "unknown",
            "short_id": getattr(obj, "short_id", None)
            or getattr(obj, "name", None)
            or "unknown",
            "labels": attrs.get("Labels") or {},
            "mountpoint": attrs.get("Mountpoint") or "",
            "created": attrs.get("CreatedAt") or "",
            "driver": attrs.get("Driver") or "local",
            "scope": attrs.get("Scope") or "local",
        }

    if result is None:
        raise ValueError(f"Unsupported object type: {type(obj)}")

    return result if overrides is None else {**result, **overrides}
