using System.Text.Json;
using TenderHive.Contracts;
using TenderHive.Gateway.Configuration;
using TenderHive.Gateway.Domain;
using TenderHive.Gateway.Storage;

namespace TenderHive.Gateway.Services;

/// <summary>
/// Прикладная логика шлюза: регистрация задания, приём результата оркестратора,
/// фиксация решения человека. Изменение состояния и события outbox пишутся одной транзакцией.
/// </summary>
public sealed class JobService
{
    private static readonly JsonSerializerOptions Json = JsonContract.Options;

    private readonly IJobStore _store;
    private readonly IArtifactStorage _storage;
    private readonly GatewayOptions _options;

    public JobService(IJobStore store, IArtifactStorage storage, GatewayOptions options)
    {
        _store = store;
        _storage = storage;
        _options = options;
    }

    public sealed record IncomingFile(string PartName, string FileName, string ContentType, byte[] Content);

    public sealed record CreateJobInput(string Source, string Mailbox, string? MessageId, string ProfileRef, string? TelegramChatId, IReadOnlyList<IncomingFile> Files);

    public sealed record CreateJobOutcome(JobDto Job, bool Created);

    public async Task<CreateJobOutcome> CreateJobAsync(CreateJobInput input, UserContext actor, CancellationToken ct)
    {
        if (input.Files is null || input.Files.Count == 0)
        {
            throw new ApiException(400, "Комплект не принят: не передан ни один файл.");
        }

        var source = NormalizeSource(input.Source);
        foreach (var file in input.Files)
        {
            if (file.Content.LongLength > _options.MaxUploadBytes)
            {
                throw new ApiException(413, $"Файл «{file.FileName}» больше допустимого размера.");
            }
        }

        var activeKb = await _store.GetActiveKbVersionAsync(ct)
            ?? throw new ApiException(409, "Активная версия базы знаний не задана: обработка задания невозможна.");

        var hashes = input.Files.Select(file => IdempotencyKey.Sha256Hex(file.Content)).ToList();
        var mailbox = string.IsNullOrWhiteSpace(input.Mailbox) ? "-" : input.Mailbox.Trim();
        var messageId = string.IsNullOrWhiteSpace(input.MessageId) ? "-" : input.MessageId.Trim();
        var idempotencyKey = IdempotencyKey.Create(source, mailbox, messageId, hashes);

        var existing = await _store.FindJobByIdempotencyKeyAsync(idempotencyKey, ct);
        if (existing is not null)
        {
            return new CreateJobOutcome(await ToDtoAsync(existing, ct), false);
        }

        var jobId = Guid.NewGuid();
        var createdAt = DateTimeOffset.UtcNow;
        var documents = new List<DocumentRecord>();
        for (var index = 0; index < input.Files.Count; index++)
        {
            var file = input.Files[index];
            var storagePath = _storage.Save("documents", jobId, file.FileName, file.Content);
            documents.Add(new DocumentRecord
            {
                DocumentId = Guid.NewGuid(),
                JobId = jobId,
                FileName = file.FileName,
                ContentType = file.ContentType,
                Size = file.Content.LongLength,
                Sha256 = hashes[index],
                StoragePath = storagePath
            });
        }

        var profileRef = string.IsNullOrWhiteSpace(input.ProfileRef) ? "company-default" : input.ProfileRef.Trim();
        var job = new JobRecord
        {
            JobId = jobId,
            IdempotencyKey = idempotencyKey,
            Source = source,
            CreatedAt = createdAt,
            Status = JobStatus.Queued,
            KbVersion = activeKb.Version,
            ProfileRef = profileRef,
            TelegramChatId = string.IsNullOrWhiteSpace(input.TelegramChatId) ? null : input.TelegramChatId.Trim()
        };

        var created = await _store.CreateJobAsync(
            job,
            documents,
            Event(EventTopic.JobCreated, new
            {
                job_id = jobId,
                idempotency_key = idempotencyKey,
                source,
                created_at = createdAt,
                kb_version = activeKb.Version,
                profile_ref = profileRef,
                telegram_chat_id = job.TelegramChatId,
                documents = documents.Select(document => new { file_name = document.FileName, sha256 = document.Sha256, size = document.Size })
            }),
            ct);

        if (!created)
        {
            var raced = await _store.FindJobByIdempotencyKeyAsync(idempotencyKey, ct)
                ?? throw new ApiException(409, "Задание с таким идемпотентным ключом уже существует.");
            return new CreateJobOutcome(await ToDtoAsync(raced, ct), false);
        }

        await _store.WriteAuditAsync(new AuditRecord
        {
            OccurredAt = createdAt,
            Actor = actor.Username,
            Role = actor.Role,
            Action = "job.registered",
            JobId = jobId,
            Details = $"source={source}, files={documents.Count}, kb_version={activeKb.Version}, idempotency_key={idempotencyKey}"
        }, ct);

        return new CreateJobOutcome(await ToDtoAsync(job, ct), true);
    }

