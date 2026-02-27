"""Helm CLI wrapper service."""
import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import settings
from src.models.schemas import (
    PodInfo,
    PodPhase,
    ReleaseInfo,
    ReleaseRevision,
    ReleaseStatus,
)

logger = logging.getLogger(__name__)


def validate_namespace(namespace: str) -> None:
    """Validate that namespace starts with allowed prefix."""
    if not namespace.startswith(settings.namespace_prefix):
        raise ValueError(
            f"Namespace must start with '{settings.namespace_prefix}'"
        )


class HelmException(Exception):
    """Exception raised when Helm command fails."""

    def __init__(self, message: str, command: str, stderr: str):
        self.message = message
        self.command = command
        self.stderr = stderr
        super().__init__(self.message)


class KubectlException(Exception):
    """Exception raised when kubectl command fails."""

    def __init__(self, message: str, command: str, stderr: str):
        self.message = message
        self.command = command
        self.stderr = stderr
        super().__init__(self.message)


class HelmService:
    """Service for executing Helm CLI operations."""

    def __init__(self):
        self.helm_bin = settings.helm_binary
        self.timeout = settings.helm_timeout

    def _run_command(
        self,
        args: List[str],
        input_data: Optional[str] = None,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess:
        """Execute a Helm command.

        Args:
            args: Command arguments (without 'helm' prefix)
            input_data: Optional stdin input
            capture_output: Whether to capture stdout/stderr

        Returns:
            CompletedProcess object

        Raises:
            HelmException: If command fails
        """
        cmd = [self.helm_bin] + args
        logger.info(f"Executing: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                input=input_data.encode() if input_data else None,
                capture_output=capture_output,
                text=True,
                timeout=self.timeout,
                check=False,
            )

            if result.returncode != 0:
                raise HelmException(
                    message=f"Helm command failed with code {result.returncode}",
                    command=" ".join(cmd),
                    stderr=result.stderr,
                )

            return result

        except subprocess.TimeoutExpired as e:
            raise HelmException(
                message=f"Helm command timed out after {self.timeout}s",
                command=" ".join(cmd),
                stderr=str(e),
            )
        except FileNotFoundError:
            raise HelmException(
                message=f"Helm binary not found: {self.helm_bin}",
                command=" ".join(cmd),
                stderr="",
            )

    def install(
        self,
        namespace: str,
        name: str,
        chart: str,
        values: Optional[Dict[str, Any]] = None,
        version: Optional[str] = None,
        create_namespace: bool = False,
    ) -> ReleaseInfo:
        """Install a Helm chart.

        Args:
            namespace: Target namespace
            name: Release name
            chart: Chart reference
            values: Values override
            version: Chart version
            create_namespace: Create namespace if not exists

        Returns:
            Release information

        Raises:
            ValueError: If namespace is invalid
            HelmException: If installation fails
        """
        validate_namespace(namespace)

        args = [
            "install",
            name,
            chart,
            "--namespace",
            namespace,
            "--output",
            "json",
        ]

        if create_namespace:
            args.append("--create-namespace")

        if version:
            args.extend(["--version", version])

        # Write values to temp file if provided
        if values:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False
            ) as f:
                import yaml

                yaml.dump(values, f)
                args.extend(["--values", f.name])
                temp_file = f.name

        try:
            result = self._run_command(args)
            release_data = json.loads(result.stdout)
            return self._parse_release_info(release_data)
        finally:
            if values:
                Path(temp_file).unlink(missing_ok=True)

    def get(self, namespace: str, name: str) -> ReleaseInfo:
        """Get information about a release.

        Args:
            namespace: Release namespace
            name: Release name

        Returns:
            Release information
        """
        validate_namespace(namespace)

        # Use 'helm list --filter' which provides all needed info in one call
        args = [
            "list",
            "--namespace",
            namespace,
            "--filter",
            f"^{name}$",
            "--output",
            "json",
        ]

        result = self._run_command(args)
        releases = json.loads(result.stdout)

        if not releases:
            raise HelmException(
                message=f"Release {name} not found",
                command=" ".join(args),
                stderr=f"Release {name} not found in namespace {namespace}",
            )

        return self._parse_list_release_info(releases[0])

    def upgrade(
        self,
        namespace: str,
        name: str,
        chart: Optional[str] = None,
        values: Optional[Dict[str, Any]] = None,
        version: Optional[str] = None,
        reset_values: bool = False,
        reuse_values: bool = True,
    ) -> ReleaseInfo:
        """Upgrade a Helm release.

        Args:
            namespace: Release namespace
            name: Release name
            chart: Chart reference (full URL like oci://... or repo/chart)
            values: Values override
            version: Chart version
            reset_values: Reset to chart default values
            reuse_values: Reuse last release values

        Returns:
            Updated release information

        Raises:
            ValueError: If chart is not provided (Helm doesn't store original chart URL)
        """
        validate_namespace(namespace)

        # Chart is required for upgrade - Helm doesn't store the original chart URL
        if not chart:
            raise ValueError(
                "Chart reference is required for upgrade. "
                "Helm does not store the original chart URL. "
                "Please provide the full chart reference (e.g., oci://registry-1.docker.io/bitnamicharts/postgresql)"
            )

        args = [
            "upgrade",
            name,
            chart,
            "--namespace",
            namespace,
            "--output",
            "json",
        ]

        if version:
            args.extend(["--version", version])

        if reset_values:
            args.append("--reset-values")
        elif reuse_values:
            args.append("--reuse-values")

        if values:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False
            ) as f:
                import yaml

                yaml.dump(values, f)
                args.extend(["--values", f.name])
                temp_file = f.name

        try:
            result = self._run_command(args)
            release_data = json.loads(result.stdout)
            return self._parse_release_info(release_data)
        finally:
            if values:
                Path(temp_file).unlink(missing_ok=True)

    def uninstall(self, namespace: str, name: str) -> Dict[str, str]:
        """Uninstall a Helm release.

        Args:
            namespace: Release namespace
            name: Release name

        Returns:
            Uninstall result message
        """
        validate_namespace(namespace)

        args = [
            "uninstall",
            name,
            "--namespace",
            namespace,
        ]

        result = self._run_command(args)
        return {"message": result.stdout.strip()}

    def rollback(
        self, namespace: str, name: str, revision: Optional[int] = None
    ) -> Dict[str, str]:
        """Rollback a Helm release.

        Args:
            namespace: Release namespace
            name: Release name
            revision: Target revision number. If not provided, rolls back to the
                previous revision.

        Returns:
            Rollback result message
        """
        validate_namespace(namespace)

        args = [
            "rollback",
            name,
            "--namespace",
            namespace,
        ]

        if revision is not None:
            args.append(str(revision))

        result = self._run_command(args)
        return {"message": result.stdout.strip()}

    def history(self, namespace: str, name: str) -> List[ReleaseRevision]:
        """Get release revision history.

        Args:
            namespace: Release namespace
            name: Release name

        Returns:
            List of revisions
        """
        validate_namespace(namespace)

        args = [
            "history",
            name,
            "--namespace",
            namespace,
            "--output",
            "json",
        ]

        result = self._run_command(args)
        revisions_data = json.loads(result.stdout)

        return [
            ReleaseRevision(
                revision=rev["revision"],
                updated=rev["updated"],
                status=ReleaseStatus(rev["status"].lower()),
                chart=rev["chart"],
                app_version=rev.get("app_version", ""),
                description=rev.get("description", ""),
            )
            for rev in revisions_data
        ]

    def get_version(self) -> str:
        """Get Helm version.

        Returns:
            Helm version string
        """
        args = ["version", "--short"]
        result = self._run_command(args)
        return result.stdout.strip()

    def _parse_release_info(self, data: Dict[str, Any]) -> ReleaseInfo:
        """Parse Helm install/upgrade JSON output to ReleaseInfo.

        Args:
            data: Helm JSON output from install/upgrade

        Returns:
            ReleaseInfo object
        """
        info = data.get("info", {})
        chart_metadata = data.get("chart", {}).get("metadata", {})

        return ReleaseInfo(
            name=data.get("name", ""),
            namespace=data.get("namespace", ""),
            revision=info.get("revision", 0),
            status=ReleaseStatus(info.get("status", "unknown").lower()),
            chart=chart_metadata.get("name", ""),
            app_version=chart_metadata.get("appVersion", ""),
            updated=info.get("last_deployed", ""),
            description=info.get("description"),
            values=data.get("config"),
        )

    def _parse_list_release_info(self, data: Dict[str, Any]) -> ReleaseInfo:
        """Parse Helm list JSON output to ReleaseInfo.

        Args:
            data: Single release from helm list output

        Returns:
            ReleaseInfo object
        """
        # helm list output: name, namespace, revision, updated, status, chart, app_version
        # Extract chart name without version (e.g., "postgresql-18.2.3" -> "postgresql")
        chart_with_version = data.get("chart", "")
        chart_name = chart_with_version.rsplit("-", 1)[0] if "-" in chart_with_version else chart_with_version

        return ReleaseInfo(
            name=data.get("name", ""),
            namespace=data.get("namespace", ""),
            revision=int(data.get("revision", 0)),
            status=ReleaseStatus(data.get("status", "unknown").lower()),
            chart=chart_name,
            app_version=data.get("app_version", ""),
            updated=data.get("updated", ""),
            description=None,
            values=None,
        )


