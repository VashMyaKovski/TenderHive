using TenderHive.Contracts;
using TenderHive.Gateway.Configuration;
using TenderHive.Gateway.Domain;
using TenderHive.Gateway.Services;
using TenderHive.Gateway.Storage;

namespace TenderHive.Gateway.Hitl;

/// <summary>
/// Контроль сроков ожидания. Просроченная карточка закрывается значением «таймаут» и передаётся
/// руководителю подразделения новой карточкой; автоматическое согласие не формируется.
/// </summary>
public sealed class DecisionTimeoutService : BackgroundService
{
    private readonly IJobStore _store;
    private readonly GatewayOptions _options;
    private readonly ILogger<DecisionTimeoutService> _logger;

    public DecisionTimeoutService(IJobStore store, GatewayOptions options, ILogger<DecisionTimeoutService> logger)
    {
        _store = store;
        _options = options;
        _logger = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                await SweepAsync(stoppingToken);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                _logger.LogError(exception, "Ошибка контроля сроков согласования");
            }

            try
            {
                await Task.Delay(TimeSpan.FromSeconds(_options.TimeoutSweepSeconds), stoppingToken);
            }
            catch (OperationCanceledException)
            {
                break;
            }
        }
    }

    private async Task SweepAsync(CancellationToken ct)
    {
        var now = DateTimeOffset.UtcNow;
        var due = await _store.ListDueDecisionsAsync(now, 50, ct);
        foreach (var card in due)
        {
            var escalated = new DecisionRecord
            {
                DecisionId = Guid.NewGuid(),
                JobId = card.JobId,
                ReasonCode = card.ReasonCode,
                Role = AppRole.DepartmentHead,
                CreatedAt = now,
                Deadline = now.AddHours(card.TimeoutHours),
                TimeoutHours = card.TimeoutHours,
                Decision = DecisionValue.Pending,
                Comment = $"Передано руководителю по таймауту карточки {card.DecisionId}."
            };

            var outbox = JobService.Event(EventTopic.HumanRequired, new
            {
                job_id = card.JobId,
                decision_id = escalated.DecisionId,
                reason_code = card.ReasonCode,
                role = AppRole.DepartmentHead,
                escalated_from = card.DecisionId,
                timeout = true
            });

            var expired = await _store.ExpireDecisionAndEscalateAsync(
                card.DecisionId,
                "system",
                now,
                escalated,
                outbox,
                new AuditRecord
                {
                    OccurredAt = now,
                    Actor = "system",
                    Role = AppRole.Service,
                    Action = "decision.timeout",
                    JobId = card.JobId,
                    Details = $"decision_id={card.DecisionId}, escalated_to={escalated.DecisionId}, reason_code={card.ReasonCode}"
                },
                ct);

            if (expired)
            {
                _logger.LogInformation("Карточка {DecisionId} просрочена и передана руководителю ({EscalatedId})", card.DecisionId, escalated.DecisionId);
            }
        }
    }
}