    public async Task<JobDto> SubmitResultAsync(Guid jobId, ResultSubmissionDto submission, IReadOnlyList<IncomingFile> files, UserContext actor, CancellationToken ct)
    {
        var job = await _store.GetJobAsync(jobId, ct) ?? throw new ApiException(404, "Задание не найдено.");

        if (job.Status is JobStatus.Completed or JobStatus.Rejected)
        {
            throw new ApiException(409, $"Задание уже закрыто (статус {job.Status}): новый результат не принимается.");
        }

        var now = DateTimeOffset.UtcNow;

        var artifacts = new List<ArtifactRecord>();
        var pendingByKind = new Dictionary<string, int>(StringComparer.Ordinal);
        foreach (var descriptor in submission.Artifacts ?? Array.Empty<ResultArtifactDto>())
        {
            var source = files.FirstOrDefault(file =>
                string.Equals(file.PartName, descriptor.ContentPart, StringComparison.OrdinalIgnoreCase) ||
                string.Equals(file.FileName, descriptor.FileName, StringComparison.OrdinalIgnoreCase))
                ?? throw new ApiException(400, $"Файл «{descriptor.FileName}» не найден среди частей запроса.");

            var kind = string.IsNullOrWhiteSpace(descriptor.Kind) ? "document" : descriptor.Kind.Trim();
            pendingByKind.TryGetValue(kind, out var pending);
            var version = await _store.CountArtifactsByKindAsync(jobId, kind, ct) + pending + 1;
            pendingByKind[kind] = pending + 1;

            artifacts.Add(new ArtifactRecord
            {
                ArtifactId = Guid.NewGuid(),
                JobId = jobId,
                Kind = kind,
                FileName = string.IsNullOrWhiteSpace(descriptor.FileName) ? source.FileName : descriptor.FileName,
                ContentType = string.IsNullOrWhiteSpace(descriptor.ContentType) ? "application/octet-stream" : descriptor.ContentType,
                Version = version,
                Size = source.Content.LongLength,
                Sha256 = IdempotencyKey.Sha256Hex(source.Content),
                StoragePath = _storage.Save("artifacts", jobId, descriptor.FileName, source.Content),
                CreatedAt = now
            });
        }

        var escalations = (submission.Escalations ?? Array.Empty<EscalationDto>())
            .Where(item => !string.IsNullOrWhiteSpace(item.ReasonCode))
            .GroupBy(item => item.ReasonCode.Trim(), StringComparer.Ordinal)
            .Select(group => group.First())
            .ToList();

        foreach (var escalation in escalations)
        {
            if (!HitlPolicy.IsKnown(escalation.ReasonCode.Trim()))
            {
                throw new ApiException(400, $"Неизвестное основание остановки «{escalation.ReasonCode}».");
            }
        }

        var openReasons = (await _store.ListDecisionsAsync(jobId, ct))
            .Where(card => card.Decision == DecisionValue.Pending)
            .Select(card => card.ReasonCode)
            .ToHashSet(StringComparer.Ordinal);

        var decisions = new List<DecisionRecord>();
        foreach (var escalation in escalations)
        {
            var reasonCode = escalation.ReasonCode.Trim();
            if (openReasons.Contains(reasonCode))
            {
                continue;
            }

            decisions.Add(new DecisionRecord
            {
                DecisionId = Guid.NewGuid(),
                JobId = jobId,
                ReasonCode = reasonCode,
                Role = HitlPolicy.RoleFor(reasonCode),
                CreatedAt = now,
                Deadline = now.AddHours(_options.HitlTimeoutHours),
                TimeoutHours = _options.HitlTimeoutHours,
                Decision = DecisionValue.Pending,
                Comment = escalation.Detail
            });
        }

        var waitingHuman = escalations.Count > 0;
        job.Status = waitingHuman
            ? JobStatus.WaitingHuman
            : string.Equals(submission.Status, "failed", StringComparison.OrdinalIgnoreCase) ? JobStatus.Failed : JobStatus.Completed;
        job.KbVersion = string.IsNullOrWhiteSpace(submission.KbVersion) ? job.KbVersion : submission.KbVersion.Trim();
        job.Recommendation = submission.Recommendation?.Decision;
        job.MarginPercent = submission.Estimate?.MarginPercent;

        var events = new List<OutboxEventRecord>
        {
            Event(EventTopic.StageDone, new
            {
                job_id = jobId,
                stage = "solver",
                status = job.Status,
                kb_version = job.KbVersion,
                recommendation = job.Recommendation,
                margin_percent = job.MarginPercent,
                escalated = waitingHuman
            })
        };

        if (waitingHuman)
        {
            events.Add(Event(EventTopic.HumanRequired, new
            {
                job_id = jobId,
                cards = decisions.Select(card => new { decision_id = card.DecisionId, reason_code = card.ReasonCode, role = card.Role, deadline = card.Deadline })
            }));
        }

        if (job.Status == JobStatus.Completed)
        {
            events.Add(Event(EventTopic.ArtifactsReady, new
            {
                job_id = jobId,
                artifacts = artifacts.Select(artifact => new { artifact_id = artifact.ArtifactId, kind = artifact.Kind, version = artifact.Version })
            }));
        }

        await _store.SaveResultAsync(job, JsonSerializer.Serialize(submission, Json), artifacts, decisions, events, ct);

        await _store.WriteAuditAsync(new AuditRecord
        {
            OccurredAt = now,
            Actor = actor.Username,
            Role = actor.Role,
            Action = "result.accepted",
            JobId = jobId,
            Details = $"status={job.Status}, artifacts={artifacts.Count}, cards={decisions.Count}, reasons={string.Join(',', escalations.Select(item => item.ReasonCode))}"
        }, ct);

        return await ToDtoAsync(job, ct);
    }

