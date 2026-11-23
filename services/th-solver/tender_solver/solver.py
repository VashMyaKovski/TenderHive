import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Literal, cast

from pydantic import JsonValue

from tender_solver.config import Settings
from tender_solver.errors import ErrorCode, SolverError
from tender_solver.models import (
    ArtifactDescription,
    Compliance,
    Estimate,
    Explanation,
    ExplanationStep,
    Plan,
    Requirement,
    ResourceCheck,
    Risk,
    Rule,
    SolveRequest,
    SolverResult,
    StopReason,
)
from tender_solver.ports import KnowledgeClient, ResultCache, RiskClient
from tender_solver.rules import apply_rules

_NUMBER = re.compile(r"(\d+(?:[.,]\d+)?)")


def _percent(value: str | None) -> Decimal | None:
    if value is None:
        return None
    match = _NUMBER.search(value)
    if match is None:
        return None
    try:
        return Decimal(match.group(1).replace(",", "."))
    except InvalidOperation:
        return None


class Solver:
    def __init__(
        self,
        settings: Settings,
        knowledge: KnowledgeClient,
        risk: RiskClient,
        cache: ResultCache,
        confidence_floor: float,
    ) -> None:
        self._settings = settings
        self._knowledge = knowledge
        self._risk = risk
        self._cache = cache
        self._confidence_floor = confidence_floor

    @staticmethod
    def cache_key(request: SolveRequest) -> str:
        payload = request.model_dump(mode="json", exclude={"trace_id"})
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return "tenderhive:solver:v1:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def solve(self, request: SolveRequest) -> SolverResult:
        key = self.cache_key(request)
        cached = await self._cache.get(key)
        if cached is not None and cached.kb_version == request.kb_version:
            return cached

        context = await self._knowledge.context(
            request.kb_version,
            request.regime,
            request.task_class,
            list(request.lot_ids),
            request.requirements[0].text if request.requirements else None,
        )
        if context.version != request.kb_version:
            raise SolverError(
                ErrorCode.KB_VERSION_NOT_FOUND,
                f"knowledge base version {request.kb_version} is not active",
            )
        if not request.requirements and not request.facts:
            raise SolverError(ErrorCode.NO_SOLUTION, "no requirements or facts to solve")

        facts: dict[str, JsonValue] = {}
        for fact in [*context.facts, *request.facts]:
            facts[fact.key] = fact.value
        matched, conflicts = apply_rules(context.rules, facts)

        compliance = self._compliance(request.requirements, matched)
        resources = self._resources(request.requirements, facts)
        estimate = self._estimate(facts)
        risks, model_escalation = await self._risks(request)
        stop_reasons: list[StopReason] = []

        for left, right in conflicts:
            stop_reasons.append(
                StopReason(
                    code="INCOMPATIBLE_RULES",
                    reason=f"rules {left.rule_id} and {right.rule_id} conflict",
                )
            )
        if any(item.status == "NEEDS_REVIEW" for item in compliance):
            stop_reasons.append(
                StopReason(code="UNCLEAR_REQUIREMENTS", reason="requirement needs review")
            )
        if model_escalation:
            stop_reasons.append(
                StopReason(code="UNCLEAR_REQUIREMENTS", reason="risk assessment needs review")
            )
        if any(not item.covered for item in resources):
            stop_reasons.append(StopReason(code="RESOURCE_GAP", reason="resource not covered"))

        margin = _percent(_as_text(facts.get("margin_percent")))
        if margin is not None and margin < self._settings.margin_threshold_percent:
            stop_reasons.append(
                StopReason(code="LOW_MARGIN", reason=f"margin {margin}% below threshold")
            )

        status = "INCOMPLETE" if stop_reasons else "READY"
        recommendation = self._recommendation(compliance, risks, stop_reasons)
        result = SolverResult(
            job_id=request.job_id,
            kb_version=request.kb_version,
            status=status,
            requirements=request.requirements,
            compliance=compliance,
            resources=resources,
            estimate=estimate,
            risks=risks,
            recommendation=recommendation,
            plan=self._plan(),
            explanation=self._explanation(matched, request, context.version),
            artifacts=self._artifacts(request.kb_version),
            stop_reasons=stop_reasons,
        )
        if status == "READY":
            await self._cache.set(key, result, self._settings.cache_ttl_seconds)
        return result

    def _compliance(self, requirements: list[Requirement], matched: list[Rule]) -> list[Compliance]:
        items: list[Compliance] = []
        for requirement in requirements:
            if requirement.confidence < self._confidence_floor:
                items.append(
                    Compliance(
                        requirement_id=requirement.requirement_id,
                        status="NEEDS_REVIEW",
                        rule_id=None,
                        reason="low extraction confidence",
                        source_ref=requirement.source_ref,
                    )
                )
                continue
            candidates = [rule for rule in matched if rule.scope in {requirement.kind, "global"}]
            if not candidates:
                items.append(
                    Compliance(
                        requirement_id=requirement.requirement_id,
                        status="NEEDS_REVIEW",
                        rule_id=None,
                        reason="no applicable rule",
                        source_ref=requirement.source_ref,
                    )
                )
                continue
            consequence = candidates[0].consequence
            status = (
                "MET"
                if consequence == "MET"
                else "NOT_MET"
                if consequence == "NOT_MET"
                else "NEEDS_REVIEW"
            )
            items.append(
                Compliance(
                    requirement_id=requirement.requirement_id,
                    status=cast(Literal["MET", "NOT_MET", "NEEDS_REVIEW"], status),
                    rule_id=candidates[0].rule_id,
                    reason=f"rule {candidates[0].rule_id}: {consequence}",
                    source_ref=requirement.source_ref,
                )
            )
        return items

    @staticmethod
    def _resources(
        requirements: list[Requirement], facts: dict[str, JsonValue]
    ) -> list[ResourceCheck]:
        checks: list[ResourceCheck] = []
        for requirement in requirements:
            if requirement.kind != "resource":
                continue
            position = str(requirement.requirement_id)[:8]
            covered = bool(facts.get(f"resource:{position}", False))
            checks.append(
                ResourceCheck(
                    position_id=position,
                    covered=covered,
                    shortage=None if covered else "no confirmed supply fact",
                )
            )
        return checks

    def _estimate(self, facts: dict[str, JsonValue]) -> Estimate:
        return Estimate(
            total_cost=_as_text(facts.get("total_cost")),
            offer_price=_as_text(facts.get("offer_price")),
            margin_percent=_as_text(facts.get("margin_percent")),
            threshold=str(self._settings.margin_threshold_percent),
            threshold_source="R-12",
        )

    async def _risks(self, request: SolveRequest) -> tuple[list[Risk], bool]:
        risks: list[Risk] = []
        escalation = False
        for requirement in request.requirements:
            if requirement.kind != "penalty":
                continue
            daily = _percent(requirement.value)
            if daily is None:
                escalation = True
                continue
            try:
                assessment = await self._risk.assess(
                    requirement.text,
                    request.kb_version,
                    self._settings.prompt_version,
                    daily,
                    self._settings.penalty_threshold_percent,
                )
            except Exception:
                escalation = True
                continue
            risks.append(
                Risk(
                    risk_id=f"risk-{requirement.requirement_id}",
                    category="penalty",
                    level=cast(Literal["low", "medium", "high"], assessment.level),
                    basis=assessment.summary,
                    mitigation="agree on penalty cap or escalate to lawyer",
                    source_ref=requirement.source_ref,
                )
            )
            if assessment.human_reason is not None:
                escalation = True
        return risks, escalation

    @staticmethod
    def _recommendation(
        compliance: list[Compliance], risks: list[Risk], stops: list[StopReason]
    ) -> Literal["PARTICIPATE", "NOT_PARTICIPATE", "PARTICIPATE_WITH_CONDITIONS"]:
        if any(item.status == "NOT_MET" for item in compliance) or any(
            risk.level == "high" for risk in risks
        ):
            return "NOT_PARTICIPATE"
        if stops:
            return "PARTICIPATE_WITH_CONDITIONS"
        return "PARTICIPATE"

    @staticmethod
    def _plan() -> Plan:
        return Plan(
            steps=["check requirements", "confirm resources", "approve offer package"],
            deadlines=[],
            owners=["tender_specialist", "supply", "division_head"],
        )

    @staticmethod
    def _explanation(matched: list[Rule], request: SolveRequest, version: str) -> Explanation:
        steps = [
            ExplanationStep(
                step="apply rule",
                rule_id=rule.rule_id,
                version=version,
                source_ref=request.requirements[0].source_ref if request.requirements else None,
            )
            for rule in matched
        ]
        return Explanation(
            steps=steps,
            rule_trace=[rule.rule_id for rule in matched],
            source_refs=[item.source_ref for item in request.requirements],
        )

    @staticmethod
    def _artifacts(version: str) -> list[ArtifactDescription]:
        return [
            ArtifactDescription(
                artifact_id=f"docx-{version}",
                kind="DOCX",
                version=version,
                description="checklist and draft offer",
            ),
            ArtifactDescription(
                artifact_id=f"pdf-{version}",
                kind="PDF",
                version=version,
                description="risk report with rationale",
            ),
            ArtifactDescription(
                artifact_id=f"xlsx-{version}",
                kind="XLSX",
                version=version,
                description="estimate with positions, quantities, prices and margin",
            ),
        ]


def _as_text(value: JsonValue | None) -> str:
    return "unknown" if value is None else str(value)
