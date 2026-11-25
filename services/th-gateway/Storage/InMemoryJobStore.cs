using TenderHive.Contracts;
using TenderHive.Gateway.Domain;

namespace TenderHive.Gateway.Storage;

/// <summary>
/// Демонстрационное хранилище в памяти: шлюз и дашборд поднимаются без PostgreSQL.
/// Порядок и атомарность операций повторяют реализацию на PostgreSQL.
/// </summary>
public sealed class InMemoryJobStore : IJobStore
{
    private readonly object _gate = new();
    private readonly List<UserRecord> _users = new();
    private readonly List<JobRecord> _jobs = new();
    private readonly List<DocumentRecord> _documents = new();
    private readonly List<StepRecord> _steps = new();
    private readonly Dictionary<Guid, string> _results = new();
    private readonly List<ArtifactRecord> _artifacts = new();
    private readonly List<DecisionRecord> _decisions = new();
    private readonly List<OutboxEventRecord> _outbox = new();
    private readonly List<AuditRecord> _audit = new();
    private readonly List<KbVersionRecord> _kbVersions = new();
    private readonly List<KbActivationRecord> _kbActivations = new();
    private long _eventId;
    private long _auditId;
    private long _activationId;

    public string Name => "in-memory";

    public Task InitializeAsync(CancellationToken ct) => Task.CompletedTask;

    public Task AddUserAsync(UserRecord user, CancellationToken ct)
    {
        lock (_gate)
        {
            _users.Add(user);
        }

        return Task.CompletedTask;
    }

    public Task<UserRecord?> FindUserAsync(string username, CancellationToken ct)
    {
        lock (_gate)
        {
            return Task.FromResult(_users.FirstOrDefault(u =>
                string.Equals(u.Username, username, StringComparison.OrdinalIgnoreCase)));
        }
    }

    public Task<JobRecord?> FindJobByIdempotencyKeyAsync(string key, CancellationToken ct)
    {
        lock (_gate)
        {
            return Task.FromResult(_jobs.FirstOrDefault(j => j.IdempotencyKey == key));
        }
    }

    public Task<JobRecord?> GetJobAsync(Guid jobId, CancellationToken ct)
    {
        lock (_gate)
        {
            return Task.FromResult(_jobs.FirstOrDefault(j => j.JobId == jobId));
        }
    }

    public Task<IReadOnlyList<JobRecord>> ListJobsAsync(int limit, CancellationToken ct)
    {
        lock (_gate)
        {
            IReadOnlyList<JobRecord> jobs = _jobs
                .OrderByDescending(j => j.CreatedAt)
                .Take(Math.Max(1, limit))
                .ToList();
            return Task.FromResult(jobs);
        }
    }

    public Task<bool> CreateJobAsync(JobRecord job, IReadOnlyList<DocumentRecord> documents, OutboxEventRecord outbox, CancellationToken ct)
    {
        lock (_gate)
        {
            if (_jobs.Any(j => j.IdempotencyKey == job.IdempotencyKey))
            {
                return Task.FromResult(false);
            }

            _jobs.Add(job);
            _documents.AddRange(documents);
            Append(outbox);
            return Task.FromResult(true);
        }
    }

    public Task TouchJobStatusAsync(Guid jobId, string status, CancellationToken ct)
    {
        lock (_gate)
        {
            var job = _jobs.FirstOrDefault(j => j.JobId == jobId);
            if (job is not null)
            {
                job.Status = status;
            }
        }

        return Task.CompletedTask;
    }

    public Task<IReadOnlyList<DocumentRecord>> ListDocumentsAsync(Guid jobId, CancellationToken ct)
    {
        lock (_gate)
        {
            IReadOnlyList<DocumentRecord> documents = _documents.Where(d => d.JobId == jobId).ToList();
            return Task.FromResult(documents);
        }
    }

    public Task AddStepAsync(StepRecord step, CancellationToken ct)
    {
        lock (_gate)
        {
            _steps.Add(step);
        }

        return Task.CompletedTask;
    }

