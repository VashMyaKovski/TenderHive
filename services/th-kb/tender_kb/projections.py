import asyncio
from typing import LiteralString
from uuid import UUID, uuid5

import httpx
from neo4j import AsyncDriver, EagerResult, RoutingControl
from neo4j.exceptions import DriverError, Neo4jError
from pydantic import BaseModel, TypeAdapter, ValidationError

from tender_kb.dependencies import Embeddings
from tender_kb.domain import (
    ContextSelection,
    CorpusFragment,
    DependencyUnavailable,
    GraphEdge,
    GraphNode,
    GraphSlice,
)

_FRAGMENTS = TypeAdapter(list[CorpusFragment])


class Neo4jGraph:
    def __init__(self, driver: AsyncDriver, database: str, timeout: int) -> None:
        self._driver = driver
        self._database = database
        self._timeout = timeout

    async def migrate(self) -> None:
        await self._query(
            """
            CREATE CONSTRAINT th_kb_snapshot_node IF NOT EXISTS
            FOR (node:THKBNode) REQUIRE (node.projection, node.node_id) IS UNIQUE
            """
        )

    async def ready(self) -> None:
        await self._query("RETURN 1")

    async def put(self, projection: UUID, graph: GraphSlice) -> None:
        await self._query(
            """
            UNWIND $nodes AS node
            CREATE (:THKBNode {
                projection: $projection, node_id: node.id, kind: node.kind,
                lot_id: node.lot, payload: node.payload
            })
            """,
            projection=str(projection),
            nodes=[
                {
                    "id": node.node_id,
                    "kind": node.kind,
                    "lot": node.lot_id,
                    "payload": node.model_dump_json(),
                }
                for node in graph.nodes
            ],
        )
        await self._query(
            """
            UNWIND $edges AS edge
            MATCH (source:THKBNode {projection: $projection, node_id: edge.source})
            MATCH (target:THKBNode {projection: $projection, node_id: edge.target})
            CREATE (source)-[:THKB_LINK {payload: edge.payload}]->(target)
            """,
            projection=str(projection),
            edges=[
                {"source": edge.source, "target": edge.target, "payload": edge.model_dump_json()}
                for edge in graph.edges
            ],
        )

    async def delete(self, projection: UUID) -> None:
        await self._query(
            "MATCH (node:THKBNode {projection: $projection}) DETACH DELETE node",
            projection=str(projection),
        )

    async def slice(self, projection: UUID, lot_ids: tuple[str, ...]) -> GraphSlice:
        result = await self._query(
            """
            MATCH (anchor:THKBNode {projection: $projection})
            WHERE size($lot_ids) = 0 OR anchor.lot_id IN $lot_ids
                OR (anchor.kind = 'lot' AND anchor.node_id IN $lot_ids)
            OPTIONAL MATCH (anchor)-[:THKB_LINK]-(neighbor:THKBNode {projection: $projection})
            WHERE size($lot_ids) > 0
            WITH collect(DISTINCT anchor) + collect(DISTINCT neighbor) AS nearby
            UNWIND nearby AS node
            WITH collect(DISTINCT node) AS nodes
            UNWIND nodes AS node
            OPTIONAL MATCH (node)-[edge:THKB_LINK]->(target:THKBNode {projection: $projection})
            WHERE target IN nodes
            RETURN collect(DISTINCT node.payload) AS nodes, collect(DISTINCT edge.payload) AS edges
            """,
            read=True,
            projection=str(projection),
            lot_ids=list(lot_ids),
        )
        try:
            row = result.records[0]
            nodes = [GraphNode.model_validate_json(node) for node in row["nodes"]]
            edges = [GraphEdge.model_validate_json(edge) for edge in row["edges"]]
        except (IndexError, ValidationError, TypeError, KeyError) as error:
            raise DependencyUnavailable("Neo4j returned an invalid graph slice.") from error
        return GraphSlice(
            nodes=sorted(nodes, key=lambda node: node.node_id),
            edges=sorted(edges, key=lambda edge: (edge.source, edge.target, edge.relation)),
        )

    async def _query(
        self, query: LiteralString, *, read: bool = False, **parameters: object
    ) -> EagerResult:
        try:
            async with asyncio.timeout(self._timeout):
                return await self._driver.execute_query(
                    query,
                    parameters_=parameters,
                    database_=self._database,
                    routing_=RoutingControl.READ if read else RoutingControl.WRITE,
                )
        except (DriverError, Neo4jError, TimeoutError) as error:
            raise DependencyUnavailable("Neo4j is unavailable.") from error


