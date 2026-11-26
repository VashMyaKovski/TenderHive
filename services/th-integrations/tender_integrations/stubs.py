from typing import Annotated, Literal

from mcp.server import MCPServer
from pydantic import BaseModel, StringConstraints

type Reference = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]


class Permit(BaseModel):
    permit_id: str
    kind: str
    status: Literal["valid"]


class Resource(BaseModel):
    item_code: str
    available_quantity: int
    unit: str


class Price(BaseModel):
    item_code: str
    unit_price: str
    currency: Literal["BYN"]


class OrganizationProfile(BaseModel):
    profile_ref: str
    permits: list[Permit]
    resources: list[Resource]
    prices: list[Price]
    simulated: Literal[True] = True


class DocumentStatus(BaseModel):
    document_ref: str
    status: Literal["received"]
    simulated: Literal[True] = True


class Counterparty(BaseModel):
    registration_number: str
    legal_name: str
    active: bool
    simulated: Literal[True] = True


def stub_server() -> MCPServer[None]:
    server: MCPServer[None] = MCPServer(
        "TenderHive external-system stubs",
        instructions="Read-only synthetic records for 1C, electronic document exchange and registry.",
    )

    @server.tool(name="getOrganizationProfile")
    def organization_profile(profile_ref: Reference) -> OrganizationProfile:
        """Return sample permits, resources and prices for an organization reference."""
        return OrganizationProfile(
            profile_ref=profile_ref,
            permits=[
                Permit(permit_id="demo-permit-001", kind="SUPPLIER_CERTIFICATE", status="valid")
            ],
            resources=[Resource(item_code="DEMO-01", available_quantity=100, unit="pcs")],
            prices=[Price(item_code="DEMO-01", unit_price="25.00", currency="BYN")],
        )

    @server.tool(name="getDocumentStatus")
    def document_status(document_ref: Reference) -> DocumentStatus:
        """Return the sample electronic exchange status for a document reference."""
        return DocumentStatus(document_ref=document_ref, status="received")

    @server.tool(name="checkCounterparty")
    def check_counterparty(registration_number: Reference) -> Counterparty:
        """Return sample registry attributes for a counterparty identifier."""
        return Counterparty(
            registration_number=registration_number,
            legal_name="TenderHive Demo Supplier",
            active=True,
        )

    return server