    public async Task ReportStepAsync(Guid jobId, StepReportDto report, UserContext actor, CancellationToken ct)
    {
        var job = await _store.GetJobAsync(jobId, ct) ?? throw new ApiException(404, "Задание не найдено.");
        if (string.IsNullOrWhiteSpace(report.Code))
        {
            throw new ApiException(400, "Не указан код шага.");
        }

        var state = string.IsNullOrWhiteSpace(report.State) ? "running" : report.State.Trim();
        await _store.AddStepAsync(new StepRecord
        {
            StepId = Guid.NewGuid(),
            JobId = jobId,
            Code = report.Code.Trim(),
            Title = string.IsNullOrWhiteSpace(report.Title) ? report.Code.Trim() : report.Title.Trim(),
            State = state,
            StartedAt = DateTimeOffset.UtcNow,
            FinishedAt = string.Equals(state, "finished", StringComparison.OrdinalIgnoreCase) ? DateTimeOffset.UtcNow : null,
            Detail = report.Detail
        }, ct);

        if (job.Status == JobStatus.Queued)
        {
            await _store.TouchJobStatusAsync(jobId, JobStatus.Processing, ct);
        }
    }

    public async Task<DecisionResultDto> DecideAsync(Guid decisionId, string decision, string? comment, UserContext actor, CancellationToken ct)
    {
        var value = (decision ?? string.Empty).Trim().ToLowerInvariant();
        if (value != DecisionValue.Approve && value != DecisionValue.Reject)
        {
            throw new ApiException(400, "Допустимые решения по карточке: approve или reject.");
        }

        var card = await _store.GetDecisionAsync(decisionId, ct)
            ?? throw new ApiException(404, "Карточка согласования не найдена.");

        if (card.Decision != DecisionValue.Pending)
        {
            throw new ApiException(409, $"Решение по карточке уже зафиксировано ({card.Decision}); повторная запись отклонена.");
        }

        var allowed = string.Equals(actor.Role, card.Role, StringComparison.Ordinal) || actor.Role == AppRole.DepartmentHead;
        if (!allowed)
        {
            throw new ApiException(403, $"Решение принимает роль «{card.Role}» или руководитель подразделения; текущая роль — «{actor.Role}».");
        }

        var reasonCode = card.ReasonCode;
        var result = await _store.ApplyDecisionAsync(
            decisionId,
            value,
            comment,
            actor.Username,
            actor.Role,
            context => BuildOutcome(value, context),
            new AuditRecord
            {
                Actor = actor.Username,
                Role = actor.Role,
                Action = "decision." + value,
                Details = $"decision_id={decisionId}, reason_code={reasonCode}, comment={comment}"
            },
            ct);

        if (!result.Applied || result.Decision is null)
        {
            throw new ApiException(409, "Решение по карточке уже зафиксировано: повторная запись отклонена.");
        }

        return new DecisionResultDto(ToDto(result.Decision), result.NewStatus ?? jobStatusUnknown);
    }

