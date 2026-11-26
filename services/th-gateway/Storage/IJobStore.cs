using TenderHive.Gateway.Domain;

namespace TenderHive.Gateway.Storage;

/// <summary>
/// Единая точка доступа шлюза к состоянию. Реализации: PostgreSQL (рабочий режим)
/// и хранилище в памяти (демонстрационный режим без инфраструктуры).
/// </summary>
public interface IJobStore
{
    string Name { get; }

    Task InitializeAsync(CancellationToken ct);

    Task AddUserAsync(UserRecord user, CancellationToken ct);

    Task<UserRecord?> FindUserAsync(string username, CancellationToken ct);

    Task<JobRecord?> FindJobByIdempotencyKeyAsync(string key, CancellationToken ct);

    Task<JobRecord?> GetJobAsync(Guid jobId, CancellationToken ct);

    Task<IReadOnlyList<JobRecord>> ListJobsAsync(int limit, CancellationToken ct);

    /// <summary>Вставка задания и события outbox одной транзакцией. <c>false</c> — ключ уже занят.</summary>
    Task<bool> CreateJobAsync(JobRecord job, IReadOnlyList<DocumentRecord> documents, OutboxEventRecord outbox, CancellationToken ct);

    Task TouchJobStatusAsync(Guid jobId, string status, CancellationToken ct);

    Task<IReadOnlyList<DocumentRecord>> ListDocumentsAsync(Guid jobId, CancellationToken ct);

    Task AddStepAsync(StepRecord step, CancellationToken ct);

    Task<IReadOnlyList<StepRecord>> ListStepsAsync(Guid jobId, CancellationToken ct);

    /// <summary>Запись результата, артефактов, новых карточек и событий одной транзакцией.</summary>
    Task SaveResultAsync(JobRecord job, string resultJson, IReadOnlyList<ArtifactRecord> artifacts, IReadOnlyList<DecisionRecord> decisions, IReadOnlyList<OutboxEventRecord> outbox, CancellationToken ct);

    Task<string?> GetResultJsonAsync(Guid jobId, CancellationToken ct);

    Task<IReadOnlyList<ArtifactRecord>> ListArtifactsAsync(Guid jobId, CancellationToken ct);

    Task<ArtifactRecord?> GetArtifactAsync(Guid artifactId, CancellationToken ct);

    Task<int> CountArtifactsByKindAsync(Guid jobId, string kind, CancellationToken ct);

    Task<IReadOnlyList<DecisionRecord>> ListDecisionsAsync(Guid jobId, CancellationToken ct);

    Task<IReadOnlyList<DecisionRecord>> ListOpenDecisionsAsync(int limit, CancellationToken ct);

    Task<DecisionRecord?> GetDecisionAsync(Guid decisionId, CancellationToken ct);

    /// <summary>Атомарное закрытие карточки: повторное решение по уже решённой карточке отклоняется.</summary>
    Task<DecisionApplyResult> ApplyDecisionAsync(
        Guid decisionId,
        string decision,
        string? comment,
        string actor,
        string actorRole,
        Func<ApplyContext, ApplyOutcome> outcomeFactory,
        AuditRecord audit,
        CancellationToken ct);

    Task<IReadOnlyList<DecisionRecord>> ListDueDecisionsAsync(DateTimeOffset now, int limit, CancellationToken ct);

    /// <summary>Просроченная карточка закрывается со значением «таймаут» и передаётся руководителю.</summary>
    Task<bool> ExpireDecisionAndEscalateAsync(
        Guid decisionId,
        string expiredBy,
        DateTimeOffset now,
        DecisionRecord escalated,
        OutboxEventRecord outbox,
        AuditRecord audit,
        CancellationToken ct);

    Task<IReadOnlyList<JobRecord>> ListJobsAwaitingDeliveryAsync(int limit, CancellationToken ct);

    Task MarkArtifactsDeliveredAsync(Guid jobId, DateTimeOffset at, CancellationToken ct);

    Task WriteAuditAsync(AuditRecord audit, CancellationToken ct);

    Task<IReadOnlyList<AuditRecord>> ListAuditAsync(Guid? jobId, int limit, CancellationToken ct);

    Task<KbVersionRecord?> GetActiveKbVersionAsync(CancellationToken ct);

    Task<IReadOnlyList<KbVersionRecord>> ListKbVersionsAsync(CancellationToken ct);

    Task<bool> ActivateKbVersionAsync(string version, string actor, string? comment, CancellationToken ct);

    Task<IReadOnlyList<KbActivationRecord>> ListKbActivationsAsync(int limit, CancellationToken ct);

    Task EnsureKbVersionAsync(KbVersionRecord version, CancellationToken ct);

    Task<IReadOnlyList<OutboxEventRecord>> DequeueOutboxAsync(int limit, CancellationToken ct);

    Task MarkOutboxPublishedAsync(long eventId, CancellationToken ct);

    Task MarkOutboxFailedAsync(long eventId, string error, CancellationToken ct);
}
