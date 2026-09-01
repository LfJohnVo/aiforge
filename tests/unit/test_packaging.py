"""Packaging and hardening: the instance generator, the images and the Helm chart.

The claim these tests defend is the product claim: **a new area is configuration, not
code.** So the generator is checked for what it produces and, more importantly, for what
it leaves alone -- if `make new-instance` ever needed a source edit, this would be a
framework someone forks, not a product someone deploys.

The container and chart assertions are read off the files rather than off a running
cluster. That is a real limit and worth naming: they prove the manifests say the right
thing, not that a cluster enforced it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "deploy" / "compose" / "docker-compose.yml"
DOCKERFILE = REPO_ROOT / "deploy" / "compose" / "Dockerfile"
CHART = REPO_ROOT / "deploy" / "helm" / "agent-forge"
DASHBOARDS = REPO_ROOT / "deploy" / "observability" / "grafana" / "dashboards"


# ----------------------------------------------------------- instance generator


@pytest.fixture
def generated(tmp_path: Path) -> Path:
    from scripts.new_instance import main

    assert main(["--name", "ventas", "--tenant", "acme-mx", "--root", str(tmp_path)]) == 0
    return tmp_path / "acme-mx-ventas"


def test_a_new_instance_produces_everything_it_needs_to_run(generated: Path) -> None:
    assert (generated / ".env").is_file()
    assert (generated / "docker-compose.yml").is_file()
    assert (generated / "agent.profile.yaml").is_file()
    assert (generated / "README.md").is_file()


def test_the_generated_instance_has_its_own_project_port_and_ledger(generated: Path) -> None:
    """Three collisions a second cell on one host would otherwise hit."""
    env = (generated / ".env").read_text(encoding="utf-8")
    compose = yaml.safe_load((generated / "docker-compose.yml").read_text(encoding="utf-8"))

    assert "COMPOSE_PROJECT_NAME=agent-forge-acme-mx-ventas" in env
    assert "AGENT_API_PORT=8180" in env
    assert "acme-mx-ventas-ledger" in compose["volumes"]


def test_the_generated_profile_names_this_instance(generated: Path) -> None:
    profile = yaml.safe_load((generated / "agent.profile.yaml").read_text(encoding="utf-8"))

    assert profile["identity"]["agent_name"] == "asistente-ventas"
    assert profile["identity"]["tenant_id"] == "acme-mx"
    assert profile["identity"]["area"] == "ventas"
    # The persona has to name this area too: it is the one field a user reads, and a
    # `ventas` cell introducing itself as the finance assistant is a wrong default.
    assert "Ventas" in profile["identity"]["persona"]


def test_the_generated_profile_keeps_the_reference_comments(generated: Path) -> None:
    """A generated minimal profile would drop everything the example explains."""
    text = (generated / "agent.profile.yaml").read_text(encoding="utf-8")

    assert text.count("#") > 40
    assert "governance:" in text and "autonomy:" in text


def test_the_generated_compose_includes_rather_than_copies(generated: Path) -> None:
    """A full copy drifts from the original the first time anyone edits either."""
    compose = yaml.safe_load((generated / "docker-compose.yml").read_text(encoding="utf-8"))

    assert compose["include"] == [{"path": "../../deploy/compose/docker-compose.yml"}]
    # It overrides only what differs; it does not redefine the services.
    assert set(compose["services"]) == {"agent-api", "ingestion-worker"}
    assert "build" not in compose["services"]["agent-api"]


# ------------------------------------------------- shared-infrastructure mode


class _ComposeLoader(yaml.SafeLoader):
    """A loader that tolerates Compose's own YAML tags.

    `!reset` and `!override` are Compose merge directives, not standard YAML, so
    `safe_load` refuses the file outright. They are load-bearing here -- `!reset` on
    `depends_on` is what stops the cell waiting for services another project owns -- so the
    test reads them rather than avoiding them.
    """


_ComposeLoader.add_constructor(
    "!reset", lambda loader, node: loader.construct_sequence(node) if node.value else []
)
_ComposeLoader.add_constructor(
    "!override", lambda loader, node: loader.construct_sequence(node)
)


def _compose(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader)  # noqa: S506


@pytest.fixture
def shared(tmp_path: Path) -> Path:
    from scripts.new_instance import main

    argv = ["--name", "soc", "--tenant", "acme-mx", "--shared", "--root", str(tmp_path)]
    assert main(argv) == 0
    return tmp_path / "acme-mx-soc"


def test_a_shared_instance_brings_no_datastore_of_its_own(shared: Path) -> None:
    """The whole point of the mode, and the thing that was silently wrong before.

    `include` pulls in every service, so bringing the instance up under its own project
    name cloned the entire stack -- five containers where one was promised, and the
    tenant_id namespacing that exists to let cells share stores did nothing.
    """
    compose = _compose(shared / "docker-compose.yml")

    assert "include" not in compose
    assert set(compose["services"]) == {"agent-api", "ingestion-worker"}
    for service in compose["services"].values():
        assert "extends" in service


def test_a_shared_instance_joins_the_base_stacks_networks(shared: Path) -> None:
    compose = _compose(shared / "docker-compose.yml")

    for name in ("frontend", "backend"):
        assert compose["networks"][name]["external"] is True
        assert "BASE_COMPOSE_PROJECT" in compose["networks"][name]["name"]
    assert "BASE_COMPOSE_PROJECT=" in (shared / ".env").read_text(encoding="utf-8")


def test_a_shared_instance_waits_on_nothing_it_does_not_own(shared: Path) -> None:
    """`extends` copies depends_on, and waiting on a service in another project hangs."""
    text = (shared / "docker-compose.yml").read_text(encoding="utf-8")

    assert "depends_on: !reset []" in text


def test_a_shared_instance_still_keeps_its_own_evidence_chain(shared: Path) -> None:
    """Two instances writing one chain fork it, and a forked chain fails verification."""
    compose = _compose(shared / "docker-compose.yml")

    assert compose["volumes"]["ledger-data"]["name"] == "acme-mx-soc-ledger"


def test_the_two_modes_are_the_only_difference(tmp_path: Path) -> None:
    """Same profile, same port allocation, same README structure -- only the topology."""
    from scripts.new_instance import main

    main(["--name", "soc", "--tenant", "acme", "--root", str(tmp_path / "std")])
    main(["--name", "soc", "--tenant", "acme", "--shared", "--root", str(tmp_path / "sh")])

    for name in (".env", "docker-compose.yml", "README.md", "agent.profile.yaml"):
        assert (tmp_path / "std" / "acme-soc" / name).is_file(), name
        assert (tmp_path / "sh" / "acme-soc" / name).is_file(), name

    std_profile = (tmp_path / "std" / "acme-soc" / "agent.profile.yaml").read_text("utf-8")
    shared_profile = (tmp_path / "sh" / "acme-soc" / "agent.profile.yaml").read_text("utf-8")
    assert std_profile == shared_profile


def test_the_generated_compose_only_names_services_that_exist(generated: Path) -> None:
    """An override for a service the base file does not define is silently inert."""
    base = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    generated_compose = yaml.safe_load(
        (generated / "docker-compose.yml").read_text(encoding="utf-8")
    )

    assert set(generated_compose["services"]) <= set(base["services"])


def test_the_generated_env_carries_no_secret(generated: Path) -> None:
    """The shared infrastructure's credentials stay in the repository's `.env`."""
    settings = [
        line.lower()
        for line in (generated / ".env").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    for line in settings:
        for forbidden in ("password", "secret", "token", "api_key"):
            assert forbidden not in line, line


def test_a_second_instance_takes_the_next_port(tmp_path: Path) -> None:
    from scripts.new_instance import main

    main(["--name", "ventas", "--tenant", "acme-mx", "--root", str(tmp_path)])
    main(["--name", "soc", "--tenant", "acme-mx", "--root", str(tmp_path)])

    assert "AGENT_API_PORT=8180" in (tmp_path / "acme-mx-ventas" / ".env").read_text("utf-8")
    assert "AGENT_API_PORT=8280" in (tmp_path / "acme-mx-soc" / ".env").read_text("utf-8")


def test_regenerating_without_force_refuses(tmp_path: Path) -> None:
    """Overwriting an instance would discard a profile somebody edited."""
    from scripts.new_instance import main

    assert main(["--name", "ventas", "--tenant", "acme-mx", "--root", str(tmp_path)]) == 0
    assert main(["--name", "ventas", "--tenant", "acme-mx", "--root", str(tmp_path)]) == 2


# `Ventas` is absent on purpose: the generator lowercases before validating, which is
# friendly and safe. What must be refused is what cannot be a directory, a Compose
# project name and a volume prefix.
@pytest.mark.parametrize("name", ["ventas!", "v", "a" * 40, "1ventas", "ven tas"])
def test_a_malformed_name_is_refused(tmp_path: Path, name: str) -> None:
    """The name becomes a directory, a Compose project and a volume prefix."""
    from scripts.new_instance import main

    assert main(["--name", name, "--tenant", "acme-mx", "--root", str(tmp_path)]) == 2


def test_no_source_file_is_touched(tmp_path: Path) -> None:
    """The product claim, asserted: a new area changes configuration, never code."""
    from scripts.new_instance import main

    source = sorted((REPO_ROOT / "src").rglob("*.py"))
    before = {path: path.stat().st_mtime_ns for path in source}

    main(["--name", "soc", "--tenant", "otra-empresa", "--root", str(tmp_path)])

    assert {path: path.stat().st_mtime_ns for path in source} == before


# ----------------------------------------------------------------- containers


def test_the_images_run_as_a_non_root_user() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert "--uid 10001" in text
    # Every stage that runs something has to step down. A `USER` on the builder alone
    # would leave the shipped image running as root.
    assert text.count("USER forge") >= 2
    assert "nologin" in text


def test_every_service_drops_capabilities_and_forbids_privilege_escalation() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))

    for name, service in compose["services"].items():
        assert service.get("cap_drop") == ["ALL"], name
        options = service.get("security_opt") or []
        assert any("no-new-privileges" in str(o) for o in options), name


# Images whose entrypoint chowns its data directory as root before stepping down, and the
# capabilities that needs. Each one was verified by booting the container with ALL dropped
# and reading why it crash-looped -- not by reasoning about what it probably wants.
EXPECTED_CAP_ADD = {
    "postgres": {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"},
    "redis": {"CHOWN", "SETGID", "SETUID"},
    "neo4j": {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"},
}


def test_only_the_images_that_need_a_capability_get_one() -> None:
    """A new service must not quietly acquire capabilities by being added."""
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))

    granted = {
        name: set(service["cap_add"])
        for name, service in compose["services"].items()
        if service.get("cap_add")
    }

    assert granted == EXPECTED_CAP_ADD


def test_no_healthcheck_needs_a_shell_the_image_does_not_have() -> None:
    """A distroless image with a CMD-SHELL probe sits `unhealthy` for ever.

    Anything depending on it with `service_healthy` then never starts, which is how a
    stack that looks configured never comes up. The two distroless images here
    (otel-collector, loki) carry no container healthcheck for exactly this reason.
    """
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))

    for name in ("otel-collector", "loki"):
        assert not compose["services"][name].get("healthcheck"), name


def test_nothing_waits_on_a_service_that_cannot_report_health() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))

    for name, service in compose["services"].items():
        for target, condition in (service.get("depends_on") or {}).items():
            wanted = condition.get("condition") if isinstance(condition, dict) else condition
            if wanted == "service_healthy":
                assert compose["services"][target].get("healthcheck"), f"{name} -> {target}"


def test_postgres_data_is_mounted_where_postgres_18_expects_it() -> None:
    """postgres:18+ refuses to start if it finds a mount at the old `/data` path.

    Verified by booting it: the container crash-loops with a message about
    `pg_ctlcluster` and an un-upgraded cluster.
    """
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))

    mounts = compose["services"]["postgres"]["volumes"]
    assert "postgres-data:/var/lib/postgresql" in mounts
    assert "postgres-data:/var/lib/postgresql/data" not in mounts


def test_no_service_publishes_beyond_loopback() -> None:
    """A store reachable from the network is a store somebody will reach."""
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))

    for name, service in compose["services"].items():
        for published in service.get("ports") or []:
            assert str(published).startswith("127.0.0.1:"), f"{name}: {published}"


def test_every_image_is_pinned_to_a_tag() -> None:
    """`latest` makes two deployments of the same commit different systems."""
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))

    for name, service in compose["services"].items():
        image = service.get("image", "")
        if not image or image.startswith("agent-forge/"):
            continue  # built here, versioned by AGENT_FORGE_VERSION
        assert ":" in image, name
        assert not image.endswith(":latest"), name


# --------------------------------------------------------------------- chart


def _chart_file(name: str) -> str:
    return (CHART / name).read_text(encoding="utf-8")


def test_the_chart_has_the_pieces_deployment_md_promises() -> None:
    for name in (
        "Chart.yaml",
        "values.yaml",
        "templates/deployment.yaml",
        "templates/service.yaml",
        "templates/hpa.yaml",
        "templates/configmap.yaml",
        "templates/job-ingestion.yaml",
        "templates/cronjob-maintenance.yaml",
    ):
        assert (CHART / name).is_file(), name


def test_the_chart_expects_a_secret_and_never_renders_one() -> None:
    """A Secret rendered from values lands in the Helm history and in `helm get values`."""
    values = yaml.safe_load(_chart_file("values.yaml"))

    assert values["secrets"]["existingSecret"]
    assert not (CHART / "templates" / "secret.yaml").exists()
    for template in (CHART / "templates").glob("*.yaml"):
        assert "kind: Secret" not in template.read_text(encoding="utf-8"), template.name


def test_the_chart_runs_as_non_root_with_a_read_only_root() -> None:
    values = yaml.safe_load(_chart_file("values.yaml"))
    security = values["securityContext"]

    assert security["runAsNonRoot"] is True
    assert security["runAsUser"] == 10001
    assert security["readOnlyRootFilesystem"] is True
    assert security["allowPrivilegeEscalation"] is False
    assert security["capabilities"]["drop"] == ["ALL"]


def test_the_ledger_volume_survives_an_uninstall() -> None:
    """Deleting the chain would destroy the audit trail of everything the cell did."""
    assert "helm.sh/resource-policy: keep" in _chart_file("templates/pvc.yaml")
    assert "ReadWriteOnce" in _chart_file("templates/pvc.yaml")


def test_a_profile_change_restarts_the_pods() -> None:
    """Otherwise the ConfigMap updates and the pods keep serving the old profile."""
    assert "checksum/profile" in _chart_file("templates/deployment.yaml")


def test_the_chart_does_not_package_the_datastores() -> None:
    """A `helm upgrade` of the cell must not be able to touch Postgres."""
    chart = yaml.safe_load(_chart_file("Chart.yaml"))

    assert "dependencies" not in chart
    assert chart["annotations"]["agent-forge/dependencies"] == "external"


# ----------------------------------------------------------------- dashboards


def test_the_five_dashboards_are_valid_json_with_stable_uids() -> None:
    boards = sorted(DASHBOARDS.glob("*.json"))

    assert len(boards) == 5
    uids = set()
    for path in boards:
        board = json.loads(path.read_text(encoding="utf-8"))
        assert board["uid"] not in uids, f"duplicate uid in {path.name}"
        uids.add(board["uid"])
        assert board["panels"], path.name


def test_every_dashboard_query_names_a_metric_the_cell_emits() -> None:
    """A panel querying a metric nobody exports is a flat line nobody investigates."""
    from agent_forge.observability.metrics import Metrics

    # `collect()` gives the base name (`agentforge_llm_cost_usd`); PromQL uses the
    # exported one (`..._total`, `..._bucket`, `..._count`, `..._sum`). Compare on the
    # base so the two vocabularies line up.
    emitted = {collector.name for collector in Metrics().registry.collect()}

    for path in sorted(DASHBOARDS.glob("*.json")):
        board = json.loads(path.read_text(encoding="utf-8"))
        for panel in board["panels"]:
            for target in panel.get("targets", []):
                for token in _metric_tokens(target["expr"]):
                    assert token in emitted, f"{path.name}/{panel['title']}: {token}"


def _metric_tokens(expr: str) -> set[str]:
    import re

    names = set()
    for raw in re.findall(r"agentforge_[a-z_]+", expr):
        for suffix in ("_bucket", "_count", "_sum", "_total"):
            if raw.endswith(suffix):
                raw = raw[: -len(suffix)]
                break
        names.add(raw)
    return names


def test_every_dashboard_filters_by_tenant() -> None:
    """A dashboard that mixes tenants is unusable the moment there are two."""
    for path in sorted(DASHBOARDS.glob("*.json")):
        board = json.loads(path.read_text(encoding="utf-8"))
        names = {v["name"] for v in board["templating"]["list"]}
        assert "tenant" in names, path.name


# ---------------------------------------------------------------- the toolchain


def test_the_makefile_targets_the_docs_promise_all_exist() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    for target in (
        "check",
        "test",
        "cov",
        "up",
        "down",
        "new-instance",
        "new-connector",
        "repo-graph",
        "verify-ledger",
        "evals",
        "evals-ci",
        "sbom",
        "scan",
        "ingest",
        "docs-check",
    ):
        assert f"\n{target}:" in makefile, target


def test_every_script_the_makefile_calls_exists() -> None:
    """A target pointing at a missing script fails only when somebody runs it."""
    import re

    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    for script in re.findall(r"python (scripts/[a-z_]+\.py)", makefile):
        assert (REPO_ROOT / script).is_file(), script
