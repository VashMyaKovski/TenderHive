using TenderHive.Contracts;
using TenderHive.Gateway.Services;

namespace TenderHive.Gateway.Tests;

public sealed class HitlPolicyTests
{
    [Theory]
    [InlineData(ReasonCode.PenaltyAboveThreshold, AppRole.Lawyer)]
    [InlineData(ReasonCode.MarginBelowThreshold, AppRole.DepartmentHead)]
    [InlineData(ReasonCode.ResourceGap, AppRole.Procurement)]
    [InlineData(ReasonCode.RequirementsIncomplete, AppRole.TenderSpecialist)]
    [InlineData(ReasonCode.NoSolution, AppRole.DepartmentHead)]
    [InlineData(ReasonCode.FinalApproval, AppRole.DepartmentHead)]
    public void Six_stop_reasons_have_owner_roles(string reasonCode, string role)
    {
        Assert.True(HitlPolicy.IsKnown(reasonCode));
        Assert.Equal(role, HitlPolicy.RoleFor(reasonCode));
    }

    [Fact]
    public void Unknown_reason_is_not_accepted()
    {
        Assert.False(HitlPolicy.IsKnown("something_else"));
    }
}
