"""Bounded, read-only container log snapshots scoped to one Helm release.

Logs are tenant-controlled text and can contain credentials. Never log stdout or
stderr, and never allow a caller-supplied pod/container to bypass discovery.
"""
import json
import re
import subprocess
from typing import Optional

from src.models.schemas import ReleaseLogSource, ReleaseLogsResponse
from src.services.helm import KubectlException, KubernetesService, validate_namespace

LOG_TAIL_DEFAULT = 500
LOG_TAIL_MAX = 2000
LOG_BYTE_LIMIT = 256 * 1024
LOG_COMMAND_TIMEOUT_SECONDS = 4
_INSTANCE_LABEL = "app.kubernetes.io/instance"
_RELEASE_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,51}[a-z0-9])?")
_RESOURCE_NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")
_CONTAINER_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")


class ReleaseLogReader:
    """Two short kubectl reads per snapshot; no exec, Helm, or streaming."""

    def __init__(self, kubectl_bin="kubectl"):
        self.k8s = KubernetesService()
        self.k8s.kubectl_bin = kubectl_bin

    def _run(self, args):
        # Bytes tolerate a byte limit that cuts a UTF-8 character. Discard stderr
        # at the subprocess boundary, so failures cannot leak kubeconfig details
        # or application credentials through exception messages / access logs.
        return self.k8s._run_command(
            args, timeout=LOG_COMMAND_TIMEOUT_SECONDS, sensitive_output=True,
        )

    def read(
        self, namespace: str, release_name: str, pod: Optional[str] = None,
        container: Optional[str] = None, tail_lines: int = LOG_TAIL_DEFAULT,
        previous: bool = False,
    ) -> ReleaseLogsResponse:
        validate_namespace(namespace)
        if not _RELEASE_NAME.fullmatch(release_name):
            raise ValueError("Invalid release name")
        if not _CONTAINER_NAME.fullmatch(namespace) or len(namespace) > 63:
            raise ValueError("Invalid namespace")
        if type(tail_lines) is not int or not 1 <= tail_lines <= LOG_TAIL_MAX:
            raise ValueError("Invalid log tail limit")
        if type(previous) is not bool:
            raise ValueError("Invalid previous flag")
        response = ReleaseLogsResponse()
        try:
            listed = self._run([
                "get", "pods", "--namespace", namespace, "--selector",
                f"{_INSTANCE_LABEL}={release_name}", "--output", "json",
                f"--request-timeout={LOG_COMMAND_TIMEOUT_SECONDS}s",
            ])
            if listed.returncode:
                response.reason = "lookup_failed"
                return response
            items = json.loads(listed.stdout).get("items", [])
            candidates = []
            for item in items:
                metadata = item.get("metadata") or {}
                name = metadata.get("name", "")
                # Independently check the returned ownership, even though the
                # API server already applied the exact selector. A similar name
                # or a selected pod in the same namespace grants no access.
                if (metadata.get("labels", {}).get(_INSTANCE_LABEL) != release_name
                        or metadata.get("namespace") != namespace
                        or not _RESOURCE_NAME.fullmatch(name) or len(name) > 253):
                    continue
                spec = item.get("spec") or {}
                containers = []
                for entry in [
                    *spec.get("containers", []), *spec.get("initContainers", []),
                ]:
                    value = entry.get("name", "")
                    if (_CONTAINER_NAME.fullmatch(value) and len(value) <= 63
                            and value not in containers):
                        containers.append(value)
                if not containers:
                    continue
                candidates.append((
                    bool(metadata.get("deletionTimestamp")),
                    (item.get("status") or {}).get("phase") != "Running",
                    name, containers,
                ))
            candidates.sort(key=lambda item: item[:3])
            response.sources = [
                ReleaseLogSource(pod=name, containers=containers)
                for _, _, name, containers in candidates
            ]
        except KubectlException as exc:
            response.reason = "timeout" if exc.timed_out else "lookup_failed"
            return response
        except subprocess.TimeoutExpired:
            response.reason = "timeout"
            return response
        except (OSError, ValueError, TypeError, AttributeError):
            response.reason = "lookup_failed"
            return response

        if not response.sources:
            response.reason = "no_pods"
            return response
        selected = (
            next((source for source in response.sources if source.pod == pod), None)
            if pod else response.sources[0]
        )
        if selected is None:
            response.reason = "source_not_found"
            return response
        response.pod = selected.pod
        response.container = container or selected.containers[0]
        if response.container not in selected.containers:
            response.container = None
            response.reason = "container_not_found"
            return response
        args = [
            "logs", selected.pod, "--namespace", namespace,
            "--container", response.container, "--timestamps=true",
            f"--tail={tail_lines}", f"--limit-bytes={LOG_BYTE_LIMIT + 1}",
            f"--request-timeout={LOG_COMMAND_TIMEOUT_SECONDS}s",
            "--pod-running-timeout=1s",
        ]
        if previous:
            args.append("--previous=true")
        try:
            result = self._run(args)
            if result.returncode:
                response.reason = "logs_unavailable"
                return response
            raw = result.stdout
            response.truncated = len(raw) > LOG_BYTE_LIMIT
            # Replace malformed UTF-8, preserving all valid text.
            # The frontend renders this as text, never HTML or terminal escapes.
            response.text = raw[:LOG_BYTE_LIMIT].decode("utf-8", errors="replace")
            # A trailing replacement can encode to three bytes; enforce the
            # response bound again on valid UTF-8 without splitting characters.
            encoded = response.text.encode("utf-8")
            response.truncated = response.truncated or len(encoded) > LOG_BYTE_LIMIT
            response.text = encoded[:LOG_BYTE_LIMIT].decode("utf-8", errors="ignore")
            response.available = True
            return response
        except KubectlException as exc:
            response.reason = "timeout" if exc.timed_out else "logs_unavailable"
        except subprocess.TimeoutExpired:
            response.reason = "timeout"
        except OSError:
            response.reason = "logs_unavailable"
        return response
