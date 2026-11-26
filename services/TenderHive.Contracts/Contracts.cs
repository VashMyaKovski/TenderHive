using System.Text.Json.Serialization;

namespace TenderHive.Contracts;

/// <summary>Источники поступления комплекта документов.</summary>
public static class JobSource
{
    public const string Mail = "mail";
    public const string Telegram = "telegram";
    public const string Web = "web";
}

/// <summary>Состояния задания. Переходы выполняет только шлюз по событию оркестратора или решению человека.</summary>
public static class JobStatus
{
    public const string Registered = "registered";
    public const string Queued = "queued";
    public const string Processing = "processing";
    public const string WaitingHuman = "waiting_human";
    public const string Completed = "completed";
    public const string Rejected = "rejected";
    public const string Failed = "failed";
}

/// <summary>Шесть оснований обязательной остановки процесса.</summary>
public static class ReasonCode
{
    public const string PenaltyAboveThreshold = "penalty_above_threshold";
    public const string MarginBelowThreshold = "margin_below_threshold";
    public const string ResourceGap = "resource_gap";
    public const string RequirementsIncomplete = "requirements_incomplete";
    public const string NoSolution = "no_solution";
    public const string FinalApproval = "final_approval";
}

/// <summary>Значения карточки согласования.</summary>
public static class DecisionValue
{
    public const string Pending = "pending";
    public const string Approve = "approve";
    public const string Reject = "reject";
    public const string Timeout = "timeout";
}

/// <summary>Роли пользователей и сервисных клиентов.</summary>
public static class AppRole
{
    public const string TenderSpecialist = "tender_specialist";
    public const string Procurement = "procurement";
    public const string Lawyer = "lawyer";
    public const string DepartmentHead = "department_head";
    public const string KbAdmin = "kb_admin";
    public const string Service = "service";
}

/// <summary>Темы Kafka, в которые шлюз пишет через таблицу outbox.</summary>
public static class EventTopic
{
    public const string JobCreated = "tender.job.created";
    public const string StageDone = "tender.stage.done";
    public const string HumanRequired = "tender.human.required";
    public const string HumanDecided = "tender.human.decided";
    public const string ArtifactsReady = "tender.artifacts.ready";
}

public sealed record LoginRequest(
    [property: JsonPropertyName("username")] string Username,
    [property: JsonPropertyName("password")] string Password);

public sealed record ServiceTokenRequest([property: JsonPropertyName("api_key")] string ApiKey);

public sealed record LoginResponse(string Token, string Username, string DisplayName, string Role, DateTimeOffset ExpiresAt);

public sealed record JobDocumentDto(Guid DocumentId, string FileName, string ContentType, long Size, string Sha256);

public sealed record JobDto(
    Guid JobId,
    string IdempotencyKey,
    string Source,
    DateTimeOffset CreatedAt,
    string Status,
    string KbVersion,
    IReadOnlyList<string> LotIds,
    IReadOnlyList<JobDocumentDto> Documents,
    string ProfileRef,
    string? TelegramChatId,
    string? Recommendation,
    double? MarginPercent);

public sealed record JobDetailDto(JobDto Job, IReadOnlyList<JobStepDto> Steps);

public sealed record JobStepDto(string Code, string Title, string State, DateTimeOffset StartedAt, DateTimeOffset? FinishedAt, string? Detail);

public sealed record StepReportDto(string Code, string Title, string State, string? Detail);

public sealed record ArtifactDto(Guid ArtifactId, string Kind, string FileName, string ContentType, int Version, long Size, string Sha256, DateTimeOffset CreatedAt);

public sealed record ArtifactLinkDto(string Url, DateTimeOffset ExpiresAt);

public sealed record HumanDecisionDto(
    Guid DecisionId,
    Guid JobId,
    string ReasonCode,
    string Role,
    DateTimeOffset CreatedAt,
    DateTimeOffset Deadline,
    double TimeoutHours,
    string Decision,
    string? Comment,
    string? DecidedBy,
    DateTimeOffset? DecidedAt);

public sealed record DecisionRequest(string Decision, string? Comment);

public sealed record DecisionResultDto(HumanDecisionDto Decision, string JobStatus);

public sealed record RequirementDto(string Code, string Text, bool Mandatory, string SourceRef);

public sealed record ComplianceDto(string Result, IReadOnlyList<RequirementDto> Satisfied, IReadOnlyList<RequirementDto> Violated);

public sealed record ResourceGapDto(string PositionCode, string Title, decimal Required, decimal Available, string? Supplier);

public sealed record ResourcesDto(double Coverage, IReadOnlyList<ResourceGapDto> Deficits);

public sealed record EstimateDto(decimal Total, double MarginPercent, double MarginThreshold);

public sealed record RiskDto(string Code, string Category, string Level, string Consequence, string Mitigation);

public sealed record RecommendationDto(string Decision, string Rationale);

public sealed record TraceEntryDto(int Step, string Rule, string Fact, string Conclusion);

public sealed record ExplanationDto(IReadOnlyList<TraceEntryDto> Trace);

public sealed record EscalationDto(string ReasonCode, string? Detail);

public sealed record ResultArtifactDto(string Kind, string FileName, string ContentType, string ContentPart);

/// <summary>Тело ответа оркестратора на точку <c>POST /api/jobs/{jobId}/result</c>.</summary>
public sealed record ResultSubmissionDto(
    string? KbVersion,
    string Status,
    IReadOnlyList<EscalationDto>? Escalations,
    IReadOnlyList<RequirementDto>? Requirements,
    ComplianceDto? Compliance,
    ResourcesDto? Resources,
    EstimateDto? Estimate,
    IReadOnlyList<RiskDto>? Risks,
    RecommendationDto? Recommendation,
    ExplanationDto? Explanation,
    IReadOnlyList<ResultArtifactDto>? Artifacts);

public sealed record JobResultDto(
    Guid JobId,
    string KbVersion,
    string Status,
    IReadOnlyList<RequirementDto> Requirements,
    ComplianceDto? Compliance,
    ResourcesDto? Resources,
    EstimateDto? Estimate,
    IReadOnlyList<RiskDto> Risks,
    RecommendationDto? Recommendation,
    ExplanationDto? Explanation);

public sealed record KbVersionDto(string Version, string Status, DateTimeOffset CreatedAt, DateTimeOffset? ActivatedAt, string? ActivatedBy, int RulesCount, string? Notes);

public sealed record KbActivationDto(string Version, string Action, string Actor, DateTimeOffset OccurredAt, string? Comment);

public sealed record KbActivateRequest(string? Comment);

public sealed record AuditEntryDto(DateTimeOffset OccurredAt, string Actor, string Role, string Action, string? Details);

public sealed record HealthDto(string Status, string Store, string Messaging, string Telegram);
