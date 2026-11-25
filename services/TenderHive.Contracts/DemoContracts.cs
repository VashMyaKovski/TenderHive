using System.Text.Json;

namespace TenderHive.Contracts;

public sealed record DemoAttachment(string Name, string ContentType, string ContentBase64);

public sealed record DemoFacts(
    decimal Quantity,
    int OfferValidityWorkdays,
    decimal UnitCost,
    decimal OfferPrice,
    decimal AvailableQuantity,
    decimal? PenaltyDailyPercent = null,
    DateOnly? DeliveryDeadline = null);

public sealed record DemoRunInput(
    string Title, IReadOnlyList<DemoAttachment> Documents, DemoFacts Facts, string Context);

public sealed record DemoSupplyConfirmation(decimal Quantity, DateOnly DeliveryDate, decimal AdditionalCost);

public sealed record DemoDecisionInput(
    Guid CardId, string Decision, string? Comment = null, DemoSupplyConfirmation? SupplyConfirmation = null);

public sealed record DemoDocumentDto(string Name, long Size);

public sealed record DemoMessageDto(
    Guid Id, string Agent, string To, string Status, string Text, DateTimeOffset At, JsonElement? Details);

public sealed record DemoRecommendationDto(
    string Value, string Status, IReadOnlyList<string> Reasons, IReadOnlyList<string> Conditions);

public sealed record DemoSupplyRequirementDto(
    string Shortage, DateOnly? DeliveryDeadline);

public sealed record DemoCardDto(
    Guid Id, string Reason, string Role, string Question, bool CanDecide = false,
    DemoSupplyRequirementDto? Supply = null, bool RequiresComment = false);

public sealed record DemoRiskDecisionDto(string Reason, string Role, string Decision, string Comment);

public sealed record DemoArtifactDto(string Name, string Url);

public sealed record DemoRunDto(
    Guid Id,
    string Title,
    string Status,
    string Phase,
    Guid? TraceId,
    IReadOnlyList<DemoDocumentDto> Documents,
    IReadOnlyList<DemoMessageDto> Messages,
    DemoRecommendationDto? Recommendation,
    DemoCardDto? Card,
    IReadOnlyList<DemoArtifactDto> Artifacts,
    string? Error,
    IReadOnlyList<DemoRiskDecisionDto>? RiskDecisions = null);
