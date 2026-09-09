"""The manual classification override, and the asymmetry that makes it safe.

`docs/KNOWLEDGE.md` has promised a manual override since F3 and `THREAT_MODEL.md` lists
it as the compensating control for the automatic classifier getting it wrong. No such
mechanism existed: every document in a source inherited one classification from the
profile, so the control the threat model leans on was a sentence.

The rule that makes honouring a document's own declaration safe is that it may only
**raise**. THREAT_MODEL assumption 4 is that an ingested document may be hostile even
when it comes from a corporate source; if front matter could lower classification,
anyone able to drop a file into the corpus could declassify it by typing `C0`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_forge.core.classification import Classification
from agent_forge.knowledge.sources import (
    FolderSourceReader,
    SyncCursor,
    effective_classification,
    parse_front_matter,
)

pytestmark = pytest.mark.anyio

DOCUMENT = """---
title: Plan de adquisicion
classification: C3
acl_groups: [direccion]
---

Se evalua la adquisicion de Nortec por 180 millones.
"""


# --------------------------------------------------------------------- parsing


def test_a_declaration_is_read_and_stripped_from_the_indexed_text() -> None:
    """Left in, the YAML header competes with the prose for the citation."""
    parsed = parse_front_matter(DOCUMENT)

    assert parsed.classification is Classification.C3
    assert parsed.title == "Plan de adquisicion"
    assert "classification:" not in parsed.body
    assert parsed.body.strip().startswith("Se evalua")


def test_a_document_without_front_matter_is_left_exactly_as_it_is() -> None:
    text = "# Politica\n\nEl limite es 1500 MXN.\n"

    parsed = parse_front_matter(text)

    assert parsed.classification is None
    assert parsed.body == text


def test_a_malformed_header_does_not_lose_the_document() -> None:
    """The author believed they were declaring something; skipping the file hides that."""
    text = "---\nclassification: [C3\n---\n\nCuerpo.\n"

    parsed = parse_front_matter(text)

    assert parsed.classification is None
    assert "Cuerpo" in parsed.body


def test_an_unknown_level_is_ignored_rather_than_guessed() -> None:
    parsed = parse_front_matter("---\nclassification: SECRETISIMO\n---\n\nCuerpo.\n")

    assert parsed.classification is None


def test_a_line_that_merely_looks_like_a_separator_is_not_front_matter() -> None:
    """A horizontal rule mid-document must not swallow half the text."""
    text = "Primera linea.\n\n---\n\nSegunda.\n"

    assert parse_front_matter(text).body == text


# ------------------------------------------------------------------- the rule


def test_a_document_may_raise_its_own_classification() -> None:
    assert effective_classification(Classification.C3, Classification.C2) is Classification.C3


def test_a_document_may_never_lower_it() -> None:
    """The whole reason the override is safe to honour at all."""
    assert effective_classification(Classification.C0, Classification.C2) is Classification.C2


def test_no_declaration_leaves_the_source_default_in_place() -> None:
    assert effective_classification(None, Classification.C2) is Classification.C2


# ------------------------------------------------------------- through the reader


async def test_the_folder_reader_applies_the_declaration(tmp_path: Path) -> None:
    (tmp_path / "reservado.md").write_text(DOCUMENT, encoding="utf-8")
    (tmp_path / "normal.md").write_text("Sin cabecera.\n", encoding="utf-8")

    reader = FolderSourceReader(
        tmp_path,
        tenant_id="acme-mx",
        acl_groups=["direccion"],
        default_classification=Classification.C1,
    )
    documents = {doc.title: doc async for doc in reader.read(SyncCursor())}

    assert documents["Plan de adquisicion"].classification is Classification.C3
    assert documents["normal"].classification is Classification.C1


async def test_a_hostile_document_cannot_declassify_itself(tmp_path: Path) -> None:
    """The attack the rule exists for: a file dropped into a C2 corpus claiming C0."""
    (tmp_path / "hostil.md").write_text(
        "---\nclassification: C0\n---\n\nDatos del cliente.\n", encoding="utf-8"
    )

    reader = FolderSourceReader(
        tmp_path, tenant_id="acme-mx", default_classification=Classification.C2
    )
    documents = [doc async for doc in reader.read(SyncCursor())]

    assert documents[0].classification is Classification.C2


async def test_changing_only_the_declaration_still_counts_as_a_change(tmp_path: Path) -> None:
    """Hashing the stripped body would make a reclassification invisible to the re-sync."""
    path = tmp_path / "doc.md"
    path.write_text("---\nclassification: C1\n---\n\nCuerpo estable.\n", encoding="utf-8")
    reader = FolderSourceReader(
        tmp_path, tenant_id="acme-mx", default_classification=Classification.C1
    )

    cursor = SyncCursor()
    assert [d async for d in reader.read(cursor)], "first pass should yield the document"
    assert not [d async for d in reader.read(cursor)], "unchanged file should be skipped"

    path.write_text("---\nclassification: C3\n---\n\nCuerpo estable.\n", encoding="utf-8")
    again = [d async for d in reader.read(cursor)]

    assert len(again) == 1
    assert again[0].classification is Classification.C3


# ------------------------------------------------------- and through the pipeline


async def test_the_classifier_cannot_lower_what_the_document_arrived_with() -> None:
    """The last place the override was being silently discarded.

    The reader set C3 from the front matter, then the pipeline overwrote it with the
    classifier's verdict -- which, with no model reachable, is the profile default. The
    document was indexed as C2 and nothing said so.
    """
    from agent_forge.knowledge.classifier import ClassificationResult, Classifier
    from agent_forge.knowledge.documents import Document, SourceRef
    from agent_forge.knowledge.ingestion import IngestionPipeline
    from agent_forge.knowledge.rag.embeddings import HashingEmbeddings

    class _AlwaysC0(Classifier):
        async def classify(self, document: Document) -> ClassificationResult:
            return ClassificationResult(classification=Classification.C0, method="default")

    indexed: list[object] = []

    class _Store:
        async def upsert(self, chunks: object) -> None:
            indexed.extend(chunks)  # type: ignore[arg-type]

        async def delete_source(self, *a: object, **k: object) -> int:
            return 0

        async def aclose(self) -> None: ...

    pipeline = IngestionPipeline(
        vector_store=_Store(),  # type: ignore[arg-type]
        embeddings=HashingEmbeddings(),
        classifier=_AlwaysC0(),
    )
    document = Document(
        source=SourceRef(kind="folder", locator="/data/corpus/direccion/plan.md"),
        tenant_id="acme-mx",
        title="Plan",
        text="Se evalua la adquisicion de Nortec por 180 millones de pesos.",
        classification=Classification.C3,
    )

    chunks = await pipeline.ingest_document(document)

    assert chunks
    assert all(c.classification is Classification.C3 for c in chunks)