    public Task<IReadOnlyList<StepRecord>> ListStepsAsync(Guid jobId, CancellationToken ct)
    {
        lock (_gate)
        {
            IReadOnlyList<StepRecord> steps = _steps
                .Where(s => s.JobId == jobId)
                .OrderBy(s => s.StartedAt)
                .ToList();
            return Task.FromResult(steps);
        }
    }

    public Task SaveResultAsync(JobRecord job, string resultJson, IReadOnlyList<ArtifactRecord> artifacts, IReadOnlyList<DecisionRecord> decisions, IReadOnlyList<OutboxEventRecord> outbox, CancellationToken ct)
    {
        lock (_gate)
        {
            var stored = _jobs.FirstOrDefault(j => j.JobId == job.JobId);
            if (stored is not null)
            {
                stored.Status = job.Status;
                stored.KbVersion = job.KbVersion;
                stored.Recommendation = job.Recommendation;
                stored.MarginPercent = job.MarginPercent;
            }

            _results[job.JobId] = resultJson;
            _artifacts.AddRange(artifacts);

            foreach (var decision in decisions)
            {
                if (_decisions.All(d => d.DecisionId != decision.DecisionId))
                {
                    _decisions.Add(decision);
                }
            }

            foreach (var item in outbox)
            {
                Append(item);
            }
        }

        return Task.CompletedTask;
    }

    public Task<string?> GetResultJsonAsync(Guid jobId, CancellationToken ct)
    {
        lock (_gate)
        {
            return Task.FromResult(_results.TryGetValue(jobId, out var json) ? json : null);
        }
    }

    public Task<IReadOnlyList<ArtifactRecord>> ListArtifactsAsync(Guid jobId, CancellationToken ct)
    {
        lock (_gate)
        {
            IReadOnlyList<ArtifactRecord> artifacts = _artifacts
                .Where(a => a.JobId == jobId)
                .OrderBy(a => a.Kind, StringComparer.Ordinal)
                .ThenBy(a => a.Version)
                .ToList();
            return Task.FromResult(artifacts);
        }
    }

    public Task<ArtifactRecord?> GetArtifactAsync(Guid artifactId, CancellationToken ct)
    {
        lock (_gate)
        {
            return Task.FromResult(_artifacts.FirstOrDefault(a => a.ArtifactId == artifactId));
        }
    }

    public Task<int> CountArtifactsByKindAsync(Guid jobId, string kind, CancellationToken ct)
    {
        lock (_gate)
        {
            return Task.FromResult(_artifacts.Count(a => a.JobId == jobId && a.Kind == kind));
        }
    }

    public Task<IReadOnlyList<DecisionRecord>> ListDecisionsAsync(Guid jobId, CancellationToken ct)
    {
        lock (_gate)
        {
            IReadOnlyList<DecisionRecord> decisions = _decisions
                .Where(d => d.JobId == jobId)
                .OrderBy(d => d.CreatedAt)
                .ToList();
            return Task.FromResult(decisions);
        }
    }

    public Task<IReadOnlyList<DecisionRecord>> ListOpenDecisionsAsync(int limit, CancellationToken ct)
    {
        lock (_gate)
        {
            IReadOnlyList<DecisionRecord> decisions = _decisions
                .Where(d => d.Decision == DecisionValue.Pending)
                .OrderBy(d => d.Deadline)
                .Take(Math.Max(1, limit))
                .ToList();
            return Task.FromResult(decisions);
        }
    }

    public Task<DecisionRecord?> GetDecisionAsync(Guid decisionId, CancellationToken ct)
    {
        lock (_gate)
        {
            return Task.FromResult(_decisions.FirstOrDefault(d => d.DecisionId == decisionId));
        }
    }

