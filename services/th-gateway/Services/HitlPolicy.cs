using TenderHive.Contracts;

namespace TenderHive.Gateway.Services;

/// <summary>Правила обязательной остановки: основание определяет роль владельца карточки.</summary>
public static class HitlPolicy
{
    public static bool IsKnown(string reasonCode) => reasonCode switch
    {
        ReasonCode.PenaltyAboveThreshold => true,
        ReasonCode.MarginBelowThreshold => true,
        ReasonCode.ResourceGap => true,
        ReasonCode.RequirementsIncomplete => true,
        ReasonCode.NoSolution => true,
        ReasonCode.FinalApproval => true,
        _ => false
    };

    public static string RoleFor(string reasonCode) => reasonCode switch
    {
        ReasonCode.PenaltyAboveThreshold => AppRole.Lawyer,
        ReasonCode.ResourceGap => AppRole.Procurement,
        ReasonCode.RequirementsIncomplete => AppRole.TenderSpecialist,
        ReasonCode.MarginBelowThreshold => AppRole.DepartmentHead,
        ReasonCode.NoSolution => AppRole.DepartmentHead,
        ReasonCode.FinalApproval => AppRole.DepartmentHead,
        _ => AppRole.DepartmentHead
    };
}

/// <summary>Ошибка прикладного уровня: статус ответа и понятная причина вместо частичного результата.</summary>
public sealed class ApiException : Exception
{
    public ApiException(int statusCode, string message) : base(message)
    {
        StatusCode = statusCode;
    }

    public int StatusCode { get; }
}
