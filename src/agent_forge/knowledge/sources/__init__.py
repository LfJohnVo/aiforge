"""Ingestion sources: where documents come from.

Every source yields ``Document`` objects with their ACL and, where the origin knows it,
their sensitivity label. Two rules:

* **Incremental by default.** A source reports what changed, not everything it has.
  Re-embedding an unchanged corpus every six hours is the difference between a cron job
  and a bill.
* **The ACL comes from the origin when the origin has one.** SharePoint knows who can see
  a file; a folder does not, so the profile supplies the default. A source that cannot
  determine an ACL uses the configured default and never an empty one.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

from agent_forge.core.classification import Classification
from agent_forge.core.errors import AgentForgeError
from agent_forge.knowledge.documents import AccessControl, Document, SourceRef
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "FolderSourceReader",
    "FrontMatter",
    "S3SourceReader",
    "SharePointSourceReader",
    "SourceError",
    "SourceReader",
    "SyncCursor",
    "build_reader",
    "effective_classification",
    "parse_front_matter",
    "parse_text",
]

# Extensions the built-in parser handles without Docling. Anything else needs the
# `knowledge` extra; a source that hits one without it logs and skips rather than
# pretending the document was ingested.
PLAIN_SUFFIXES = frozenset(
    {".txt", ".md", ".markdown", ".csv", ".json", ".yaml", ".yml", ".html", ".htm", ".rst", ".log"}
)
DOCLING_SUFFIXES = frozenset({".pdf", ".docx", ".pptx", ".xlsx", ".doc", ".ppt", ".xls"})


class SourceError(AgentForgeError):
    """A source could not be read."""

    code = "source_unavailable"


@dataclass(slots=True)
class SyncCursor:
    """Where the last sync got to, so the next one is incremental.

    ``token`` is whatever the origin uses (a Graph delta link, an S3 continuation).
    ``seen`` maps source_id to content hash for origins that have no delta mechanism.
    """

    token: str = ""
    seen: dict[str, str] = field(default_factory=dict)
    last_run: datetime | None = None

    def unchanged(self, source_id: str, content_hash: str) -> bool:
        return self.seen.get(source_id) == content_hash

    def record(self, source_id: str, content_hash: str) -> None:
        self.seen[source_id] = content_hash

    def to_payload(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "seen": self.seen,
            "last_run": self.last_run.isoformat() if self.last_run else None,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> SyncCursor:
        data = payload or {}
        raw = data.get("last_run")
        return cls(
            token=str(data.get("token", "")),
            seen=dict(data.get("seen") or {}),
            last_run=datetime.fromisoformat(raw) if raw else None,
        )


@runtime_checkable
class SourceReader(Protocol):
    """Yields documents that changed since the cursor."""

    kind: str

    def read(self, cursor: SyncCursor) -> AsyncIterator[Document]: ...

    async def health(self) -> bool: ...


# ------------------------------------------------------------------- parsing


def parse_text(path: Path, raw: bytes) -> str | None:
    """Extract text. Docling for rich formats when installed, built-in otherwise.

    Returns None when the format cannot be handled, so the caller can skip and say so.
    Returning an empty string instead would silently ingest a blank document.
    """
    suffix = path.suffix.lower()
    if suffix in PLAIN_SUFFIXES:
        return raw.decode("utf-8", errors="replace")
    if suffix in DOCLING_SUFFIXES:
        return _parse_with_docling(path)
    return None


def _parse_with_docling(path: Path) -> str | None:
    try:
        from docling.document_converter import DocumentConverter
    except ImportError:
        log.warning(
            "ingestion.rich_format_skipped",
            suffix=path.suffix,
            detail="install the `knowledge` extra to parse PDF and Office documents",
        )
        return None
    try:
        result = DocumentConverter().convert(str(path))
    except Exception as exc:
        log.warning("ingestion.parse_failed", path=path.name, detail=type(exc).__name__)
        return None
    return str(result.document.export_to_markdown())


# -------------------------------------------------------------------- folder


class FolderSourceReader:
    """A local or mounted directory. The source that always works."""

    kind = "folder"

    def __init__(
        self,
        root: str | Path,
        *,
        tenant_id: str,
        acl_groups: Sequence[str] = (),
        default_classification: Classification = Classification.C2,
        max_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        self._root = Path(root)
        self._tenant_id = tenant_id
        self._acl = AccessControl(groups=frozenset(acl_groups))
        self._default = default_classification
        self._max_bytes = max_bytes

    async def read(self, cursor: SyncCursor) -> AsyncIterator[Document]:
        if not self._root.is_dir():
            raise SourceError("corpus directory not found", path=str(self._root))

        for path in sorted(self._root.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.stat().st_size > self._max_bytes:
                log.warning("ingestion.file_too_large", path=path.name, size=path.stat().st_size)
                continue

            text = parse_text(path, path.read_bytes())
            if text is None or not text.strip():
                continue

            relative = path.relative_to(self._root).as_posix()
            source = SourceRef(kind=self.kind, locator=f"{self._root.as_posix()}/{relative}")
            # Hash the file as written, header included: a change to the declared
            # classification has to count as a change, or the re-sync skips it.
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
            if cursor.unchanged(source.source_id, content_hash):
                continue
            cursor.record(source.source_id, content_hash)

            declared = parse_front_matter(text, source_id=source.source_id)
            yield Document(
                source=source,
                tenant_id=self._tenant_id,
                title=declared.title or path.stem.replace("_", " ").replace("-", " "),
                text=declared.body,
                acl=self._acl,
                classification=effective_classification(
                    declared.classification, self._default, source_id=source.source_id
                ),
                updated_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
                metadata={"path": relative, "suffix": path.suffix.lower()},
            )

    async def health(self) -> bool:
        return self._root.is_dir()


# ------------------------------------------------------------------ front matter

# `---` ... `---` at the very top of a text document. Markdown corpora carry it as a
# matter of course, and it is the only place a *document author* can say something the
# pipeline should obey.
_FRONT_MATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)


@dataclass(frozen=True, slots=True)
class FrontMatter:
    """What a document declares about itself. Everything is optional."""

    classification: Classification | None = None
    title: str = ""
    body: str = ""


def parse_front_matter(text: str, *, source_id: str = "") -> FrontMatter:
    """Read the document's own declaration, and strip it from the indexed text.

    `docs/KNOWLEDGE.md` has promised a **manual override** of classification since F3 and
    `THREAT_MODEL.md` lists it as the compensating control for the classifier getting it
    wrong. Until now no such mechanism existed: every document in a source inherited one
    classification from the profile.

    Stripping matters as much as parsing. Left in, the YAML block becomes part of the
    first chunk, so a search for "classification" matches every document in the corpus and
    the header competes with the prose for the citation.
    """
    match = _FRONT_MATTER.match(text)
    if match is None:
        return FrontMatter(body=text)

    body = text[match.end() :]
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        # A malformed header is not a reason to skip a document, but it is a reason to
        # say so: the author believed they were declaring something.
        log.warning("ingestion.front_matter_invalid", source=source_id, detail=str(exc))
        return FrontMatter(body=body)

    if not isinstance(parsed, dict):
        return FrontMatter(body=body)

    declared: Classification | None = None
    raw = parsed.get("classification")
    if raw is not None:
        try:
            declared = Classification.parse(raw)
        except ValueError:
            log.warning("ingestion.front_matter_unknown_class", source=source_id, got=str(raw))

    title = parsed.get("title")
    return FrontMatter(
        classification=declared,
        title=str(title) if isinstance(title, str | int | float) else "",
        body=body,
    )


def effective_classification(
    declared: Classification | None, default: Classification, *, source_id: str = ""
) -> Classification:
    """The document's declaration may only **raise** the classification, never lower it.

    This is the asymmetry that makes the override safe to honour. THREAT_MODEL assumption
    4 is that an ingested document may be hostile even from a corporate source; if front
    matter could lower classification, anyone able to drop a file into the corpus could
    declassify it by typing `classification: C0`. Raising is a different act: the worst a
    hostile document achieves is making itself *less* reachable.
    """
    if declared is None or declared <= default:
        if declared is not None and declared < default:
            log.info(
                "ingestion.front_matter_ignored",
                source=source_id,
                declared=str(declared),
                applied=str(default),
                detail="a document may raise its classification, never lower it",
            )
        return default
    return declared


# ---------------------------------------------------------------- sharepoint


class SharePointSourceReader:
    """SharePoint and OneDrive through Microsoft Graph, using delta queries.

    Delta is the whole point: Graph returns only what changed since the stored token, so
    a six-hourly sync over a large site costs almost nothing. The ACL comes from the
    item's own permissions, not from the profile, because SharePoint is the authority on
    who may read a file.
    """

    kind = "sharepoint"

    def __init__(
        self,
        *,
        site: str,
        drives: Sequence[str],
        tenant_id: str,
        graph_tenant: str,
        client_id: str,
        client_secret: str,
        fallback_groups: Sequence[str] = (),
        default_classification: Classification = Classification.C2,
    ) -> None:
        self._site = site
        self._drives = list(drives)
        self._tenant_id = tenant_id
        self._fallback_acl = AccessControl(groups=frozenset(fallback_groups))
        self._default = default_classification
        self._credentials = (graph_tenant, client_id, client_secret)
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from azure.identity.aio import ClientSecretCredential
            from msgraph import GraphServiceClient
        except ImportError as exc:
            raise SourceError(
                "the `sources` extra is required to ingest from SharePoint",
                hint="uv sync --extra sources",
            ) from exc
        tenant, client_id, secret = self._credentials
        if not all((tenant, client_id, secret)):
            raise SourceError(
                "SharePoint ingestion needs GRAPH_TENANT_ID, GRAPH_CLIENT_ID and "
                "GRAPH_CLIENT_SECRET"
            )
        credential = ClientSecretCredential(tenant, client_id, secret)
        self._client = GraphServiceClient(
            credentials=credential, scopes=["https://graph.microsoft.com/.default"]
        )
        return self._client

    async def read(self, cursor: SyncCursor) -> AsyncIterator[Document]:
        client = self._ensure_client()
        for drive_id in self._drives:
            async for document in self._read_drive(client, drive_id, cursor):
                yield document

    async def _read_drive(
        self, client: Any, drive_id: str, cursor: SyncCursor
    ) -> AsyncIterator[Document]:
        request = client.drives.by_drive_id(drive_id).root.delta
        page = await (request.with_url(cursor.token).get() if cursor.token else request.get())
        while page is not None:
            for item in page.value or []:
                document = await self._to_document(client, drive_id, item)
                if document is not None:
                    yield document
            next_link = getattr(page, "odata_next_link", None)
            if next_link:
                page = await request.with_url(next_link).get()
                continue
            # The delta link is the cursor for the *next* run.
            cursor.token = str(getattr(page, "odata_delta_link", "") or cursor.token)
            page = None

    async def _to_document(self, client: Any, drive_id: str, item: Any) -> Document | None:
        name = getattr(item, "name", "") or ""
        if getattr(item, "folder", None) is not None or not name:
            return None
        suffix = Path(name).suffix.lower()
        if suffix not in PLAIN_SUFFIXES | DOCLING_SUFFIXES:
            return None

        content = (
            await client.drives.by_drive_id(drive_id).items.by_drive_item_id(item.id).content.get()
        )
        text = parse_text(Path(name), bytes(content or b""))
        if text is None or not text.strip():
            return None

        return Document(
            source=SourceRef(kind=self.kind, locator=f"{self._site}/{drive_id}/{item.id}"),
            tenant_id=self._tenant_id,
            title=Path(name).stem,
            text=text,
            acl=self._acl_for(item),
            classification=self._default,
            updated_at=getattr(item, "last_modified_date_time", None) or datetime.now(UTC),
            metadata={
                "drive_id": drive_id,
                "web_url": getattr(item, "web_url", ""),
                "sensitivity_label": _sensitivity_label(item),
            },
        )

    def _acl_for(self, item: Any) -> AccessControl:
        """Read the item's own permissions; fall back to the profile default, never open."""
        groups: set[str] = set()
        users: set[str] = set()
        for permission in getattr(item, "permissions", None) or []:
            identity = getattr(permission, "granted_to_v2", None)
            if identity is None:
                continue
            group = getattr(identity, "group", None)
            user = getattr(identity, "user", None)
            if group is not None and getattr(group, "display_name", None):
                groups.add(str(group.display_name))
            if user is not None and getattr(user, "id", None):
                users.add(str(user.id))
        if not groups and not users:
            return self._fallback_acl
        return AccessControl(groups=frozenset(groups), users=frozenset(users))

    async def health(self) -> bool:
        try:
            self._ensure_client()
        except SourceError:
            return False
        return True