    public async Task<IReadOnlyList<JobDto>> ListJobsAsync(int limit, CancellationToken ct)
    {
        var jobs = await _store.ListJobsAsync(limit, ct);
        var result = new List<JobDto>(jobs.Count);
        foreach (var job in jobs)
        {
            result.Add(await ToDtoAsync(job, ct));
        }

        return result;
    }

    public async Task<JobDetailDto> GetJobDetailAsync(Guid jobId, CancellationToken ct)
    {
        var job = await _store.GetJobAsync(jobId, ct) ?? throw new ApiException(404, "Задание не найдено.");
        var steps = await _store.ListStepsAsync(jobId, ct);
        return new JobDetailDto(await ToDtoAsync(job, ct), steps.Select(ToDto).ToList());
    }

    public async Task<JobResultDto?> GetResultAsync(Guid jobId, CancellationToken ct)
    {
        var job = await _store.GetJobAsync(jobId, ct) ?? throw new ApiException(404, "Задание не найдено.");
        var json = await _store.GetResultJsonAsync(jobId, ct);
        if (string.IsNullOrWhiteSpace(json))
        {
            return null;
        }

        ResultSubmissionDto? submission;
        try
        {
            submission = JsonSerializer.Deserialize<ResultSubmissionDto>(json, Json);
        }
        catch (JsonException)
        {
            throw new ApiException(500, "Сохранённый результат задания повреждён.");
        }

        if (submission is null)
        {
            return null;
        }

        return new JobResultDto(
            job.JobId,
            job.KbVersion,
            job.Status,
            submission.Requirements ?? Array.Empty<RequirementDto>(),
            submission.Compliance,
            submission.Resources,
            submission.Estimate,
            submission.Risks ?? Array.Empty<RiskDto>(),
            submission.Recommendation,
            submission.Explanation);
    }

    public async Task<IReadOnlyList<ArtifactDto>> ListArtifactsAsync(Guid jobId, CancellationToken ct)
    {
        _ = await _store.GetJobAsync(jobId, ct) ?? throw new ApiException(404, "Задание не найдено.");
        var artifacts = await _store.ListArtifactsAsync(jobId, ct);
        return artifacts.Select(ToDto).ToList();
    }

    public async Task<ArtifactLinkDto> GetArtifactLinkAsync(Guid jobId, Guid artifactId, ArtifactLinkService links, CancellationToken ct)
    {
        var artifact = await _store.GetArtifactAsync(artifactId, ct) ?? throw new ApiException(404, "Артефакт не найден.");
        if (artifact.JobId != jobId)
        {
            throw new ApiException(404, "Артефакт не принадлежит указанному заданию.");
        }

        var url = links.Create(artifactId, out var expiresAt);
        return new ArtifactLinkDto(url, expiresAt);
    }

