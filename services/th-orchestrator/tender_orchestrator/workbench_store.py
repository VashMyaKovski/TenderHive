import asyncio
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import JsonValue, StringConstraints

from contracts.agent_task import AgentResult
from contracts.models import Contract, Document, UtcDatetime
from tender_orchestrator.db import Database
from tender_orchestrator.models import JobView
from tender_orchestrator.workbench_extraction import ExtractionReview
from tender_orchestrator.workbench_flow import Run
from tender_orchestrator.workbench_inputs import DecisionInput, Facts, Upload

type Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class RunState(Contract):
    id: UUID
    created_at: UtcDatetime
    title: str
    facts: Facts | None
    context: str | None
    uploads: list[Document]
    normalization: Document
    view: JobView | None
    extraction: AgentResult | None
    extraction_review: ExtractionReview | None
    extraction_confirmed: bool
    pending: bool
    pending_decision: DecisionInput | None
    error: str | None
    messages: list[dict[str, JsonValue]]
    artifacts: dict[str, Digest]


class WorkbenchStore:
    def __init__(self, database: Database, storage: Path) -> None:
        self._database = database
        self._storage = storage

    async def ready(self) -> None:
        await asyncio.to_thread(self._storage.mkdir, parents=True, exist_ok=True)
        async with self._database.connect() as connection:
            await connection.execute("SELECT 1 FROM th_orchestrator.workbench_runs LIMIT 1")

    async def save(self, run: Run) -> None:
        artifacts = {name: sha256(data).hexdigest() for name, data in run.artifacts.items()}
        uploads = [*run.uploads, run.normalization]
        files = {item.document.checksum: item.data for item in uploads}
        files.update({digest: run.artifacts[name] for name, digest in artifacts.items()})
        await asyncio.to_thread(self._write, run.id, files)
        state = RunState(
            id=run.id,
            created_at=run.created_at,
            title=run.title,
            facts=run.facts,
            context=run.context,
            uploads=[item.document for item in run.uploads],
            normalization=run.normalization.document,
            view=run.view,
            extraction=run.extraction,
            extraction_review=run.extraction_review,
            extraction_confirmed=run.extraction_confirmed,
            pending=run.pending,
            pending_decision=run.pending_decision,
            error=run.error,
            messages=run.messages,
            artifacts=artifacts,
        )
        async with self._database.connect() as connection:
            await connection.execute(
                """
                INSERT INTO th_orchestrator.workbench_runs (run_id, created_at, pending, snapshot)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE
                SET pending = EXCLUDED.pending, snapshot = EXCLUDED.snapshot
                """,
                (run.id, run.created_at, run.pending, Jsonb(state.model_dump(mode="json"))),
            )

    async def get(self, run_id: UUID) -> Run | None:
        async with self._database.connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT snapshot FROM th_orchestrator.workbench_runs WHERE run_id = %s",
                    (run_id,),
                )
            ).fetchone()
        if row is None:
            return None
        return await asyncio.to_thread(self._restore, RunState.model_validate(row["snapshot"]))

    async def pending_ids(self) -> list[UUID]:
        async with self._database.connect() as connection:
            rows = await (
                await connection.execute(
                    "SELECT run_id FROM th_orchestrator.workbench_runs "
                    "WHERE pending ORDER BY created_at"
                )
            ).fetchall()
        return [row["run_id"] for row in rows]

    def _write(self, run_id: UUID, files: dict[str, bytes]) -> None:
        directory = self._storage / str(run_id)
        directory.mkdir(exist_ok=True)
        for digest, data in files.items():
            target = directory / digest
            if target.exists():
                continue
            with NamedTemporaryFile(dir=directory, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
            try:
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)

    def _restore(self, item: RunState) -> Run:
        directory = self._storage / str(item.id)

        def upload(document: Document) -> Upload:
            return Upload(document, (directory / document.checksum).read_bytes())

        return Run(
            id=item.id,
            title=item.title,
            facts=item.facts,
            context=item.context,
            uploads=[upload(document) for document in item.uploads],
            normalization=upload(item.normalization),
            view=item.view,
            extraction=item.extraction,
            error=item.error,
            messages=item.messages,
            artifacts={
                name: (directory / digest).read_bytes() for name, digest in item.artifacts.items()
            },
            created_at=item.created_at,
            extraction_review=item.extraction_review,
            extraction_confirmed=item.extraction_confirmed,
            pending=item.pending,
            pending_decision=item.pending_decision,
            save=self.save,
        )
