"""Load and validate the edge webhook route configuration."""

from dataclasses import dataclass
from collections.abc import Mapping
from pathlib import Path



@dataclass(frozen=True, slots=True)
class Route:
    path: str
    verifier: str
    secret: str
    job_kind: str
    queue: str


def load_routes(filename: str, environ: Mapping[str, str]) -> tuple[Route, ...]:
    import yaml

    try:
        data = yaml.safe_load(Path(filename).read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot load routes file {filename}: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("routes file must contain a list of routes")
    routes = []
    paths: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict) or any(
            not isinstance(entry.get(key), str) or not entry[key].strip()
            for key in ("path", "verifier", "secret_env", "job_kind", "queue")
        ):
            raise ValueError("each route requires path, verifier, secret_env, job_kind and queue")
        path, verifier, secret_env = entry["path"], entry["verifier"], entry["secret_env"]
        if not path.startswith("/") or "?" in path or "#" in path or path in paths or path in {"/health", "/frameio/callback", "/tap"} or path.startswith(("/queue/", "/registry/", "/operator/", "/review/")):
            raise ValueError(f"invalid or duplicate route path: {path}")
        if verifier not in {"frameio_v0", "notion", "google_channel"}:
            raise ValueError(f"unknown verifier: {verifier}")
        if entry["queue"] not in {"main", "frameio", "diagnostic"}:
            raise ValueError(f"route {path}: unknown queue {entry['queue']}")
        secret = environ.get(secret_env, "")
        if not secret and not (verifier == "notion" and environ.get("T6_NOTION_BOOTSTRAP") == "1"):
            raise ValueError(f"route {path}: secret environment variable {secret_env} is unset or empty")
        paths.add(path)
        routes.append(Route(path, verifier, secret, entry["job_kind"], entry["queue"]))
    return tuple(routes)
