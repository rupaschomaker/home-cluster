---
name: truecharts-chart-upgrade
description: Upgrade a TrueCharts HelmRelease while migrating values from the current chart documentation and verifying Flux reconciliation.
---

# TrueCharts chart upgrades

Use this skill when asked to upgrade a TrueCharts chart or HelmRelease.

## Required workflow

1. Locate the target HelmRelease and read the complete file before editing. Preserve unrelated user changes.
2. Determine the latest available chart version from the configured OCI repository:

   ```bash
   helm show chart oci://oci.trueforge.org/truecharts/<chart>
   ```

3. Read the current TrueCharts documentation and the latest chart values:

   ```bash
   helm show values oci://oci.trueforge.org/truecharts/<chart> \
     --version <version>
   ```

   Use the chart documentation page and its linked common-chart documentation. Check the chart's values schema and dependency values, especially when a value belongs under the `common:` dependency key.
4. Compare the existing HelmRelease values with the new chart. Remove obsolete overrides only when the new chart default provides the same behavior. Correct invalid paths and migrate changed values. Do not change application image versions, credentials, networking behavior, persistence, or service exposure unless the request requires it.
5. Set `spec.chart.spec.version` to the selected chart version. Set `spec.upgrade.preserveValues: false` unless the user explicitly requires preserving stored Helm values, so removed or renamed values do not survive the upgrade.
6. Run the repository validator:

   ```bash
   ./manual/validate-helm-release.sh <helm-release.yaml>
   ```

   This must include Helm rendering and Helm's server-side upgrade dry-run. Treat schema errors, duplicate merge-key entries, unresolved substitutions, and API validation errors as blockers. Do not replace this with server-side apply or disable OpenAPI validation. PodSecurity warnings are reportable but are not automatically upgrade failures.
7. Run the repository update command to stage and commit the change:

   ```bash
   ./doit -no-push "update <chart>"
   ```

   The `-no-push` option stages and commits the update without pushing, so the committed diff can be reviewed first. If it cannot complete because of unrelated unstaged changes or another repository error, stop and report the blocker without stashing, reverting, or committing unrelated work.
8. Verify the rendered manifest contains the intended workload, image, services, ports, persistence, and migrated environment values without printing secrets.
9. Display the committed chart diff:

   ```bash
   git fetch origin main
   git --no-pager diff origin/main HEAD -- <helm-release.yaml>
   ```

   This compares the local committed HEAD with the current GitHub `main` HEAD. Copy the complete, relevant diff into the assistant's visible response; do not make the user expand a tool result to see it. Review the diff for unintended changes and report the chart version change, added or removed values, and any migrations. Do not print resolved values or secrets.
10. Ask the user whether the committed changes should be pushed. Do not push without confirmation. If the user confirms:

    ```bash
    git push
    ```

    Then use the `monitor-deploy` skill for the upgraded chart to verify Git, Flux synchronization, reconciliation, and workload rollout. If the user declines, stop after reporting the local commit and deployment remains unchanged.

## Substitution and encryption

The repository uses Flux `postBuild.substituteFrom` with `cluster-config` for `${...}` variables. The validator reads `clusters/main/clusterenv.yaml` to emulate this locally. If that file is SOPS-encrypted, validation must fail and instruct the user to run:

```bash
clustertool decrypt
```

Never print substituted secrets or include them in generated files, diffs, or status reports.

## Change discipline

Make surgical edits only to the requested HelmRelease and directly related validation logic. Do not delete workloads, force reconciliation, or mutate live resources unless explicitly requested. Report the exact chart version, values migrations, validation result, and any remaining Flux or workload errors.
