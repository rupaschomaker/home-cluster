#!/usr/bin/env python3
"""Diagnose and optionally recover a faulted Longhorn WAL PVC for CNPG."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

LONGHORN_NAMESPACE = "longhorn-system"
FAILURE_VOLUME_STATES = {"detached", "detaching"}
FAILED_ENGINE_STATES = {"error", "stopped"}
LOGGER = logging.getLogger(__name__)


class RecoveryError(Exception):
    """An expected kubectl, discovery, or recovery failure."""


class Kubectl:
    def __init__(self, executable: str = "kubectl") -> None:
        self.executable = executable

    def run(self, *args: str) -> str:
        try:
            result = subprocess.run(
                [self.executable, *args],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise RecoveryError(f"could not run {self.executable}: {error}") from error
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RecoveryError(f"kubectl {' '.join(args)} failed: {detail}")
        return result.stdout

    def get(
        self,
        resource: str,
        namespace: str,
        name: str | None = None,
        selector: str | None = None,
    ) -> dict[str, Any]:
        args = ["get", resource]
        if name:
            args.append(name)
        args.extend(["-n", namespace])
        if selector:
            args.extend(["-l", selector])
        args.extend(["-o", "json"])
        try:
            result = json.loads(self.run(*args))
        except json.JSONDecodeError as error:
            raise RecoveryError(
                f"kubectl returned invalid JSON for {resource}: {error}"
            ) from error
        if not isinstance(result, dict):
            raise RecoveryError(f"kubectl returned an unexpected response for {resource}")
        return result

    def delete_pod(self, namespace: str, pod_name: str, timeout: int) -> None:
        self.run(
            "delete",
            "pod",
            pod_name,
            "-n",
            namespace,
            "--wait=true",
            f"--timeout={timeout}s",
        )

    def wait_ready(self, namespace: str, pod_name: str, timeout: int) -> None:
        self.run(
            "wait",
            "--for=condition=Ready",
            f"pod/{pod_name}",
            "-n",
            namespace,
            f"--timeout={timeout}s",
        )


@dataclass(frozen=True)
class Target:
    namespace: str
    cluster: str
    pvc: str
    volume: str
    instance: str
    node: str
    manager: str


def items(response: dict[str, Any]) -> list[dict[str, Any]]:
    values = response.get("items", [])
    return values if isinstance(values, list) else []


def obj_name(resource: dict[str, Any]) -> str:
    return resource.get("metadata", {}).get("name", "")


def discover_clusters(kube: Kubectl, namespace: str, selected: str | None) -> list[str]:
    clusters = sorted(
        obj_name(cluster)
        for cluster in items(kube.get("clusters.postgresql.cnpg.io", namespace))
    )
    if selected:
        if selected not in clusters:
            raise RecoveryError(
                f"CNPG cluster {selected!r} was not found in namespace {namespace!r}"
            )
        LOGGER.info("Selected CNPG cluster %s/%s", namespace, selected)
        return [selected]
    if len(clusters) > 1:
        raise RecoveryError(
            f"namespace {namespace!r} has multiple CNPG clusters "
            f"({', '.join(clusters)}); specify --cluster"
        )
    if not clusters:
        raise RecoveryError(f"no CNPG clusters found in namespace {namespace!r}")
    LOGGER.info("Found CNPG cluster(s) in namespace %s: %s", namespace, ", ".join(clusters))
    return clusters


def engine_for_volume(
    engines: list[dict[str, Any]], volume_name: str
) -> dict[str, Any] | None:
    for engine in engines:
        labels = engine.get("metadata", {}).get("labels", {})
        if (
            engine.get("spec", {}).get("volumeName") == volume_name
            or labels.get("longhornvolume") == volume_name
        ):
            return engine
    return None


def replicas_for_volume(
    replicas: list[dict[str, Any]], volume_name: str
) -> list[dict[str, Any]]:
    return [
        replica
        for replica in replicas
        if replica.get("spec", {}).get("volumeName") == volume_name
        or replica.get("metadata", {}).get("labels", {}).get("longhornvolume")
        == volume_name
    ]


def find_manager(
    node: str,
    engine: dict[str, Any],
    replicas: list[dict[str, Any]],
    managers: list[dict[str, Any]],
) -> str:
    manager_names = {
        value
        for value in (
            engine.get("status", {}).get("instanceManagerName"),
            *(replica.get("status", {}).get("instanceManagerName") for replica in replicas),
        )
        if value
    }
    manager_by_name = {obj_name(manager): manager for manager in managers}
    if len(manager_names) > 1:
        raise RecoveryError(
            f"volume replicas reference multiple instance-managers: "
            f"{', '.join(sorted(manager_names))}"
        )
    if manager_names:
        manager_name = next(iter(manager_names))
        manager = manager_by_name.get(manager_name)
        if not manager:
            raise RecoveryError(f"Longhorn instance-manager {manager_name} was not found")
        if manager.get("spec", {}).get("nodeID") != node:
            raise RecoveryError(f"instance-manager {manager_name} is not on node {node}")
    else:
        candidates = [
            manager
            for manager in managers
            if manager.get("spec", {}).get("nodeID") == node
            and manager.get("spec", {}).get("type") == "aio"
        ]
        if len(candidates) != 1:
            raise RecoveryError(
                f"expected one all-in-one instance-manager on {node}, found {len(candidates)}"
            )
        manager = candidates[0]
        manager_name = obj_name(manager)
    if manager.get("spec", {}).get("type") != "aio":
        raise RecoveryError(
            f"instance-manager {manager_name} is not all-in-one; "
            "refusing a restart that may not clear the iSCSI frontend"
        )
    return manager_name


def identify_target(
    kube: Kubectl,
    namespace: str,
    longhorn_namespace: str,
    cluster: str,
    pvc: dict[str, Any],
    engines: list[dict[str, Any]],
    replicas: list[dict[str, Any]],
    managers: list[dict[str, Any]],
) -> tuple[Target | None, str]:
    pvc_meta = pvc.get("metadata", {})
    pvc_name = pvc_meta.get("name", "")
    labels = pvc_meta.get("labels", {})
    instance = labels.get("cnpg.io/instanceName", "")
    volume_name = pvc.get("spec", {}).get("volumeName", "")
    if not volume_name:
        return None, f"{cluster}/{pvc_name}: PVC is not bound to a volume"
    provisioner = (
        pvc_meta.get("annotations", {}).get("volume.kubernetes.io/storage-provisioner")
        or pvc_meta.get("annotations", {}).get("volume.beta.kubernetes.io/storage-provisioner")
    )
    if provisioner and provisioner != "driver.longhorn.io":
        return None, f"{cluster}/{pvc_name}: provisioner={provisioner}, not Longhorn"

    volume = kube.get("volumes.longhorn.io", longhorn_namespace, volume_name)
    status = volume.get("status", {})
    engine = engine_for_volume(engines, volume_name)
    if not engine:
        raise RecoveryError(f"no Longhorn engine found for volume {volume_name}")
    engine_state = engine.get("status", {}).get("currentState", "")
    volume_state = status.get("state", "")
    robustness = status.get("robustness", "")
    failure_matches = (
        robustness == "faulted"
        and volume_state in FAILURE_VOLUME_STATES
        and engine_state in FAILED_ENGINE_STATES
    )
    summary = (
        f"{cluster}/{pvc_name}: volume={volume_state or 'unknown'}/{robustness or 'unknown'}, "
        f"engine={engine_state or 'unknown'}"
    )
    if not failure_matches:
        return None, summary
    if not instance:
        raise RecoveryError(f"cannot determine CNPG instance for PVC {pvc_name}")

    volume_replicas = replicas_for_volume(replicas, volume_name)
    node_ids = {
        value
        for value in (
            volume.get("spec", {}).get("nodeID"),
            status.get("currentNodeID"),
            status.get("currentEngineNodeID"),
            engine.get("spec", {}).get("nodeID"),
            *(replica.get("spec", {}).get("nodeID") for replica in volume_replicas),
        )
        if value
    }
    if len(node_ids) != 1:
        raise RecoveryError(
            f"cannot safely identify one Longhorn node for volume {volume_name}: "
            f"{', '.join(sorted(node_ids)) or 'no node recorded'}"
        )
    node = next(iter(node_ids))
    manager = find_manager(node, engine, volume_replicas, managers)
    return (
        Target(namespace, cluster, pvc_name, volume_name, instance, node, manager),
        summary,
    )


def find_targets(
    kube: Kubectl,
    namespace: str,
    longhorn_namespace: str,
    cluster_names: list[str],
) -> tuple[list[Target], list[str]]:
    targets: list[Target] = []
    summaries: list[str] = []
    engines = items(kube.get("engines.longhorn.io", longhorn_namespace))
    replicas = items(kube.get("replicas.longhorn.io", longhorn_namespace))
    managers = items(kube.get("instancemanagers.longhorn.io", longhorn_namespace))
    for cluster in cluster_names:
        LOGGER.info("Checking WAL PVCs for CNPG cluster %s/%s", namespace, cluster)
        pvcs = items(
            kube.get(
                "pvc",
                namespace,
                selector=f"cnpg.io/cluster={cluster},cnpg.io/pvcRole=PG_WAL",
            )
        )
        if not pvcs:
            summaries.append(f"{cluster}: no CNPG WAL PVCs found")
            LOGGER.info("No WAL PVCs found for cluster %s", cluster)
        for pvc in pvcs:
            LOGGER.info("Inspecting WAL PVC %s/%s", namespace, obj_name(pvc))
            target, summary = identify_target(
                kube,
                namespace,
                longhorn_namespace,
                cluster,
                pvc,
                engines,
                replicas,
                managers,
            )
            summaries.append(summary)
            if target:
                LOGGER.warning(
                    "Detected failed Longhorn WAL volume %s for %s/%s",
                    target.volume,
                    namespace,
                    target.pvc,
                )
                targets.append(target)
            else:
                LOGGER.info("%s", summary)
    return targets, summaries


def manager_pod(
    kube: Kubectl, longhorn_namespace: str, manager_name: str
) -> dict[str, Any]:
    pod = kube.get("pod", longhorn_namespace, manager_name)
    labels = pod.get("metadata", {}).get("labels", {})
    owners = pod.get("metadata", {}).get("ownerReferences", [])
    if labels.get("longhorn.io/component") != "instance-manager" or not any(
        owner.get("kind") == "InstanceManager" and owner.get("name") == manager_name
        for owner in owners
    ):
        raise RecoveryError(
            f"pod {manager_name} is not verified as the Longhorn instance-manager pod"
        )
    ready = any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in pod.get("status", {}).get("conditions", [])
    )
    if not ready:
        raise RecoveryError(f"Longhorn instance-manager pod {manager_name} is not Ready")
    return pod


def impact_counts(
    kube: Kubectl, longhorn_namespace: str, manager_name: str
) -> tuple[int, int]:
    engines = items(kube.get("engines.longhorn.io", longhorn_namespace))
    replicas = items(kube.get("replicas.longhorn.io", longhorn_namespace))
    engine_count = sum(
        engine.get("status", {}).get("currentState") == "running"
        and engine.get("status", {}).get("instanceManagerName") == manager_name
        for engine in engines
    )
    replica_count = sum(
        replica.get("status", {}).get("currentState") == "running"
        and replica.get("status", {}).get("instanceManagerName") == manager_name
        for replica in replicas
    )
    return engine_count, replica_count


def wait_for_volume(
    kube: Kubectl,
    namespace: str,
    volume_name: str,
    timeout: int,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    LOGGER.info("Waiting for Longhorn volume %s to become attached and healthy", volume_name)
    deadline = time.monotonic() + timeout
    last_reported: tuple[str, str] | None = None
    while True:
        volume = kube.get("volumes.longhorn.io", namespace, volume_name)
        status = volume.get("status", {})
        if status.get("state") == "attached" and status.get("robustness") == "healthy":
            LOGGER.info("Longhorn volume %s is attached and healthy", volume_name)
            return
        current = (status.get("state", "unknown"), status.get("robustness", "unknown"))
        if current != last_reported:
            LOGGER.info(
                "Longhorn volume %s recovery status: state=%s robustness=%s",
                volume_name,
                *current,
            )
            last_reported = current
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RecoveryError(
                f"volume {volume_name} did not recover; "
                f"state={status.get('state')}, robustness={status.get('robustness')}"
            )
        sleep(min(5, remaining))


def recover(
    kube: Kubectl,
    targets: list[Target],
    longhorn_namespace: str,
    timeout: int,
) -> None:
    grouped: dict[str, list[Target]] = {}
    for target in targets:
        grouped.setdefault(target.manager, []).append(target)

    for manager_name, manager_targets in grouped.items():
        manager_pod(kube, longhorn_namespace, manager_name)
        engine_count, replica_count = impact_counts(
            kube, longhorn_namespace, manager_name
        )
        print(
            f"Restarting {manager_name} on {manager_targets[0].node}; "
            f"this interrupts {engine_count} running volume engines and "
            f"{replica_count} running replicas on that manager."
        )
        LOGGER.info("Deleting Longhorn instance-manager pod %s", manager_name)
        kube.delete_pod(longhorn_namespace, manager_name, timeout)
        LOGGER.info("Waiting for Longhorn instance-manager pod %s to become Ready", manager_name)
        kube.wait_ready(longhorn_namespace, manager_name, timeout)
        LOGGER.info("Instance-manager pod %s is Ready", manager_name)
        for target in manager_targets:
            wait_for_volume(kube, longhorn_namespace, target.volume, timeout)
    for target in targets:
        LOGGER.info(
            "Waiting for CNPG instance pod %s/%s to become Ready",
            target.namespace,
            target.instance,
        )
        kube.wait_ready(target.namespace, target.instance, timeout)
        LOGGER.info("CNPG instance pod %s/%s is Ready", target.namespace, target.instance)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check CNPG WAL PVCs and optionally restart a Longhorn instance-manager "
            "for the faulted/detaching + stopped-engine failure signature."
        )
    )
    parser.add_argument(
        "--namespace",
        default="bitmagnet",
        help="Kubernetes namespace containing CNPG (default: bitmagnet)",
    )
    parser.add_argument(
        "--cluster", help="CNPG cluster name (required if namespace has multiple)"
    )
    parser.add_argument(
        "--longhorn-namespace",
        default=LONGHORN_NAMESPACE,
        help=f"Longhorn system namespace (default: {LONGHORN_NAMESPACE})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Seconds to wait for manager, volumes, and CNPG instances (default: 300)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Restart affected Longhorn instance-managers; otherwise only report (default)",
    )
    args = parser.parse_args(argv)
    if args.timeout < 1:
        parser.error("--timeout must be a positive number of seconds")
    return args


def main(argv: list[str] | None = None, kube: Kubectl | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = parse_args(argv)
    kube = kube or Kubectl()
    try:
        LOGGER.info("Starting CNPG WAL PVC check for namespace %s", args.namespace)
        clusters = discover_clusters(kube, args.namespace, args.cluster)
        targets, summaries = find_targets(
            kube, args.namespace, args.longhorn_namespace, clusters
        )
        for summary in summaries:
            print(summary)
        if not targets:
            print("No matching faulted CNPG WAL volume failure detected; no action taken.")
            LOGGER.info("No affected WAL PVCs found; nothing to remediate")
            return 0

        manager_names = sorted({target.manager for target in targets})
        for target in targets:
            print(
                f"Failure detected: cluster={target.cluster} PVC={target.pvc} "
                f"Longhorn volume={target.volume} node={target.node} "
                f"instance-manager={target.manager}"
            )
        for manager_name in manager_names:
            manager_pod(kube, args.longhorn_namespace, manager_name)
            engine_count, replica_count = impact_counts(
                kube, args.longhorn_namespace, manager_name
            )
            print(
                f"Impact for {manager_name}: {engine_count} running engines and "
                f"{replica_count} running replicas will be interrupted; Longhorn may "
                "auto-salvage replicas as they restart."
            )
        if not args.apply:
            print("Dry run only. Re-run with --apply to perform the manager restart.")
            LOGGER.info("Dry run complete; no resources were changed")
            return 0

        LOGGER.info("Applying Longhorn instance-manager recovery")
        recover(kube, targets, args.longhorn_namespace, args.timeout)
        print("Recovery completed: affected Longhorn volumes and CNPG instances are Ready.")
        LOGGER.info("Recovery completed successfully")
        return 0
    except RecoveryError as error:
        LOGGER.error("Recovery check failed: %s", error)
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
