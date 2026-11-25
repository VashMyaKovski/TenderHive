namespace TenderHive.Gateway.Domain;

public sealed class UserRecord
{
    public Guid UserId { get; set; }
    public string Username { get; set; } = string.Empty;
    public string DisplayName { get; set; } = string.Empty;
    public string Role { get; set; } = string.Empty;
    public string PasswordHash { get; set; } = string.Empty;
}

public sealed class JobRecord
{
    public Guid JobId { get; set; }
    public string IdempotencyKey { get; set; } = string.Empty;
    public string Source { get; set; } = string.Empty;
    public DateTimeOffset CreatedAt { get; set; }
    public string Status { get; set; } = string.Empty;
    public string KbVersion { get; set; } = string.Empty;
    public List<string> LotIds { get; set; } = new();
    public string ProfileRef { get; set; } = string.Empty;
    public string? TelegramChatId { get; set; }
    public string? Recommendation { get; set; }
    public double? MarginPercent { get; set; }
    public DateTimeOffset? ArtifactsDeliveredAt { get; set; }
}

public sealed class DocumentRecord
{
    public Guid DocumentId { get; set; }
    public Guid JobId { get; set; }
    public string FileName { get; set; } = string.Empty;
    public string ContentType { get; set; } = string.Empty;
    public long Size { get; set; }
    public string Sha256 { get; set; } = string.Empty;
    public string StoragePath { get; set; } = string.Empty;
}

public sealed class StepRecord
{
    public Guid StepId { get; set; }
    public Guid JobId { get; set; }
    public string Code { get; set; } = string.Empty;
    public string Title { get; set; } = string.Empty;
    public string State { get; set; } = string.Empty;
    public DateTimeOffset StartedAt { get; set; }
    public DateTimeOffset? FinishedAt { get; set; }
    public string? Detail { get; set; }
}

public sealed class ArtifactRecord
{
    public Guid ArtifactId { get; set; }
    public Guid JobId { get; set; }
    public string Kind { get; set; } = string.Empty;
    public string FileName { get; set; } = string.Empty;
    public string ContentType { get; set; } = string.Empty;
    public int Version { get; set; }
    public long Size { get; set; }
    public string Sha256 { get; set; } = string.Empty;
    public string StoragePath { get; set; } = string.Empty;
    public DateTimeOffset CreatedAt { get; set; }
}

public sealed class DecisionRecord
{
    public Guid DecisionId { get; set; }
    public Guid JobId { get; set; }
    public string ReasonCode { get; set; } = string.Empty;
    public string Role { get; set; } = string.Empty;
    public DateTimeOffset CreatedAt { get; set; }
    public DateTimeOffset Deadline { get; set; }
    public double TimeoutHours { get; set; }
    public string Decision { get; set; } = string.Empty;
    public string? Comment { get; set; }
    public string? DecidedBy { get; set; }
    public DateTimeOffset? DecidedAt { get; set; }
}

public sealed class OutboxEventRecord
{
    public long EventId { get; set; }
    public string Topic { get; set; } = string.Empty;
    public string PayloadJson { get; set; } = "{}";
    public DateTimeOffset CreatedAt { get; set; }
    public DateTimeOffset? PublishedAt { get; set; }
    public int Attempts { get; set; }
    public string? LastError { get; set; }
}

public sealed class AuditRecord
{
    public long Id { get; set; }
    public DateTimeOffset OccurredAt { get; set; }
    public string Actor { get; set; } = string.Empty;
    public string Role { get; set; } = string.Empty;
    public string Action { get; set; } = string.Empty;
    public Guid? JobId { get; set; }
    public string? Details { get; set; }
}

public sealed class KbVersionRecord
{
    public string Version { get; set; } = string.Empty;
    public string Status { get; set; } = string.Empty;
    public DateTimeOffset CreatedAt { get; set; }
    public DateTimeOffset? ActivatedAt { get; set; }
    public string? ActivatedBy { get; set; }
    public int RulesCount { get; set; }
    public string? Notes { get; set; }
}

public sealed class KbActivationRecord
{
    public long Id { get; set; }
    public string Version { get; set; } = string.Empty;
    public string Action { get; set; } = string.Empty;
    public string Actor { get; set; } = string.Empty;
    public DateTimeOffset OccurredAt { get; set; }
    public string? Comment { get; set; }
}

/// <summary>Состояние задания в момент принятия решения: нужно для выбора нового статуса событиями.</summary>
public sealed record ApplyContext(JobRecord Job, DecisionRecord Decision, int RemainingOpenDecisions);

/// <summary>Результат бизнес-логики решения: новый статус задания и события для outbox.</summary>
public sealed record ApplyOutcome(string NewStatus, IReadOnlyList<OutboxEventRecord> Events);

public sealed record DecisionApplyResult(bool Applied, DecisionRecord? Decision, string? NewStatus);
