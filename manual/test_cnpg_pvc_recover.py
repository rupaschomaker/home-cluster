import contextlib
import io
import unittest
from unittest.mock import patch

from manual import cnpg_pvc_recover as recover


def resource(name, **values):
    metadata = {"name": name}
    result = {}
    for key, value in values.items():
        if key in {"labels", "ownerReferences"}:
            metadata[key] = value
        else:
            result[key] = value
    return {"metadata": metadata, **result}


class FakeKubectl:
    def __init__(self, faulted=True, cluster_names=None):
        self.cluster_names = ["app-db"] if cluster_names is None else cluster_names
        self.volume = {
            "spec": {"nodeID": ""},
            "status": {
                "state": "detaching" if faulted else "attached",
                "robustness": "faulted" if faulted else "healthy",
                "currentNodeID": "",
                "currentEngineNodeID": "",
            },
        }
        self.pvc = resource(
            "app-db-2-wal",
            labels={
                "cnpg.io/cluster": "app-db",
                "cnpg.io/pvcRole": "PG_WAL",
                "cnpg.io/instanceName": "app-db-2",
            },
            spec={"volumeName": "wal-volume"},
        )
        self.engine = resource(
            "wal-volume-e-0",
            spec={"volumeName": "wal-volume", "nodeID": ""},
            status={
                "currentState": "stopped" if faulted else "running",
                "instanceManagerName": "",
            },
        )
        self.replica = resource(
            "wal-volume-r-0",
            spec={"volumeName": "wal-volume", "nodeID": "node-a"},
            status={"currentState": "running", "instanceManagerName": "im-a"},
        )
        self.other_engine = resource(
            "other-volume-e-0",
            spec={"volumeName": "other-volume", "nodeID": "node-a"},
            status={"currentState": "running", "instanceManagerName": "im-a"},
        )
        self.manager = resource(
            "im-a",
            spec={"nodeID": "node-a", "type": "aio"},
        )
        self.pod = resource(
            "im-a",
            labels={"longhorn.io/component": "instance-manager"},
            ownerReferences=[{"kind": "InstanceManager", "name": "im-a"}],
            status={"conditions": [{"type": "Ready", "status": "True"}]},
        )
        self.deleted = []
        self.waited = []

    def get(self, resource_name, namespace, name=None, selector=None):
        if resource_name == "clusters.postgresql.cnpg.io":
            return {"items": [resource(cluster) for cluster in self.cluster_names]}
        if resource_name == "pvc":
            return {"items": [self.pvc]}
        if resource_name == "engines.longhorn.io":
            return {"items": [self.engine, self.other_engine]}
        if resource_name == "replicas.longhorn.io":
            return {"items": [self.replica]}
        if resource_name == "instancemanagers.longhorn.io":
            return {"items": [self.manager]}
        if resource_name == "volumes.longhorn.io":
            return self.volume
        if resource_name == "pod":
            return self.pod
        raise AssertionError(f"unexpected resource: {resource_name}")

    def delete_pod(self, namespace, pod_name, timeout):
        self.deleted.append((namespace, pod_name))
        self.volume["status"].update(state="attached", robustness="healthy")

    def wait_ready(self, namespace, pod_name, timeout):
        self.waited.append((namespace, pod_name, timeout))


