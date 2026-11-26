from mcp import Client

from tender_integrations.stubs import stub_server


async def test_mcp_tools_advertise_and_return_structured_fixture_data() -> None:
    async with Client(stub_server()) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools.tools} == {
            "getOrganizationProfile",
            "getDocumentStatus",
            "checkCounterparty",
        }
        profile = await client.call_tool("getOrganizationProfile", {"profile_ref": "demo"})
        assert profile.structured_content is not None
        assert profile.structured_content["profile_ref"] == "demo"
        assert profile.structured_content["permits"][0]["status"] == "valid"
        assert profile.structured_content["resources"][0]["available_quantity"] == 100
        assert profile.structured_content["prices"][0]["currency"] == "BYN"
        edo = await client.call_tool("getDocumentStatus", {"document_ref": "doc-1"})
        assert edo.structured_content == {
            "document_ref": "doc-1",
            "status": "received",
            "simulated": True,
        }
        counterparty = await client.call_tool("checkCounterparty", {"registration_number": "1234"})
        assert counterparty.structured_content == {
            "registration_number": "1234",
            "legal_name": "TenderHive Demo Supplier",
            "active": True,
            "simulated": True,
        }