def _sensitivity_label(item: Any) -> str:
    label = getattr(item, "sensitivity_label", None)
    return str(getattr(label, "display_name", "") or "") if label else ""


# ------------------------------------------------------------------------ s3


class S3SourceReader:
    """S3-compatible object storage. Incremental by ETag."""

    kind = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        tenant_id: str,
        endpoint_url: str = "",
        access_key: str = "",
        secret_key: str = "",
        acl_groups: Sequence[str] = (),
        default_classification: Classification = Classification.C2,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix
        self._tenant_id = tenant_id
        self._endpoint = endpoint_url
        self._keys = (access_key, secret_key)
        self._acl = AccessControl(groups=frozenset(acl_groups))
        self._default = default_classification

    def _session(self) -> Any:
        try:
            import aiobotocore.session
        except ImportError as exc:
            raise SourceError(
                "the `sources` extra is required to ingest from S3",
                hint="uv sync --extra sources",
            ) from exc
        return aiobotocore.session.get_session()

    async def read(self, cursor: SyncCursor) -> AsyncIterator[Document]:
        access_key, secret_key = self._keys
        session = self._session()
        async with session.create_client(
            "s3",
            endpoint_url=self._endpoint or None,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
        ) as client:
            paginator = client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=self._bucket, Prefix=self._prefix):
                for entry in page.get("Contents", []):
                    document = await self._to_document(client, entry, cursor)
                    if document is not None:
                        yield document

    async def _to_document(
        self, client: Any, entry: dict[str, Any], cursor: SyncCursor
    ) -> Document | None:
        key = str(entry["Key"])
        if key.endswith("/"):
            return None
        source = SourceRef(kind=self.kind, locator=f"{self._bucket}/{key}")
        etag = str(entry.get("ETag", "")).strip('"')
        if cursor.unchanged(source.source_id, etag):
            return None

        obj = await client.get_object(Bucket=self._bucket, Key=key)
        raw = await obj["Body"].read()
        text = parse_text(Path(key), raw)
        if text is None or not text.strip():
            return None
        cursor.record(source.source_id, etag)

        return Document(
            source=source,
            tenant_id=self._tenant_id,
            title=Path(key).stem,
            text=text,
            acl=self._acl,
            classification=self._default,
            updated_at=entry.get("LastModified") or datetime.now(UTC),
            metadata={"bucket": self._bucket, "key": key},
        )

    async def health(self) -> bool:
        try:
            self._session()
        except SourceError:
            return False
        return bool(self._bucket)


