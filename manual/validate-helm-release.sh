#!/usr/bin/env bash
#
# Validate a Flux HelmRelease and, by default, render and server-side dry-run
# its chart with Helm.
#
# Usage:
#   validate-helm-release.sh [--skip-render] [--server-dry-run] [--repo OCI_REPOSITORY] helm-release.yaml

set -euo pipefail

readonly DEFAULT_TRUECHARTS_REPOSITORY="oci://oci.trueforge.org/truecharts"
skip_render=false
render_output=false
server_dry_run=true
server_dry_run_explicit=false
repository="${TRUECHARTS_REPOSITORY:-${DEFAULT_TRUECHARTS_REPOSITORY}}"

usage() {
	cat <<'EOF'
Usage: validate-helm-release.sh [options] helm-release.yaml

Options:
  --skip-render       Only validate the HelmRelease structure; do not download/render the chart.
  --render            Print the rendered chart manifests to stdout.
  --server-dry-run    Ask Kubernetes to validate the rendered manifests with server-side apply dry-run (default).
  --no-server-dry-run Skip Kubernetes server-side apply dry-run.
  --repo URL          Chart repository URL (default: oci://oci.trueforge.org/truecharts).
  -h, --help          Show this help.
EOF
}

while (($#)); do
	case "$1" in
	--skip-render)
		skip_render=true
		shift
		;;
	--render)
		render_output=true
		shift
		;;
	--server-dry-run)
		server_dry_run=true
		server_dry_run_explicit=true
		shift
		;;
	--no-server-dry-run)
		server_dry_run=false
		server_dry_run_explicit=true
		shift
		;;
	--repo)
		if (($# < 2)); then
			echo "error: --repo requires a URL" >&2
			exit 2
		fi
		repository="$2"
		shift 2
		;;
	-h | --help)
		usage
		exit 0
		;;
	-*)
		echo "error: unknown option: $1" >&2
		usage >&2
		exit 2
		;;
	*)
		if [[ -n "${release_file:-}" ]]; then
			echo "error: expected exactly one HelmRelease file" >&2
			exit 2
		fi
		release_file="$1"
		shift
		;;
	esac
done

if [[ "$skip_render" == true && "$render_output" == true ]]; then
	echo "error: --skip-render and --render cannot be used together" >&2
	exit 2
fi

if [[ "$skip_render" == true && "$server_dry_run_explicit" == true && "$server_dry_run" == true ]]; then
	echo "error: --skip-render and --server-dry-run cannot be used together" >&2
	exit 2
fi

if [[ -z "${release_file:-}" ]]; then
	echo "error: a HelmRelease YAML file is required" >&2
	usage >&2
	exit 2
fi

if [[ ! -f "$release_file" ]]; then
	echo "error: file not found: $release_file" >&2
	exit 2
fi

for command in python3; do
	if ! command -v "$command" >/dev/null 2>&1; then
		echo "error: $command is required" >&2
		exit 1
	fi
done

if ! python3 -c 'import yaml' >/dev/null 2>&1; then
	echo "error: Python package PyYAML is required (python3 -c '\''import yaml'\'')" >&2
	exit 1
fi

if [[ "$skip_render" == false ]] && ! command -v helm >/dev/null 2>&1; then
	echo "error: helm is required unless --skip-render is used" >&2
	exit 1
fi

if [[ "$server_dry_run" == true && "$skip_render" == false ]] && ! command -v helm >/dev/null 2>&1; then
	echo "error: helm is required for --server-dry-run" >&2
	exit 1
fi

temporary_directory="$(mktemp -d)"
trap 'rm -rf "$temporary_directory"' EXIT

metadata_file="$temporary_directory/metadata"
values_file="$temporary_directory/values.yaml"
substituted_values_file="$temporary_directory/substituted-values.yaml"
existing_values_file="$temporary_directory/existing-values.yaml"
resolved_values_file="$temporary_directory/resolved-values.yaml"
rendered_file="$temporary_directory/rendered.yaml"
script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
substitution_file="${CLUSTER_ENV_FILE:-$script_directory/../kubernetes/config/cluster-secrets.sops.yaml}"

if [[ ! -f "$substitution_file" ]]; then
	echo "error: substitution file not found: $substitution_file" >&2
	exit 1
fi