    public Task<DecisionApplyResult> ApplyDecisionAsync(
        Guid decisionId,
        string decision,
        string? comment,
        string actor,
        string actorRole,
        Func<ApplyContext, ApplyOutcome> outcomeFactory,
        AuditRecord audit,
        CancellationToken ct)
    {
        lock (_gate)
        {
            var card = _decisions.FirstOrDefault(d => d.DecisionId == decisionId);
            if (card is null || card.Decision != DecisionValue.Pending)
            {
                return Task.FromResult(new DecisionApplyResult(false, card, null));
            }

            var job = _jobs.FirstOrDefault(j => j.JobId == card.JobId);
            if (job is null)
            {
                return Task.FromResult(new DecisionApplyResult(false, card, null));
            }

            card.Decision = decision;
            card.Comment = comment;
            card.DecidedBy = actor;
            card.DecidedAt = DateTimeOffset.UtcNow;

            var remaining = _decisions.Count(d => d.JobId == job.JobId && d.Decision == DecisionValue.Pending);
            var outcome = outcomeFactory(new ApplyContext(job, card, remaining));

            job.Status = outcome.NewStatus;
            foreach (var item in outcome.Events)
            {
                Append(item);
            }

            audit.JobId ??= job.JobId;
            audit.OccurredAt = audit.OccurredAt == default ? DateTimeOffset.UtcNow : audit.OccurredAt;
            audit.Id = ++_auditId;
            _audit.Add(audit);

            return Task.FromResult(new DecisionApplyResult(true, card, outcome.NewStatus));
        }
    }

    public Task<IReadOnlyList<DecisionRecord>> ListDueDecisionsAsync(DateTimeOffset now, int limit, CancellationToken ct)
    {
        lock (_gate)
        {
            IReadOnlyList<DecisionRecord> decisions = _decisions
                .Where(d => d.Decision == DecisionValue.Pending && d.Deadline <= now)
                .OrderBy(d => d.Deadline)
                .Take(Math.Max(1, limit))
                .ToList();
            return Task.FromResult(decisions);
        }
    }

    public Task<bool> ExpireDecisionAndEscalateAsync(
        Guid decisionId,
        string expiredBy,
        DateTimeOffset now,
        DecisionRecord escalated,
        OutboxEventRecord outbox,
        AuditRecord audit,
        CancellationToken ct)
    {
        lock (_gate)
        {
            var card = _decisions.FirstOrDefault(d => d.DecisionId == decisionId);
            if (card is null || card.Decision != DecisionValue.Pending)
            {
                return Task.FromResult(false);
            }

            card.Decision = DecisionValue.Timeout;
            card.DecidedBy = expiredBy;
            card.DecidedAt = now;
            card.Comment = $"Срок ожидания истёк {now:yyyy-MM-dd HH:mm} UTC. Карточка передана руководителю подразделения.";

            if (_decisions.All(d => d.DecisionId != escalated.DecisionId))
            {
                _decisions.Add(escalated);
            }

            Append(outbox);
            audit.OccurredAt = audit.OccurredAt == default ? now : audit.OccurredAt;
            audit.Id = ++_auditId;
            _audit.Add(audit);

            return Task.FromResult(true);
        }
    }

    public Task<IReadOnlyList<JobRecord>> ListJobsAwaitingDeliveryAsync(int limit, CancellationToken ct)
    {
        lock (_gate)
        {
            IReadOnlyList<JobRecord> jobs = _jobs
                .Where(j => j.Status == JobStatus.Completed
                            && !string.IsNullOrEmpty(j.TelegramChatId)
                            && j.ArtifactsDeliveredAt is null)
                .OrderBy(j => j.CreatedAt)
                .Take(Math.Max(1, limit))
                .ToList();
            return Task.FromResult(jobs);
        }
    }

    public Task MarkArtifactsDeliveredAsync(Guid jobId, DateTimeOffset at, CancellationToken ct)
    {
        lock (_gate)
        {
            var job = _jobs.FirstOrDefault(j => j.JobId == jobId);
            if (job is not null)
            {
                job.ArtifactsDeliveredAt = at;
            }
        }

        return Task.CompletedTask;
    }

    public Task WriteAuditAsync(AuditRecord audit, CancellationToken ct)
    {
        lock (_gate)
        {
            audit.OccurredAt = audit.OccurredAt == default ? DateTimeOffset.UtcNow : audit.OccurredAt;
            audit.Id = ++_auditId;
            _audit.Add(audit);
        }

        return Task.CompletedTask;
    }