    public async Task<IReadOnlyList<HumanDecisionDto>> ListDecisionsAsync(Guid jobId, CancellationToken ct)
    {
        var decisions = await _store.ListDecisionsAsync(jobId, ct);
        return decisions.Select(ToDto).ToList();
    }

    public async Task<IReadOnlyList<HumanDecisionDto>> ListOpenDecisionsAsync(int limit, CancellationToken ct)
    {
        var decisions = await _store.ListOpenDecisionsAsync(limit, ct);
        return decisions.Select(ToDto).ToList();
    }

    public async Task<IReadOnlyList<AuditEntryDto>> ListAuditAsync(Guid? jobId, int limit, CancellationToken ct)
    {
        var entries = await _store.ListAuditAsync(jobId, limit, ct);
        return entries
            .Select(entry => new AuditEntryDto(entry.OccurredAt, entry.Actor, entry.Role, entry.Action, entry.Details))
            .ToList();
    }

    public static OutboxEventRecord Event(string topic, object payload) => new()
    {
        Topic = topic,
        PayloadJson = JsonSerializer.Serialize(payload, Json),
        CreatedAt = DateTimeOffset.UtcNow
    };

    private const string jobStatusUnknown = "unknown";

    private static ApplyOutcome BuildOutcome(string decision, ApplyContext context)
    {
        var events = new List<OutboxEventRecord>
        {
            Event(EventTopic.HumanDecided, new
            {
                job_id = context.Job.JobId,
                decision_id = context.Decision.DecisionId,
                reason_code = context.Decision.ReasonCode,
                decision
            })
        };

        string status;
        if (decision == DecisionValue.Reject)
        {
            status = JobStatus.Rejected;
        }
        else if (context.RemainingOpenDecisions > 0)
        {
            status = JobStatus.WaitingHuman;
        }
        else if (context.Decision.ReasonCode == ReasonCode.FinalApproval)
        {
            status = JobStatus.Completed;
        }
        else
        {
            status = JobStatus.Processing;
        }

        if (status == JobStatus.Completed)
        {
            events.Add(Event(EventTopic.ArtifactsReady, new { job_id = context.Job.JobId }));
        }

        return new ApplyOutcome(status, events);
    }

    private static string NormalizeSource(string source)
    {
        var normalized = (source ?? string.Empty).Trim().ToLowerInvariant();
        return normalized switch
        {
            JobSource.Mail => JobSource.Mail,
            JobSource.Telegram => JobSource.Telegram,
            JobSource.Web => JobSource.Web,
            _ => throw new ApiException(400, "Источник задания должен быть одним из: mail, telegram, web.")
        };
    }

    private async Task<JobDto> ToDtoAsync(JobRecord job, CancellationToken ct)
    {
        var documents = await _store.ListDocumentsAsync(job.JobId, ct);
        return new JobDto(
            job.JobId,
            job.IdempotencyKey,
            job.Source,
            job.CreatedAt,
            job.Status,
            job.KbVersion,
            job.LotIds,
            documents.Select(document => new JobDocumentDto(document.DocumentId, document.FileName, document.ContentType, document.Size, document.Sha256)).ToList(),
            job.ProfileRef,
            job.TelegramChatId,
            job.Recommendation,
            job.MarginPercent);
    }

    private static JobStepDto ToDto(StepRecord step) => new(step.Code, step.Title, step.State, step.StartedAt, step.FinishedAt, step.Detail);

    private static ArtifactDto ToDto(ArtifactRecord artifact) => new(artifact.ArtifactId, artifact.Kind, artifact.FileName, artifact.ContentType, artifact.Version, artifact.Size, artifact.Sha256, artifact.CreatedAt);

    private static HumanDecisionDto ToDto(DecisionRecord decision) => new(
        decision.DecisionId,
        decision.JobId,
        decision.ReasonCode,
        decision.Role,
        decision.CreatedAt,
        decision.Deadline,
        decision.TimeoutHours,
        decision.Decision,
        decision.Comment,
        decision.DecidedBy,
        decision.DecidedAt);
}