class KubernetesService:
    """Service for Kubernetes operations (via kubectl)."""

    def __init__(self):
        self.kubectl_bin = "kubectl"

    def _run_command(self, args: List[str]) -> subprocess.CompletedProcess:
        """Execute a kubectl command.

        Args:
            args: Command arguments (without 'kubectl' prefix)

        Returns:
            CompletedProcess object

        Raises:
            KubectlException: If command fails or times out
        """
        cmd = [self.kubectl_bin] + args
        logger.info(f"Executing: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            if result.returncode != 0:
                raise KubectlException(
                    message=f"kubectl command failed with code {result.returncode}",
                    command=" ".join(cmd),
                    stderr=result.stderr,
                )

            return result

        except subprocess.TimeoutExpired as e:
            raise KubectlException(
                message="kubectl command timed out after 30s",
                command=" ".join(cmd),
                stderr=str(e),
            )
        except FileNotFoundError:
            raise KubectlException(
                message=f"kubectl binary not found: {self.kubectl_bin}",
                command=" ".join(cmd),
                stderr="",
            )

    def get_pods(self, namespace: str, label_selector: Optional[str] = None) -> List[PodInfo]:
        """Get pods in a namespace.

        Args:
            namespace: Namespace name
            label_selector: Optional label selector

        Returns:
            List of pod information
        """
        validate_namespace(namespace)

        args = [
            "get",
            "pods",
            "--namespace",
            namespace,
            "--output",
            "json",
        ]

        if label_selector:
            args.extend(["--selector", label_selector])

        result = self._run_command(args)
        pods_data = json.loads(result.stdout)

        pods = []
        for pod in pods_data.get("items", []):
            metadata = pod.get("metadata", {})
            status = pod.get("status", {})

            # Calculate ready status
            container_statuses = status.get("containerStatuses", [])
            ready_count = sum(1 for c in container_statuses if c.get("ready"))
            total_count = len(container_statuses)
            ready_str = f"{ready_count}/{total_count}"

            # Calculate restarts
            restarts = sum(c.get("restartCount", 0) for c in container_statuses)

            # Calculate age
            created = metadata.get("creationTimestamp", "")
            age = self._calculate_age(created)

            pods.append(
                PodInfo(
                    name=metadata.get("name", ""),
                    phase=PodPhase(status.get("phase", "Unknown")),
                    ready=ready_str,
                    restarts=restarts,
                    age=age,
                )
            )

        return pods

    def create_namespace(
        self,
        name: str,
        cpu_limit: str,
        memory_limit: str,
        storage_limit: str,
    ) -> Dict[str, str]:
        """Create namespace with resource quota.

        Args:
            name: Namespace name
            cpu_limit: CPU limit
            memory_limit: Memory limit
            storage_limit: Storage limit

        Returns:
            Creation result
        """
        validate_namespace(name)

        import yaml

        # Create namespace manifest using yaml.safe_dump to prevent injection
        ns_manifest = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": name,
                "labels": {
                    "paas-managed": "true",
                },
            },
        }

        # Create quota manifest using yaml.safe_dump to prevent injection
        quota_manifest = {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {
                "name": f"{name}-quota",
                "namespace": name,
            },
            "spec": {
                "hard": {
                    "requests.cpu": cpu_limit,
                    "requests.memory": memory_limit,
                    "limits.cpu": cpu_limit,
                    "limits.memory": memory_limit,
                    "persistentvolumeclaims": "10",
                    "requests.storage": storage_limit,
                },
            },
        }

        # Apply namespace
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(ns_manifest, f)
            ns_file = f.name

        try:
            self._run_command(["apply", "-f", ns_file])
        finally:
            Path(ns_file).unlink(missing_ok=True)

        # Apply quota
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(quota_manifest, f)
            quota_file = f.name

        try:
            self._run_command(["apply", "-f", quota_file])
        finally:
            Path(quota_file).unlink(missing_ok=True)

        return {"message": f"Namespace {name} created with quota"}

    def get_services(
        self, namespace: str, label_selector: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get services in a namespace.

        Args:
            namespace: Namespace name
            label_selector: Optional label selector

        Returns:
            List of service information dicts with name, ports, type
        """
        validate_namespace(namespace)

        args = [
            "get",
            "services",
            "--namespace",
            namespace,
            "--output",
            "json",
        ]

        if label_selector:
            args.extend(["--selector", label_selector])

        result = self._run_command(args)
        services_data = json.loads(result.stdout)

        services = []
        for svc in services_data.get("items", []):
            metadata = svc.get("metadata", {})
            spec = svc.get("spec", {})

            ports = []
            for port in spec.get("ports", []):
                ports.append({
                    "name": port.get("name", ""),
                    "port": port.get("port"),
                    "targetPort": port.get("targetPort"),
                    "protocol": port.get("protocol", "TCP"),
                })

            services.append({
                "name": metadata.get("name", ""),
                "namespace": namespace,
                "type": spec.get("type", "ClusterIP"),
                "clusterIP": spec.get("clusterIP"),
                "ports": ports,
            })

        return services

    def get_deployments(
        self, namespace: str, label_selector: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get deployments in a namespace.

        Args:
            namespace: Namespace name
            label_selector: Optional label selector

        Returns:
            List of deployment information dicts
        """
        validate_namespace(namespace)

        args = [
            "get",
            "deployments",
            "--namespace",
            namespace,
            "--output",
            "json",
        ]

        if label_selector:
            args.extend(["--selector", label_selector])

        result = self._run_command(args)
        deployments_data = json.loads(result.stdout)

        deployments = []
        for deploy in deployments_data.get("items", []):
            metadata = deploy.get("metadata", {})
            deployments.append({
                "name": metadata.get("name", ""),
                "namespace": namespace,
            })

        return deployments

    def patch_deployment(
        self,
        namespace: str,
        deployment_name: str,
        patch: str,
        patch_type: str = "json",
    ) -> Dict[str, Any]:
        """Patch a Kubernetes deployment.

        Args:
            namespace: Namespace name
            deployment_name: Name of the deployment to patch
            patch: JSON patch string
            patch_type: Patch type ('json', 'merge', or 'strategic')

        Returns:
            Patched deployment info

        Raises:
            KubectlException: If patching fails
        """
        validate_namespace(namespace)

        args = [
            "patch",
            "deployment",
            deployment_name,
            "--namespace",
            namespace,
            f"--type={patch_type}",
            f"--patch={patch}",
            "--output",
            "json",
        ]

        result = self._run_command(args)
        return json.loads(result.stdout)

    def apply_manifest(self, manifest: dict) -> Dict[str, Any]:
        """Apply a Kubernetes manifest via kubectl apply.

        Args:
            manifest: Kubernetes resource manifest dict

        Returns:
            Applied resource info

        Raises:
            KubectlException: If apply fails
        """
        import yaml

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(manifest, f)
            manifest_file = f.name

        try:
            result = self._run_command(["apply", "-f", manifest_file, "--output", "json"])
            return json.loads(result.stdout)
        finally:
            Path(manifest_file).unlink(missing_ok=True)

    def delete_service(self, namespace: str, service_name: str) -> bool:
        """Delete a Kubernetes Service.

        Args:
            namespace: Namespace name
            service_name: Name of the service to delete

        Returns:
            True if deleted, False if not found

        Raises:
            KubectlException: If deletion fails (other than not found)
        """
        validate_namespace(namespace)

        try:
            self._run_command([
                "delete", "service", service_name,
                "--namespace", namespace,
                "--ignore-not-found",
            ])
            return True
        except KubectlException as e:
            if "not found" in e.stderr.lower():
                return False
            raise

    @staticmethod
    def _calculate_age(created_timestamp: str) -> str:
        """Calculate age from creation timestamp."""
        if not created_timestamp:
            return "unknown"

        from datetime import datetime, timezone

        try:
            created = datetime.fromisoformat(created_timestamp.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = now - created

            if delta.days > 0:
                return f"{delta.days}d"
            elif delta.seconds >= 3600:
                return f"{delta.seconds // 3600}h"
            elif delta.seconds >= 60:
                return f"{delta.seconds // 60}m"
            else:
                return f"{delta.seconds}s"
        except Exception:
            return "unknown"
