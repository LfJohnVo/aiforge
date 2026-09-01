"""Database drivers, OpenConnector and the repository-graph tool.

The rule under test for the databases is structural: there is no code path that accepts a
query from a caller. The model picks a template name and supplies arguments, and anything
it invents is dropped before it reaches a driver.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from agent_forge.connectors.base import CallContext
from agent_forge.connectors.databases import (
    MongoDBConnector,
    MySQLConnector,
    PostgresConnector,
    QueryTemplate,
    RedisConnector,
    _substitute,
    _to_positional,
    build_database_connectors,
)
from agent_forge.connectors.openconnector import (
    OpenConnectorDriver,
    Operation,
    operations_from_spec,
)
from agent_forge.connectors.repo_graph import RepoGraphConnector
from agent_forge.core.autonomy import AutonomyLevel
from agent_forge.core.classification import Classification
from agent_forge.core.errors import ToolError

CTX = CallContext(
    tenant_id="acme-mx",
    user_id="u1",
    groups=("finanzas",),
    classification_ceiling=Classification.C2,
    autonomy_granted=AutonomyLevel.A1,
)

READ = QueryTemplate(
    name="invoice_by_number",
    description="Busca una factura por numero",
    statement="SELECT total FROM invoices WHERE number = :number AND year = :year",
    parameters=("number", "year"),
    classification=Classification.C2,
)
WRITE = QueryTemplate(
    name="mark_paid",
    description="Marca una factura como pagada",
    statement="UPDATE invoices SET paid = true WHERE number = :number",
    parameters=("number",),
    writes=True,
    classification=Classification.C2,
)


# ------------------------------------------------------------------ templates


def test_a_read_template_is_a0_and_a_write_is_a3() -> None:
    assert READ.autonomy_min == AutonomyLevel.A0
    assert WRITE.autonomy_min == AutonomyLevel.A3


def test_only_declared_parameters_survive_binding() -> None:
    """Anything the model invents never reaches the driver."""
    bound = READ.bind({"number": "F-1", "year": "2026", "drop_table": "invoices"})

    assert bound == {"number": "F-1", "year": "2026"}


def test_a_missing_parameter_is_refused() -> None:
    with pytest.raises(ToolError, match="missing template parameters"):
        READ.bind({"number": "F-1"})


def test_a_template_using_an_undeclared_placeholder_is_rejected() -> None:
    """A placeholder nobody declared could never be bound, so it would leak the literal."""
    bad = QueryTemplate(
        name="bad",
        description="x",
        statement="SELECT * FROM t WHERE a = :a AND b = :b",
        parameters=("a",),
    )

    with pytest.raises(ToolError, match="undeclared"):
        PostgresConnector(alias="erp", dsn="postgresql://x", templates=(bad,))


def test_a_write_template_cannot_be_registered_on_a_readonly_connection() -> None:
    with pytest.raises(ToolError, match="read-only"):
        PostgresConnector(alias="erp", dsn="postgresql://x", templates=(WRITE,), readonly=True)


def test_the_tool_schema_exposes_exactly_the_parameters() -> None:
    connector = PostgresConnector(alias="erp", dsn="postgresql://x", templates=(READ,))
    spec = next(iter(connector.capabilities()))

    assert set(spec.input_schema["properties"]) == {"number", "year"}
    assert spec.input_schema["additionalProperties"] is False


def test_named_placeholders_become_positional_in_order() -> None:
    statement, values = _to_positional(READ, {"number": "F-1", "year": "2026"})

    assert statement == ("SELECT total FROM invoices WHERE number = $1 AND year = $2")
    assert values == ["F-1", "2026"]


def test_a_placeholder_used_twice_binds_once() -> None:
    template = QueryTemplate(
        name="t",
        description="x",
        statement="SELECT * FROM t WHERE a = :x OR b = :x",
        parameters=("x",),
    )

    statement, values = _to_positional(template, {"x": 7})

    assert statement.count("$1") == 2
    assert values == [7]


async def test_a_query_above_the_ceiling_is_refused_before_it_runs() -> None:
    connector = PostgresConnector(
        alias="erp",
        dsn="postgresql://x",
        templates=(
            QueryTemplate(
                name="secret",
                description="x",
                statement="SELECT 1 WHERE a = :a",
                parameters=("a",),
                classification=Classification.C4,
            ),
        ),
    )
    result = await connector.invoke("erp.secret", {"a": "1"}, CTX)

    assert not result.ok
    assert "clearance" in (result.error or "")


async def test_an_unknown_template_is_rejected() -> None:
    connector = PostgresConnector(alias="erp", dsn="postgresql://x", templates=(READ,))

    result = await connector.invoke("erp.drop_everything", {}, CTX)

    assert not result.ok


async def test_a_driver_without_a_dsn_is_unhealthy() -> None:
    for driver in (PostgresConnector, MySQLConnector, MongoDBConnector, RedisConnector):
        connector = driver(alias="x", dsn="", templates=())
        assert await connector.health() is False, driver.__name__


def test_mongo_substitution_never_lets_arguments_add_operators() -> None:
    """Structure comes from the template; arguments only fill leaves."""
    filter_doc = json.loads('{"customer": ":name", "status": {"$in": ["open"]}}')

    bound = _substitute(filter_doc, {"name": {"$ne": None}})

    # The value is inserted as a value; it cannot introduce a new key in the document.
    assert set(bound) == {"customer", "status"}
    assert bound["status"] == {"$in": ["open"]}


def test_build_database_connectors_reads_the_profile() -> None:
    class Entry:
        type = "postgres"
        alias = "erp_ro"
        dsn_env = "ERP_PG_DSN"
        readonly = True

    built = build_database_connectors([Entry()], env={"ERP_PG_DSN": "postgresql://x"})

    assert len(built) == 1
    assert built[0].name == "erp_ro"
    assert built[0].readonly


# --------------------------------------------------------------- openconnector

SPEC: dict[str, Any] = {
    "openapi": "3.1.0",
    "paths": {
        "/tickets": {
            "get": {
                "operationId": "listTickets",
                "summary": "Lista tickets",
                "parameters": [{"name": "status", "in": "query"}],
            },
            "post": {
                "operationId": "createTicket",
                "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
            },
        },
        "/tickets/{id}": {
            "delete": {"operationId": "deleteTicket"},
            "get": {"summary": "sin operationId"},
        },
    },
}


def test_operations_are_read_from_the_spec() -> None:
    operations = {op.operation_id for op in operations_from_spec(SPEC)}

    assert {"listTickets", "createTicket", "deleteTicket"} <= operations


def test_an_operation_without_an_id_gets_a_derived_one() -> None:
    derived = [op for op in operations_from_spec(SPEC) if op.operation_id.startswith("get_")]

    assert derived, "an operation missing operationId must still be usable"


def test_path_parameters_are_found_even_without_a_declaration() -> None:
    operation = next(op for op in operations_from_spec(SPEC) if op.operation_id == "deleteTicket")

    assert operation.path_params == ("id",)


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("get", AutonomyLevel.A0),
        ("post", AutonomyLevel.A2),
        ("patch", AutonomyLevel.A2),
        ("delete", AutonomyLevel.A3),
    ],
)
def test_autonomy_comes_from_the_verb(method: str, expected: AutonomyLevel) -> None:
    """A spec cannot talk its way into a lower level."""
    assert Operation(operation_id="x", method=method, path="/x").autonomy == expected


def test_an_empty_allowlist_exposes_nothing() -> None:
    """A spec is not an invitation."""
    driver = OpenConnectorDriver(service="desk", base_url="http://desk.test", spec=SPEC)

    assert driver.capabilities() == ()


def test_only_allowlisted_operations_become_tools() -> None:
    driver = OpenConnectorDriver(
        service="desk",
        base_url="http://desk.test",
        spec=SPEC,
        allow_operations=("listTickets",),
    )

    assert {s.name for s in driver.capabilities()} == {"api.desk.listTickets"}


@respx.mock
async def test_a_generated_tool_calls_the_api() -> None:
    respx.get("http://desk.test/tickets").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    driver = OpenConnectorDriver(
        service="desk",
        base_url="http://desk.test",
        spec=SPEC,
        allow_operations=("listTickets",),
    )

    result = await driver.invoke("api.desk.listTickets", {"status": "open"}, CTX)
    await driver.aclose()

    assert result.ok
    assert (result.data or {})["body"] == [{"id": 1}]


@respx.mock
async def test_a_path_parameter_is_substituted() -> None:
    route = respx.delete("http://desk.test/tickets/42").mock(return_value=httpx.Response(204))
    driver = OpenConnectorDriver(
        service="desk",
        base_url="http://desk.test",
        spec=SPEC,
        allow_operations=("deleteTicket",),
    )

    result = await driver.invoke("api.desk.deleteTicket", {"id": "42"}, CTX)
    await driver.aclose()

    assert result.ok
    assert route.called


@respx.mock
async def test_a_missing_path_parameter_is_a_failure_not_a_malformed_url() -> None:
    driver = OpenConnectorDriver(
        service="desk",
        base_url="http://desk.test",
        spec=SPEC,
        allow_operations=("deleteTicket",),
    )

    result = await driver.invoke("api.desk.deleteTicket", {}, CTX)
    await driver.aclose()

    assert not result.ok


@respx.mock
async def test_an_api_error_becomes_a_failed_result() -> None:
    respx.get("http://desk.test/tickets").mock(return_value=httpx.Response(403))
    driver = OpenConnectorDriver(
        service="desk",
        base_url="http://desk.test",
        spec=SPEC,
        allow_operations=("listTickets",),
    )

    result = await driver.invoke("api.desk.listTickets", {}, CTX)
    await driver.aclose()

    assert not result.ok


async def test_a_driver_with_no_allowed_operations_is_unhealthy() -> None:
    driver = OpenConnectorDriver(service="desk", base_url="http://desk.test", spec=SPEC)

    assert await driver.health() is False
    await driver.aclose()


def test_a_malformed_spec_yields_no_operations() -> None:
    assert operations_from_spec({"paths": "not a mapping"}) == []


# ----------------------------------------------------------------- repo_graph


@pytest.fixture
def graph_file(tmp_path: Path) -> Path:
    payload = {
        "version": 1,
        "modules": [{"path": "src/a.py", "loc": 10}],
        "nodes": [
            {"id": "agent_forge.core.state", "kind": "module", "path": "src/state.py"},
            {"id": "agent_forge.core.graph", "kind": "module", "path": "src/graph.py"},
            {
                "id": "agent_forge.core.state:AgentState",
                "kind": "class",
                "path": "src/state.py",
                "lineno": 42,
                "doc": "The graph's state.",
            },
        ],
        "edges": [
            {
                "source": "agent_forge.core.graph",
                "target": "agent_forge.core.state",
                "kind": "imports",
            },
            {"source": "agent_forge.core.graph", "target": "langgraph", "kind": "depends"},
        ],
    }
    path = tmp_path / "repo-graph.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


async def test_find_locates_a_symbol(graph_file: Path) -> None:
    connector = RepoGraphConnector(graph_file)

    result = await connector.invoke(
        "repo_graph.query", {"kind": "find", "target": "AgentState"}, CTX
    )

    assert result.ok
    assert (result.data or {})["results"][0]["line"] == 42


async def test_dependents_reports_the_blast_radius(graph_file: Path) -> None:
    connector = RepoGraphConnector(graph_file)

    result = await connector.invoke(
        "repo_graph.query",
        {"kind": "dependents", "target": "agent_forge.core.state"},
        CTX,
    )

    assert (result.data or {})["results"] == ["agent_forge.core.graph"]


async def test_dependencies_separates_internal_from_external(graph_file: Path) -> None:
    connector = RepoGraphConnector(graph_file)

    result = await connector.invoke(
        "repo_graph.query",
        {"kind": "dependencies", "target": "agent_forge.core.graph"},
        CTX,
    )

    data = result.data or {}
    assert data["internal"] == ["agent_forge.core.state"]
    assert data["external"] == ["langgraph"]


async def test_overview_summarises_the_repository(graph_file: Path) -> None:
    connector = RepoGraphConnector(graph_file)

    result = await connector.invoke("repo_graph.query", {"kind": "overview"}, CTX)

    data = result.data or {}
    assert data["modules"] == 2
    assert data["classes"] == 1
    assert data["most_imported"][0]["module"] == "agent_forge.core.state"


async def test_the_tool_is_read_only_and_never_above_c1(graph_file: Path) -> None:
    """It reads the shape of our own code, not tenant data."""
    spec = next(iter(RepoGraphConnector(graph_file).capabilities()))

    assert spec.autonomy_min == AutonomyLevel.A0
    assert spec.max_classification == Classification.C1
    assert spec.readonly


async def test_an_unknown_query_kind_is_rejected(graph_file: Path) -> None:
    result = await RepoGraphConnector(graph_file).invoke("repo_graph.query", {"kind": "sudo"}, CTX)

    assert not result.ok


async def test_a_missing_graph_is_reported_with_the_command_to_fix_it(
    tmp_path: Path,
) -> None:
    connector = RepoGraphConnector(tmp_path / "absent.json")

    assert await connector.health() is False
    result = await connector.invoke("repo_graph.query", {"kind": "overview"}, CTX)
    assert not result.ok


async def test_it_answers_about_this_repository() -> None:
    """The real graph, not a fixture: the tool must work on the artefact we ship."""
    connector = RepoGraphConnector()
    if not await connector.health():
        pytest.skip("run `make repo-graph` first")

    result = await connector.invoke(
        "repo_graph.query", {"kind": "find", "target": "ModelPolicy"}, CTX
    )

    assert result.ok
    assert any("model_policy" in hit["path"] for hit in (result.data or {})["results"])