# ---------------------------------------------------------------------- build


def build_reader(
    source: Any, *, tenant_id: str, env: dict[str, str], default: Classification
) -> SourceReader:
    """Build the reader a profile source entry describes."""
    classification = source.default_classification or default
    if source.type == "folder":
        return FolderSourceReader(
            source.path,
            tenant_id=tenant_id,
            acl_groups=source.default_acl_groups,
            default_classification=classification,
        )
    if source.type == "sharepoint":
        return SharePointSourceReader(
            site=source.site,
            drives=source.drives,
            tenant_id=tenant_id,
            graph_tenant=env.get("GRAPH_TENANT_ID", ""),
            client_id=env.get("GRAPH_CLIENT_ID", ""),
            client_secret=env.get("GRAPH_CLIENT_SECRET", ""),
            fallback_groups=source.default_acl_groups,
            default_classification=classification,
        )
    if source.type == "s3":
        return S3SourceReader(
            bucket=source.bucket,
            prefix=source.prefix,
            tenant_id=tenant_id,
            endpoint_url=env.get("S3_ENDPOINT_URL", ""),
            access_key=env.get("S3_ACCESS_KEY_ID", ""),
            secret_key=env.get("S3_SECRET_ACCESS_KEY", ""),
            acl_groups=source.default_acl_groups,
            default_classification=classification,
        )
    raise SourceError("unknown source type", type=source.type)