class DiscoveryTests(unittest.TestCase):
    def test_ambiguous_namespace_requires_cluster(self):
        kube = FakeKubectl(cluster_names=["app-db", "other-db"])
        with self.assertRaisesRegex(recover.RecoveryError, "specify --cluster"):
            recover.discover_clusters(kube, "apps", None)
        self.assertEqual(recover.discover_clusters(kube, "apps", "app-db"), ["app-db"])

    def test_detects_faulted_wal_volume_and_resolves_node_manager(self):
        kube = FakeKubectl()
        targets, summaries = recover.find_targets(kube, "apps", "longhorn-system", ["app-db"])
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].volume, "wal-volume")
        self.assertEqual(targets[0].node, "node-a")
        self.assertEqual(targets[0].manager, "im-a")
        self.assertIn("detaching/faulted", summaries[0])

    def test_healthy_volume_does_not_trigger_remediation(self):
        kube = FakeKubectl(faulted=False)
        targets, summaries = recover.find_targets(kube, "apps", "longhorn-system", ["app-db"])
        self.assertEqual(targets, [])
        self.assertIn("attached/healthy", summaries[0])

    def test_faulted_volume_with_running_engine_does_not_trigger_remediation(self):
        kube = FakeKubectl()
        kube.engine["status"]["currentState"] = "running"
        targets, _ = recover.find_targets(kube, "apps", "longhorn-system", ["app-db"])
        self.assertEqual(targets, [])

    def test_non_longhorn_wal_pvc_is_reported_and_skipped(self):
        kube = FakeKubectl()
        kube.pvc["metadata"]["annotations"] = {
            "volume.kubernetes.io/storage-provisioner": "example.csi.driver"
        }
        targets, summaries = recover.find_targets(kube, "apps", "longhorn-system", ["app-db"])
        self.assertEqual(targets, [])
        self.assertIn("not Longhorn", summaries[0])

    def test_refuses_ambiguous_replica_nodes(self):
        kube = FakeKubectl()
        second = resource(
            "wal-volume-r-1",
            spec={"volumeName": "wal-volume", "nodeID": "node-b"},
            status={"currentState": "stopped", "instanceManagerName": ""},
        )
        with self.assertRaisesRegex(recover.RecoveryError, "cannot safely identify one"):
            recover.identify_target(
                kube,
                "apps",
                "longhorn-system",
                "app-db",
                kube.pvc,
                [kube.engine],
                [kube.replica, second],
                [kube.manager],
            )


class CliTests(unittest.TestCase):
    def test_default_namespace_is_bitmagnet(self):
        args = recover.parse_args([])
        self.assertEqual(args.namespace, "bitmagnet")

    def test_dry_run_reports_impact_without_deleting_manager(self):
        kube = FakeKubectl()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = recover.main(["--namespace", "apps"], kube)
        self.assertEqual(result, 0)
        self.assertEqual(kube.deleted, [])
        self.assertIn("1 running engines and 1 running replicas", output.getvalue())
        self.assertIn("Dry run only", output.getvalue())

    def test_apply_restarts_manager_then_waits_for_volume_and_cnpg(self):
        kube = FakeKubectl()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = recover.main(
                ["--namespace", "apps", "--apply", "--timeout", "15"], kube
            )
        self.assertEqual(result, 0)
        self.assertEqual(kube.deleted, [("longhorn-system", "im-a")])
        self.assertIn(("longhorn-system", "im-a", 15), kube.waited)
        self.assertIn(("apps", "app-db-2", 15), kube.waited)
        self.assertIn("Recovery completed", output.getvalue())

    def test_kubectl_command_failure_is_reported(self):
        failed = type("Result", (), {"returncode": 1, "stdout": "", "stderr": "forbidden"})()
        with patch("manual.cnpg_pvc_recover.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(recover.RecoveryError, "forbidden"):
                recover.Kubectl().get("pods", "apps")

    def test_namespace_with_no_cluster_is_an_error(self):
        kube = FakeKubectl(cluster_names=[])
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = recover.main(["--namespace", "apps"], kube)
        self.assertEqual(result, 1)
        self.assertIn("no CNPG clusters", errors.getvalue())

    def test_volume_recovery_timeout_is_reported_as_failure(self):
        kube = FakeKubectl()
        with patch("manual.cnpg_pvc_recover.time.monotonic", side_effect=[0, 2]):
            with self.assertRaisesRegex(recover.RecoveryError, "did not recover"):
                recover.wait_for_volume(
                    kube, "longhorn-system", "wal-volume", 1, sleep=lambda _: None
                )


if __name__ == "__main__":
    unittest.main()
