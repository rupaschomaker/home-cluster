---
name: monitor-deploy
description: Verify Git and Flux synchronization, reconcile stale Flux state, and monitor a Kubernetes deployment rollout.
---

# Deployment monitoring

Use this skill when asked to monitor deployment progress after a Git or Flux-managed change.

## Required workflow

1. Ensure the intended change is committed and pushed. Fetch the current GitHub `main` branch and record its commit:

   ```bash
   git fetch origin main
   git rev-parse origin/main
   ```

   Do not claim deployment progress for uncommitted local changes.
2. Compare the GitHub `main` commit with Flux's source artifact and Kustomization revision:

   ```bash
   kubectl -n flux-system get gitrepository cluster \
     -o jsonpath='{.status.artifact.revision}{"\n"}'
   kubectl -n flux-system get kustomization <name> \
     -o jsonpath='{.status.lastAppliedRevision}{"\n"}'
   ```

3. If Flux is not synchronized to the current GitHub revision, explicitly reconcile the Git source and Kustomization:

   ```bash
   flux reconcile source git cluster --with-source
   flux reconcile kustomization <name> --with-source
   ```

   Recheck both revisions before proceeding.
4. Monitor the HelmRelease until it observes the intended chart revision and becomes Ready:

   ```bash
   for i in $(seq 1 12); do
     kubectl -n <namespace> get helmrelease <name> -o wide
     sleep 10
   done
   ```

   Refresh every 10 seconds during the initial rollout. If the status is progressing
   normally after two minutes, use 15-30 second refreshes; do not wait 120 seconds
   between checks unless the user explicitly requests it.

5. Monitor the workload rollout and readiness:

   ```bash
   kubectl -n <namespace> rollout status deployment/<deployment> --timeout=10m
   kubectl -n <namespace> get deployment,pods -o wide
   ```

   Recheck workload status every 10-15 seconds while pods are starting, then widen
   the interval only after readiness has stabilized.

6. If progress stalls, inspect the HelmChart and recent events. Distinguish stale Git/Kustomization revisions, HelmChart artifact failures, Helm schema/render failures, Flux controller connectivity failures, and workload readiness failures. Do not claim the deployment succeeded until the HelmRelease is Ready and the workload is healthy.

## Reporting

Report the GitHub commit, Flux source and Kustomization revisions, reconciliation actions taken, HelmRelease status, and workload readiness. Never print secrets from Helm values, Kubernetes Secrets, or substituted configuration.
