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
RULES = REPO_ROOT / "deploy" / "observability" / "rules"


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
_ComposeLoader.add_constructor("!override", lambda loader, node: loader.construct_sequence(node))


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


# ------------------------------------------------------------------ images


def _sync_extras() -> dict[str, list[set[str]]]:
    """The extras each `uv sync` in the Dockerfile asks for, grouped by stage."""
    import re

    text = DOCKERFILE.read_text(encoding="utf-8")
    stages: dict[str, list[set[str]]] = {}
    current = ""
    for block in text.splitlines():
        # Comments mention `uv sync` too, and counting one as an invocation shifts every
        # stage's list by an empty entry.
        if block.strip().startswith("#"):
            continue
        stage = re.match(r"FROM .* AS ([a-z-]+)", block.strip())
        if stage:
            current = stage.group(1)
            stages.setdefault(current, [])
        if "uv sync" in block:
            stages.setdefault(current, []).append(set())
        if "--extra" in block and stages.get(current):
            stages[current][-1] |= set(re.findall(r"--extra (\w+)", block))
    return stages


def test_both_syncs_in_a_stage_ask_for_the_same_extras() -> None:
    """`uv sync` prunes: a second sync without an extra uninstalls it.

    The API image shipped with no qdrant-client this way. The deps stage installed it,
    the final sync in the same image removed it, and the build log showed the install --
    so the evidence pointed at a working image. At runtime the cell fell back to an
    in-memory vector store and answered every question with "no documented information".
    """
    deps = _sync_extras()

    for image, source in (("api", "deps-api"), ("worker", "deps-worker")):
        installed = deps[source][0]
        final = deps[image][0]
        assert installed == final, (
            f"{image}: deps stage installs {sorted(installed)} but the final sync asks "
            f"for {sorted(final)}, which uninstalls the difference"
        )


def test_the_api_can_reach_the_stores_it_reads_on_every_request() -> None:
    """Retrieval happens in the request path, so these are not optional for the API."""
    api = _sync_extras()["api"][0]

    assert {"knowledge", "memory"} <= api


def test_the_api_does_not_carry_the_document_parsers() -> None:
    """docling pulls torch: ~9 GB on every node, on every rollout, to open no PDFs."""
    assert "parsing" not in _sync_extras()["api"][0]
    assert "parsing" in _sync_extras()["worker"][0]


# ----------------------------------------------------------------- release


WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def test_the_release_pipeline_publishes_signs_and_attests() -> None:
    """CI built the image only to scan it and threw it away.

    Nothing that CI verified ever reached a registry, so the deployable artefact was
    whatever someone built on a laptop.
    """
    release = yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))

    # `on` is parsed by YAML as the boolean True. Accept either spelling.
    triggers = release.get("on") or release.get(True)
    assert list(triggers["push"]["tags"]) == ["v*"], "publishing must be a tagged decision"

    raw = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "cosign sign" in raw
    assert "attest-build-provenance" in raw
    # By digest, never by tag: a tag can point somewhere else tomorrow, which turns a
    # rollback into a lottery.
    assert "steps.build.outputs.digest" in raw
    assert "DIGEST: ${{ steps.build.outputs.digest }}" in raw


def test_the_release_runs_the_whole_gate_on_the_tag() -> None:
    """main being green says nothing about the commit being tagged."""
    release = yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))

    gate = yaml.safe_dump(release["jobs"]["verify"])
    for command in ("ruff check", "mypy", "pytest", "docs_check"):
        assert command in gate, command
    assert release["jobs"]["publish"]["needs"] == "verify"


# ------------------------------------------------------------ production override


PROD_OVERRIDE = REPO_ROOT / "deploy" / "compose" / "prod.override.yml"


def test_production_publishes_only_the_proxy() -> None:
    """One port on the host, and it terminates TLS.

    `ports: !override []` and not an omission: Compose merges list fields, so leaving
    `ports` out of the override would keep the base file's 8080 and the whole thing
    would be decorative.
    """
    override = _compose(PROD_OVERRIDE)

    published = {
        name: service.get("ports")
        for name, service in override["services"].items()
        if service.get("ports")
    }
    assert set(published) == {"caddy"}
    assert override["services"]["agent-api"]["ports"] == []
    assert override["services"]["grafana"]["ports"] == []


