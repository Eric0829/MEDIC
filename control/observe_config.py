"""Configuration loader for MEDIC observe targets."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ObserveTargetSpec:
    """One configured observe-only target."""

    name: str
    target: str = "system"
    enabled: bool = True
    patient_id: str = ""
    service_url: str = ""
    source_root: str = ""
    health_path: str = "/health"
    pid: int | None = None
    watch_processes: list[str] = field(default_factory=list)
    disk_path: str = ""
    iterations: int = 1
    interval_seconds: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "target": self.target,
            "enabled": self.enabled,
            "patient_id": self.patient_id,
            "service_url": self.service_url,
            "source_root": self.source_root,
            "health_path": self.health_path,
            "pid": self.pid,
            "watch_processes": self.watch_processes,
            "disk_path": self.disk_path,
            "iterations": self.iterations,
            "interval_seconds": self.interval_seconds,
            "metadata": self.metadata,
        }

    def watch_process_csv(self) -> str:
        return ",".join(self.watch_processes)


@dataclass
class ObserveConfig:
    """Loaded observe supervisor configuration."""

    source: str
    defaults: dict[str, Any] = field(default_factory=dict)
    targets: list[ObserveTargetSpec] = field(default_factory=list)

    def enabled_targets(self) -> list[ObserveTargetSpec]:
        return [target for target in self.targets if target.enabled]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "defaults": self.defaults,
            "targets": [target.to_dict() for target in self.targets],
            "enabled_targets": len(self.enabled_targets()),
        }


def load_observe_config(path: str | Path | None, root: str | Path) -> ObserveConfig:
    """Load a JSON observe config, or return the default local-system config."""
    if not path:
        return default_observe_config(root)

    config_path = Path(path)
    if not config_path.is_absolute():
        cwd_candidate = config_path
        root_candidate = Path(root) / config_path
        config_path = cwd_candidate if cwd_candidate.exists() else root_candidate
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("observe config must be a JSON object")

    defaults = dict(data.get("defaults", {}) or {})
    raw_targets = data.get("targets", [])
    if not isinstance(raw_targets, list):
        raise ValueError("observe config targets must be a list")

    targets = [
        _target_from_dict(item, defaults=defaults, index=index)
        for index, item in enumerate(raw_targets, start=1)
    ]
    return ObserveConfig(
        source=str(config_path),
        defaults=defaults,
        targets=targets,
    )


def default_observe_config(root: str | Path) -> ObserveConfig:
    """A safe built-in config: observe the local system once."""
    root_path = Path(root)
    return ObserveConfig(
        source="default:local-system",
        defaults={"iterations": 1, "interval_seconds": 0.0},
        targets=[
            ObserveTargetSpec(
                name="local-system",
                target="system",
                patient_id="local-system",
                disk_path=str(root_path.anchor or "/"),
                iterations=1,
                interval_seconds=0.0,
            )
        ],
    )


def observe_config_template() -> dict[str, Any]:
    """Return a ready-to-edit observe config template."""
    return {
        "version": 1,
        "defaults": {
            "iterations": 1,
            "interval_seconds": 0.0,
        },
        "targets": [
            {
                "name": "local-system",
                "target": "system",
                "patient_id": "local-system",
                "watch_processes": ["python"],
                "disk_path": "C:\\",
                "enabled": True,
            },
            {
                "name": "local-python-service",
                "target": "python-service",
                "patient_id": "local-python-service",
                "service_url": "http://127.0.0.1:8000",
                "source_root": ".",
                "health_path": "/health",
                "enabled": False,
            },
        ],
    }


def write_observe_config_template(path: str | Path, root: str | Path) -> dict[str, Any]:
    """Write a JSON template without touching runtime state."""
    target = Path(path)
    if not target.is_absolute():
        cwd_parent = target.parent if str(target.parent) != "." else Path(".")
        target = target if cwd_parent.exists() else Path(root) / target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(observe_config_template(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "status": "written",
        "path": str(target),
        "targets": len(observe_config_template()["targets"]),
    }


def _target_from_dict(
    raw: Any,
    defaults: dict[str, Any],
    index: int,
) -> ObserveTargetSpec:
    if not isinstance(raw, dict):
        raise ValueError(f"observe target #{index} must be an object")

    target_type = str(raw.get("target", raw.get("type", "system")) or "system")
    name = str(raw.get("name") or raw.get("patient_id") or f"target-{index}")
    watch_processes = _list_or_csv(raw.get("watch_processes", raw.get("watch_process", [])))
    iterations = int(raw.get("iterations", defaults.get("iterations", 1)) or 1)
    interval_seconds = float(
        raw.get("interval_seconds", defaults.get("interval_seconds", 0.0)) or 0.0
    )

    return ObserveTargetSpec(
        name=name,
        target=target_type,
        enabled=bool(raw.get("enabled", True)),
        patient_id=str(raw.get("patient_id", "")),
        service_url=str(raw.get("service_url", "")),
        source_root=str(raw.get("source_root", "")),
        health_path=str(raw.get("health_path", "/health") or "/health"),
        pid=_optional_int(raw.get("pid")),
        watch_processes=watch_processes,
        disk_path=str(raw.get("disk_path", "")),
        iterations=max(1, iterations),
        interval_seconds=max(0.0, interval_seconds),
        metadata=dict(raw.get("metadata", {}) or {}),
    )


def _list_or_csv(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)
