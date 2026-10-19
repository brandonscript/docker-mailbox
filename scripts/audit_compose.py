"""Validate the production Compose defaults without reading local secrets."""

from pathlib import Path

import yaml


def main() -> int:
    document = yaml.safe_load(Path("docker-compose.yml").read_text())
    failures: list[str] = []
    for name, service in document["services"].items():
        checks = {
            "all capabilities dropped": "ALL" in service.get("cap_drop", []),
            "no privilege escalation": "no-new-privileges:true" in service.get("security_opt", []),
            "read-only root filesystem": service.get("read_only") is True,
            "init enabled": service.get("init") is True,
            "resource limits": all(service.get(key) for key in ("mem_limit", "cpus", "pids_limit")),
            "bounded logs": service.get("logging", {}).get("options")
            == {
                "max-size": "10m",
                "max-file": "5",
            },
            "healthcheck": bool(service.get("healthcheck", {}).get("test")),
            "healthcheck timing": all(
                service.get("healthcheck", {}).get(key) == value
                for key, value in {
                    "interval": "10s",
                    "timeout": "3s",
                    "retries": 5,
                    "start_period": "30s",
                }.items()
            ),
            "consistent PID limits": service.get("pids_limit")
            == service.get("deploy", {}).get("resources", {}).get("limits", {}).get("pids"),
            "restart policy": service.get("restart") == "unless-stopped",
            "isolated network": service.get("networks") == ["mailbox"],
            "no host namespaces": not any(
                service.get(key) for key in ("privileged", "pid", "ipc", "network_mode")
            ),
            "loopback publishing": all(
                str(port).startswith("127.0.0.1:") for port in service.get("ports", [])
            ),
            "first-party image": service.get("image") == "psyb0t/mailbox:latest",
            "no Docker socket": not any(
                "docker.sock" in str(volume) for volume in service.get("volumes", [])
            ),
        }
        failures.extend(f"{name}: {check}" for check, valid in checks.items() if not valid)
    if failures:
        raise SystemExit("\n".join(failures))
    print("Compose production defaults passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