class _VectorsConfig(BaseModel):
    size: int
    distance: str


class _CollectionParams(BaseModel):
    vectors: _VectorsConfig


class _CollectionConfig(BaseModel):
    params: _CollectionParams


class _CollectionInfo(BaseModel):
    config: _CollectionConfig


class _CollectionResponse(BaseModel):
    result: _CollectionInfo


class _Point(BaseModel):
    payload: CorpusFragment


class _Points(BaseModel):
    points: list[_Point]


class _PointsResponse(BaseModel):
    result: _Points


class QdrantCorpus:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        collection: str,
        api_key: str | None,
        embeddings: Embeddings,
        vector_size: int,
    ) -> None:
        self._client = client
        self._url = f"{base_url.rstrip('/')}/collections/{collection}"
        self._headers = {"api-key": api_key} if api_key is not None else {}
        self._embeddings = embeddings
        self._vector_size = vector_size

    async def migrate(self) -> None:
        response = await self._request("GET", "", allow_missing=True)
        if response.status_code != 404:
            self._validate_collection(response)
            return
        try:
            await self._request(
                "PUT", "", body={"vectors": {"size": self._vector_size, "distance": "Cosine"}}
            )
        except DependencyUnavailable:
            # Another migration may have created the collection while this request was in flight.
            await self.ready()
            return
        await self.ready()

    async def ready(self) -> None:
        self._validate_collection(await self._request("GET", ""))

    def _validate_collection(self, response: httpx.Response) -> None:
        try:
            config = _CollectionResponse.model_validate_json(response.content)
        except ValidationError as error:
            raise DependencyUnavailable(
                "Qdrant returned invalid collection configuration."
            ) from error
        vectors = config.result.config.params.vectors
        if vectors.size != self._vector_size or vectors.distance.lower() != "cosine":
            raise DependencyUnavailable("Qdrant collection and the embedding settings differ.")

    async def put(self, projection: UUID, fragments: list[CorpusFragment]) -> None:
        if not fragments:
            return
        vectors = await self._embeddings.encode([fragment.text for fragment in fragments])
        points = [
            {
                "id": str(uuid5(projection, fragment.fragment_id)),
                "vector": vector,
                "payload": {
                    **fragment.model_dump(mode="json"),
                    "projection": str(projection),
                },
            }
            for fragment, vector in zip(fragments, vectors, strict=True)
        ]
        await self._request("PUT", "/points?wait=true", body={"points": points})

    async def delete(self, projection: UUID) -> None:
        await self._request(
            "POST", "/points/delete?wait=true", body={"filter": _point_filter(projection, ())}
        )

    async def search(
        self, projection: UUID, selection: ContextSelection, limit: int
    ) -> list[CorpusFragment]:
        body = {
            "filter": _point_filter(projection, selection.lot_ids),
            "limit": limit,
            "with_payload": ["fragment_id", "text", "source", "date", "lot_ids"],
        }
        try:
            if selection.query is None:
                response = await self._request("POST", "/points/scroll", body=body)
                result = _PointsResponse.model_validate_json(response.content)
            else:
                (query,) = await self._embeddings.encode([selection.query])
                response = await self._request(
                    "POST", "/points/query", body={**body, "query": query}
                )
                result = _PointsResponse.model_validate_json(response.content)
        except ValidationError as error:
            raise DependencyUnavailable("Qdrant returned invalid corpus fragments.") from error
        return [point.payload for point in result.result.points]

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        allow_missing: bool = False,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method, f"{self._url}{path}", headers=self._headers, json=body
            )
            return (
                response
                if allow_missing and response.status_code == 404
                else response.raise_for_status()
            )
        except httpx.HTTPError as error:
            raise DependencyUnavailable("Qdrant is unavailable.") from error


def _point_filter(projection: UUID, lot_ids: tuple[str, ...]) -> dict[str, object]:
    filter_: dict[str, object] = {
        "must": [{"key": "projection", "match": {"value": str(projection)}}]
    }
    if lot_ids:
        filter_["should"] = [
            {"is_empty": {"key": "lot_ids"}},
            {"key": "lot_ids", "match": {"any": list(lot_ids)}},
        ]
    return filter_