if grep -Eq '^[[:space:]]*sops:[[:space:]]*$|ENC\[' "$substitution_file"; then
	echo "error: substitution file appears to be SOPS-encrypted: $substitution_file" >&2
	echo "Run 'clustertool decrypt' and retry validation." >&2
	exit 1
fi

if ! python3 - "$release_file" "$metadata_file" "$values_file" "$substitution_file" "$substituted_values_file" <<'PY'
import re
import sys
from pathlib import Path

import yaml


def fail(message):
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


source = Path(sys.argv[1])
metadata_path = Path(sys.argv[2])
values_path = Path(sys.argv[3])
substitution_path = Path(sys.argv[4])
substituted_values_path = Path(sys.argv[5])

try:
    documents = list(yaml.safe_load_all(source.read_text()))
except (OSError, yaml.YAMLError) as exc:
    fail(f"unable to parse {source}: {exc}")

if len(documents) != 1 or not isinstance(documents[0], dict):
    fail("expected exactly one YAML document containing a mapping")

release = documents[0]
if release.get("kind") != "HelmRelease":
    fail("kind must be HelmRelease")
if release.get("apiVersion") != "helm.toolkit.fluxcd.io/v2":
    fail("apiVersion must be helm.toolkit.fluxcd.io/v2")

metadata = release.get("metadata")
if not isinstance(metadata, dict):
    fail("metadata must be a mapping")

spec = release.get("spec")
if not isinstance(spec, dict):
    fail("spec must be a mapping")

chart = None
version = None
chart_spec = spec.get("chart", {}).get("spec")
if isinstance(chart_spec, dict):
    source_ref = chart_spec.get("sourceRef")
    if not isinstance(source_ref, dict):
        fail("spec.chart.spec.sourceRef must be a mapping")
    chart = chart_spec.get("chart")
    version = chart_spec.get("version")
    if source_ref.get("kind", "HelmRepository") != "HelmRepository":
        fail("spec.chart.spec.sourceRef.kind must be HelmRepository")
    source_name = source_ref.get("name")
    source_namespace = source_ref.get("namespace")
    if not all(isinstance(value, str) and value for value in (chart, version, source_name, source_namespace)):
        fail("spec.chart.spec requires chart, version, and sourceRef name/namespace")
else:
    chart_ref = spec.get("chartRef")
    if not isinstance(chart_ref, dict):
        fail("spec.chart.spec or spec.chartRef must be a mapping")
    if chart_ref.get("kind") != "OCIRepository":
        fail("chartRef validation currently supports OCIRepository only")
    repository_root = substitution_path.parents[1] / "repositories"
    repository = None
    for candidate in repository_root.rglob("*.yaml"):
        try:
            documents = list(yaml.safe_load_all(candidate.read_text()))
        except (OSError, yaml.YAMLError):
            continue
        for document in documents:
            if (
                isinstance(document, dict)
                and document.get("kind") == "OCIRepository"
                and isinstance(document.get("metadata"), dict)
                and document["metadata"].get("name") == chart_ref.get("name")
                and document["metadata"].get("namespace") == chart_ref.get("namespace")
            ):
                repository = document
                break
        if repository:
            break
    if not repository:
        fail(f"OCIRepository {chart_ref.get('namespace')}/{chart_ref.get('name')} not found")
    chart = repository.get("spec", {}).get("url")
    version = repository.get("spec", {}).get("ref", {}).get("tag")
    if not isinstance(chart, str) or not chart or not isinstance(version, str) or not version:
        fail("OCIRepository must define spec.url and spec.ref.tag")

required = {
    "metadata.name": metadata.get("name"),
    "metadata.namespace": metadata.get("namespace"),
}
missing = [name for name, value in required.items() if not isinstance(value, str) or not value]
if missing:
    fail("missing required fields: " + ", ".join(missing))

values = spec.get("values", {})
if values is None:
    values = {}
if not isinstance(values, dict):
    fail("values must be a mapping")

release_name = spec.get("releaseName", metadata["name"])
if not isinstance(release_name, str) or not release_name:
    fail("spec.releaseName must be a non-empty string when set")

upgrade = spec.get("upgrade", {})
if not isinstance(upgrade, dict):
    fail("spec.upgrade must be a mapping")