    public Task<IReadOnlyList<AuditRecord>> ListAuditAsync(Guid? jobId, int limit, CancellationToken ct)
    {
        lock (_gate)
        {
            IReadOnlyList<AuditRecord> entries = _audit
                .Where(a => jobId is null || a.JobId == jobId)
                .OrderByDescending(a => a.OccurredAt)
                .Take(Math.Max(1, limit))
                .ToList();
            return Task.FromResult(entries);
        }
    }

    public Task<KbVersionRecord?> GetActiveKbVersionAsync(CancellationToken ct)
    {
        lock (_gate)
        {
            return Task.FromResult(_kbVersions.FirstOrDefault(v => v.Status == "active"));
        }
    }

    public Task<IReadOnlyList<KbVersionRecord>> ListKbVersionsAsync(CancellationToken ct)
    {
        lock (_gate)
        {
            IReadOnlyList<KbVersionRecord> versions = _kbVersions
                .OrderByDescending(v => v.CreatedAt)
                .ToList();
            return Task.FromResult(versions);
        }
    }

    public Task<bool> ActivateKbVersionAsync(string version, string actor, string? comment, CancellationToken ct)
    {
        lock (_gate)
        {
            var target = _kbVersions.FirstOrDefault(v => v.Version == version);
            if (target is null)
            {
                return Task.FromResult(false);
            }

            var active = _kbVersions.FirstOrDefault(v => v.Status == "active");
            var action = active is not null && target.CreatedAt < active.CreatedAt ? "rollback" : "activate";

            foreach (var item in _kbVersions.Where(v => v.Status == "active"))
            {
                item.Status = "archived";
            }

            target.Status = "active";
            target.ActivatedAt = DateTimeOffset.UtcNow;
            target.ActivatedBy = actor;

            _kbActivations.Add(new KbActivationRecord
            {
                Id = ++_activationId,
                Version = version,
                Action = action,
                Actor = actor,
                OccurredAt = DateTimeOffset.UtcNow,
                Comment = comment
            });

            return Task.FromResult(true);
        }
    }

    public Task<IReadOnlyList<KbActivationRecord>> ListKbActivationsAsync(int limit, CancellationToken ct)
    {
        lock (_gate)
        {
            IReadOnlyList<KbActivationRecord> activations = _kbActivations
                .OrderByDescending(a => a.OccurredAt)
                .Take(Math.Max(1, limit))
                .ToList();
            return Task.FromResult(activations);
        }
    }

    public Task EnsureKbVersionAsync(KbVersionRecord version, CancellationToken ct)
    {
        lock (_gate)
        {
            if (_kbVersions.All(v => v.Version != version.Version))
            {
                _kbVersions.Add(version);
            }
        }

        return Task.CompletedTask;
    }

    public Task<IReadOnlyList<OutboxEventRecord>> DequeueOutboxAsync(int limit, CancellationToken ct)
    {
        lock (_gate)
        {
            IReadOnlyList<OutboxEventRecord> events = _outbox
                .Where(e => e.PublishedAt is null)
                .OrderBy(e => e.EventId)
                .Take(Math.Max(1, limit))
                .ToList();
            return Task.FromResult(events);
        }
    }

    public Task MarkOutboxPublishedAsync(long eventId, CancellationToken ct)
    {
        lock (_gate)
        {
            var item = _outbox.FirstOrDefault(e => e.EventId == eventId);
            if (item is not null)
            {
                item.PublishedAt = DateTimeOffset.UtcNow;
                item.Attempts++;
                item.LastError = null;
            }
        }

        return Task.CompletedTask;
    }

    public Task MarkOutboxFailedAsync(long eventId, string error, CancellationToken ct)
    {
        lock (_gate)
        {
            var item = _outbox.FirstOrDefault(e => e.EventId == eventId);
            if (item is not null)
            {
                item.Attempts++;
                item.LastError = error;
            }
        }

        return Task.CompletedTask;
    }

    private void Append(OutboxEventRecord item)
    {
        item.EventId = ++_eventId;
        item.CreatedAt = item.CreatedAt == default ? DateTimeOffset.UtcNow : item.CreatedAt;
        _outbox.Add(item);
    }
}