def test_production_turns_on_the_mode_that_refuses_a_dev_configuration() -> None:
    override = _compose(PROD_OVERRIDE)

    for name in ("agent-api", "ingestion-worker"):
        assert override["services"][name]["environment"]["AGENT_FORGE_ENV"] == "production"


def test_the_application_containers_cannot_write_to_their_own_filesystem() -> None:
    """Not the datastores: those are the ones that are supposed to write."""
    override = _compose(PROD_OVERRIDE)

    for name in ("agent-api", "ingestion-worker"):
        assert override["services"][name]["read_only"] is True
        assert override["services"][name]["tmpfs"], f"{name}: read_only with nowhere to write"

    for name in ("postgres", "redis", "qdrant"):
        assert "read_only" not in override["services"].get(name, {})


def test_the_proxy_config_exists_and_does_not_buffer_the_stream() -> None:
    """A proxy that buffers turns streaming chat into one late block of text."""
    caddyfile = (REPO_ROOT / "deploy" / "proxy" / "Caddyfile").read_text(encoding="utf-8")

    assert "flush_interval -1" in caddyfile
    assert "reverse_proxy agent-api:8080" in caddyfile


def test_every_service_the_production_override_names_exists_in_the_base() -> None:
    """An override for a service the base file does not define is silently inert."""
    base = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    override = _compose(PROD_OVERRIDE)

    # caddy is introduced by the override itself; everything else must already exist.
    unknown = set(override["services"]) - set(base["services"]) - {"caddy"}
    assert not unknown, unknown


# --------------------------------------------------------------------- alert rules


def _alert_rules() -> list[dict]:
    rules: list[dict] = []
    for path in sorted(RULES.glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for group in document["groups"]:
            for rule in group["rules"]:
                rule["_file"] = path.name
                rules.append(rule)
    return rules


def test_prometheus_actually_loads_the_rules_directory() -> None:
    """`rule_files` pointed at a directory that did not exist until B7."""
    config = yaml.safe_load((REPO_ROOT / "deploy/observability/prometheus.yml").read_text("utf-8"))
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))

    assert config["rule_files"] == ["/etc/prometheus/rules/*.yml"]
    mounts = compose["services"]["prometheus"]["volumes"]
    assert any("/etc/prometheus/rules" in mount for mount in mounts)
    assert list(RULES.glob("*.yml")), "rule_files matches nothing"


def test_every_alert_names_a_metric_the_cell_emits() -> None:
    """An alert on a metric nobody exports never fires, and looks exactly like health."""
    from agent_forge.observability.metrics import Metrics

    emitted = {collector.name for collector in Metrics().registry.collect()}

    for rule in _alert_rules():
        for token in _metric_tokens(rule["expr"]):
            assert token in emitted, f"{rule['_file']}/{rule['alert']}: {token}"


def _runbook_anchors() -> set[str]:
    """GitHub's heading-to-anchor rule: lowercase, drop punctuation, spaces to dashes."""
    import re
    import unicodedata

    anchors = set()
    text = (REPO_ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.startswith("#"):
            continue
        heading = line.lstrip("#").strip().lower()
        # Accents survive in GitHub anchors; punctuation and dots do not.
        slug = "".join(
            c
            for c in unicodedata.normalize("NFC", heading)
            if c.isalnum() or c in " -_" or unicodedata.combining(c)
        )
        anchors.add(re.sub(r"\s+", "-", slug.strip()))
    return anchors


def test_every_alert_links_to_a_runbook_section_that_exists() -> None:
    """If nobody would act on it, it is a dashboard panel, not an alert.

    And a link to a section that was never written is worse than no link: it reads like
    there is a procedure right up to the moment someone needs it.
    """
    anchors = _runbook_anchors()

    for rule in _alert_rules():
        assert rule["annotations"]["summary"], rule["alert"]
        anchor = rule["annotations"]["runbook"].split("#", 1)[1]
        assert anchor in anchors, f"{rule['alert']} points at RUNBOOK.md#{anchor}, absent"


def test_severity_is_page_only_for_damage_that_cannot_wait() -> None:
    """Waking someone for something that keeps until morning is how alerts get muted."""
    paging = {rule["alert"] for rule in _alert_rules() if rule["labels"]["severity"] == "page"}

    assert paging == {
        "AgentForgeCelulaCaida",
        "AgentForgeTareasFallando",
        "AgentForgeIntentoDeFugaC3C4",
        "AgentForgeLedgerSinEscrituras",
    }


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
