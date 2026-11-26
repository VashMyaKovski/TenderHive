using TenderHive.Contracts;
using TenderHive.Gateway.Domain;
using TenderHive.Gateway.Storage;

namespace TenderHive.Gateway.Tests;

public sealed class DecisionImmutabilityTests
{
    private static async Task<(InMemoryJobStore Store, JobRecord Job, DecisionRecord Card)> SeedAsync()
    {
        var store = new InMemoryJobStore();
        await store.InitializeAsync(CancellationToken.None);

        var now = DateTimeOffset.UtcNow;
        var job = new JobRecord
        {
            JobId = Guid.NewGuid(),
            IdempotencyKey = "key-" + Guid.NewGuid().ToString("N"),
            Source = JobSource.Web,
            CreatedAt = now,
            Status = JobStatus.WaitingHuman,
            KbVersion = "kb-2026.11.1",
            ProfileRef = "company-default"
        };

        var created = await store.CreateJobAsync(
            job,
            Array.Empty<DocumentRecord>(),
            new OutboxEventRecord { Topic = EventTopic.JobCreated, PayloadJson = "{}", CreatedAt = now },
            CancellationToken.None);
        Assert.True(created);

        var card = new DecisionRecord
        {
            DecisionId = Guid.NewGuid(),
            JobId = job.JobId,
            ReasonCode = ReasonCode.PenaltyAboveThreshold,
            Role = AppRole.Lawyer,
            CreatedAt = now,
            Deadline = now.AddHours(24),
            TimeoutHours = 24,
            Decision = DecisionValue.Pending
        };

        await store.SaveResultAsync(
            job,
            "{}",
            Array.Empty<ArtifactRecord>(),
            new[] { card },
            Array.Empty<OutboxEventRecord>(),
            CancellationToken.None);

        return (store, job, card);
    }

    private static AuditRecord Audit(string action) => new()
    {
        Actor = "lawyer",
        Role = AppRole.Lawyer,
        Action = action
    };

    [Fact]
    public async Task Second_decision_on_the_same_card_is_rejected()
    {
        var (store, job, card) = await SeedAsync();

        var first = await store.ApplyDecisionAsync(
            card.DecisionId,
            DecisionValue.Approve,
            "штраф принят",
            "lawyer",
            AppRole.Lawyer,
            _ => new ApplyOutcome(JobStatus.Processing, Array.Empty<OutboxEventRecord>()),
            Audit("decision.approve"),
            CancellationToken.None);

        var second = await store.ApplyDecisionAsync(
            card.DecisionId,
            DecisionValue.Reject,
            "повторное решение",
            "lawyer",
            AppRole.Lawyer,
            _ => new ApplyOutcome(JobStatus.Rejected, Array.Empty<OutboxEventRecord>()),
            Audit("decision.reject"),
            CancellationToken.None);

        Assert.True(first.Applied);
        Assert.False(second.Applied);

        var stored = await store.GetDecisionAsync(card.DecisionId, CancellationToken.None);
        Assert.NotNull(stored);
        Assert.Equal(DecisionValue.Approve, stored!.Decision);
        Assert.Equal("штраф принят", stored.Comment);
        Assert.Equal(JobStatus.Processing, (await store.GetJobAsync(job.JobId, CancellationToken.None))!.Status);
    }

    [Fact]
    public async Task Expired_card_is_closed_as_timeout_and_escalated_without_automatic_approval()
    {
        var (store, job, card) = await SeedAsync();
        var now = DateTimeOffset.UtcNow;

        var escalated = new DecisionRecord
        {
            DecisionId = Guid.NewGuid(),
            JobId = job.JobId,
            ReasonCode = card.ReasonCode,
            Role = AppRole.DepartmentHead,
            CreatedAt = now,
            Deadline = now.AddHours(24),
            TimeoutHours = 24,
            Decision = DecisionValue.Pending
        };

        var applied = await store.ExpireDecisionAndEscalateAsync(
            card.DecisionId,
            "system",
            now,
            escalated,
            new OutboxEventRecord { Topic = EventTopic.HumanRequired, PayloadJson = "{}", CreatedAt = now },
            Audit("decision.timeout"),
            CancellationToken.None);

        Assert.True(applied);

        var expired = await store.GetDecisionAsync(card.DecisionId, CancellationToken.None);
        Assert.Equal(DecisionValue.Timeout, expired!.Decision);
        Assert.Equal("system", expired.DecidedBy);

        var open = await store.ListOpenDecisionsAsync(10, CancellationToken.None);
        var pending = Assert.Single(open);
        Assert.Equal(AppRole.DepartmentHead, pending.Role);

        var all = await store.ListDecisionsAsync(job.JobId, CancellationToken.None);
        Assert.DoesNotContain(all, decision => decision.Decision == DecisionValue.Approve);
    }
}