metadata_path.write_text(
    "\t".join(
        [
            chart,
            version,
            metadata["namespace"],
            release_name,
            str(upgrade.get("preserveValues", False)).lower(),
        ]
    )
    + "\n"
)
values_path.write_text(yaml.safe_dump(values, default_flow_style=False, sort_keys=False))

try:
    substitutions = yaml.safe_load(substitution_path.read_text()) or {}
except (OSError, yaml.YAMLError) as exc:
    fail(f"unable to read substitution file {substitution_path}: {exc}")

if not isinstance(substitutions, dict):
    fail(f"substitution file {substitution_path} must contain a mapping")
if substitutions.get("kind") == "Secret" and isinstance(substitutions.get("stringData"), dict):
    substitutions = substitutions["stringData"]

placeholder = re.compile(r"^\$\{([^}]+)\}$")
missing = set()


def substitute(value):
    if isinstance(value, dict):
        return {key: substitute(item) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item) for item in value]
    if isinstance(value, str):
        match = placeholder.fullmatch(value)
        if match:
            name = match.group(1)
            if name in substitutions:
                return substitutions[name]
            missing.add(name)
            return value

        def replace(match):
            name = match.group(1)
            if name not in substitutions:
                missing.add(name)
                return match.group(0)
            return str(substitutions[name])

        return re.sub(r"\$\{([^}]+)\}", replace, value)
    return value


substituted = substitute(values)
if missing:
    fail("missing substitutions: " + ", ".join(sorted(missing)))
substituted_values_path.write_text(
    yaml.safe_dump(substituted, default_flow_style=False, sort_keys=False)
)
PY
then
	exit 1
fi

IFS=$'\t' read -r chart version namespace release_name preserve_values < "$metadata_file"

if [[ "$render_output" == true ]]; then
	echo "HelmRelease structure is valid: $release_file" >&2
else
	echo "HelmRelease structure is valid: $release_file"
fi

if [[ "$skip_render" == true ]]; then
	echo "Chart rendering skipped."
	exit 0
fi

if [[ "$chart" == oci://* ]]; then
	chart_reference="$chart"
else
	chart_reference="${repository%/}/${chart}"
fi
values_arguments=(--values "$substituted_values_file")
if [[ "$server_dry_run" == true && "$preserve_values" == true ]]; then
	if helm get values "$release_name" \
		--namespace "$namespace" \
		--all \
		--output yaml \
		>"$existing_values_file" 2>/dev/null; then
		python3 - "$existing_values_file" "$substituted_values_file" "$resolved_values_file" <<'PY'
import re
import sys
from pathlib import Path

import yaml


placeholder = re.compile(r"\$\{[^}]+\}$")
existing = yaml.safe_load(Path(sys.argv[1]).read_text()) or {}
values = yaml.safe_load(Path(sys.argv[2]).read_text()) or {}


def resolve(value, previous):
    if isinstance(value, dict):
        return {
            key: resolve(item, previous.get(key) if isinstance(previous, dict) else None)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [resolve(item, None) for item in value]
    if isinstance(value, str) and placeholder.fullmatch(value) and previous is not None:
        return previous
    return value


Path(sys.argv[3]).write_text(
    yaml.safe_dump(resolve(values, existing), default_flow_style=False, sort_keys=False)
)
PY
		values_arguments=(--values "$existing_values_file" --values "$resolved_values_file")
		echo "Including existing Helm release values for upgrade simulation." >&2
	fi
fi

echo "Rendering ${chart_reference}:${version} with Helm..." >&2
helm template "$release_name" "$chart_reference" \
	--version "$version" \
	--namespace "$namespace" \
	"${values_arguments[@]}" \
	--include-crds \
	--skip-tests \
	--dry-run=client \
	>"$rendered_file"

if [[ "$render_output" == true ]]; then
	cat "$rendered_file"
else
	echo "Helm chart rendered successfully."
fi

if [[ "$server_dry_run" == true ]]; then
	echo "Validating Helm upgrade with server-side dry-run..." >&2
	helm upgrade "$release_name" "$chart_reference" \
		--version "$version" \
		--namespace "$namespace" \
		"${values_arguments[@]}" \
		--dry-run=server \
		--skip-crds \
		>/dev/null
	echo "Helm server-side upgrade dry-run succeeded."
fi
