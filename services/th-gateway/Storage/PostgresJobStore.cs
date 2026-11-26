using System.Text.Json;
using Npgsql;
using NpgsqlTypes;
using TenderHive.Contracts;
using TenderHive.Gateway.Domain;

namespace TenderHive.Gateway.Storage;

/// <summary>
/// Рабочее хранилище шлюза на PostgreSQL. Составные операции выполняются в транзакции:
/// задание и событие outbox записываются вместе, поэтому событие не теряется при падении сервиса.
/// </summary>
public sealed class PostgresJobStore : IJobStore, IAsyncDisposable
{
    private const string JobColumns =
        "job_id, idempotency_key, source, created_at, status, kb_version, lot_ids, profile_ref, telegram_chat_id, recommendation, margin_percent, artifacts_delivered_at";

    private const string DecisionColumns =
        "decision_id, job_id, reason_code, role, created_at, deadline, timeout_hours, decision, comment, decided_by, decided_at";

    private const string ArtifactColumns =
        "artifact_id, job_id, kind, file_name, content_type, version, size, sha256, storage_path, created_at";

    private const string DocumentColumns =
        "document_id, job_id, file_name, content_type, size, sha256, storage_path";

    private const string StepColumns =
        "step_id, job_id, code, title, state, started_at, finished_at, detail";

    private const string KbVersionColumns =
        "version, status, created_at, activated_at, activated_by, rules_count, notes";

    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web);

    private readonly NpgsqlDataSource _dataSource;

    public PostgresJobStore(string dsn)
    {
        _dataSource = NpgsqlDataSource.Create(dsn);
    }

    public string Name => "postgresql";

    public async Task InitializeAsync(CancellationToken ct)
    {
        var assembly = typeof(PostgresJobStore).Assembly;
        await using var stream = assembly.GetManifestResourceStream("TenderHive.Gateway.Storage.Schema.sql")
            ?? throw new InvalidOperationException("Схема PostgreSQL не найдена в сборке шлюза.");
        using var reader = new StreamReader(stream);
        var sql = await reader.ReadToEndAsync(ct);

        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand(sql, connection);
        await command.ExecuteNonQueryAsync(ct);
    }

    public async Task AddUserAsync(UserRecord user, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand(
            "insert into users (user_id, username, display_name, role, password_hash) values (@user_id, @username, @display_name, @role, @password_hash) on conflict (username) do nothing",
            connection);
        AddGuid(command.Parameters, "user_id", user.UserId);
        AddText(command.Parameters, "username", user.Username);
        AddText(command.Parameters, "display_name", user.DisplayName);
        AddText(command.Parameters, "role", user.Role);
        AddText(command.Parameters, "password_hash", user.PasswordHash);
        await command.ExecuteNonQueryAsync(ct);
    }

    public async Task<UserRecord?> FindUserAsync(string username, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand(
            "select user_id, username, display_name, role, password_hash from users where lower(username) = lower(@username)",
            connection);
        AddText(command.Parameters, "username", username);

        await using var reader = await command.ExecuteReaderAsync(ct);
        if (!await reader.ReadAsync(ct))
        {
            return null;
        }

        return new UserRecord
        {
            UserId = reader.GetGuid(0),
            Username = reader.GetString(1),
            DisplayName = reader.GetString(2),
            Role = reader.GetString(3),
            PasswordHash = reader.GetString(4)
        };
    }

    public async Task<JobRecord?> FindJobByIdempotencyKeyAsync(string key, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand($"select {JobColumns} from jobs where idempotency_key = @key", connection);
        AddText(command.Parameters, "key", key);

        await using var reader = await command.ExecuteReaderAsync(ct);
        return await reader.ReadAsync(ct) ? MapJob(reader) : null;
    }

    public async Task<JobRecord?> GetJobAsync(Guid jobId, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand($"select {JobColumns} from jobs where job_id = @job_id", connection);
        AddGuid(command.Parameters, "job_id", jobId);

        await using var reader = await command.ExecuteReaderAsync(ct);
        return await reader.ReadAsync(ct) ? MapJob(reader) : null;
    }

    public async Task<IReadOnlyList<JobRecord>> ListJobsAsync(int limit, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand($"select {JobColumns} from jobs order by created_at desc limit @limit", connection);
        AddInt(command.Parameters, "limit", Math.Max(1, limit));

        var jobs = new List<JobRecord>();
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            jobs.Add(MapJob(reader));
        }

        return jobs;
    }

    public async Task<bool> CreateJobAsync(JobRecord job, IReadOnlyList<DocumentRecord> documents, OutboxEventRecord outbox, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        var transaction = (NpgsqlTransaction)await connection.BeginTransactionAsync(ct);

        int inserted;
        await using (var command = new NpgsqlCommand(
            $"insert into jobs ({JobColumns}) values (@job_id, @idempotency_key, @source, @created_at, @status, @kb_version, @lot_ids, @profile_ref, @telegram_chat_id, @recommendation, @margin_percent, @artifacts_delivered_at) on conflict (idempotency_key) do nothing",
            connection,
            transaction))
        {
            AddJobParameters(command, job);
            inserted = await command.ExecuteNonQueryAsync(ct);
        }

        if (inserted == 0)
        {
            await transaction.RollbackAsync(ct);
            return false;
        }

        foreach (var document in documents)
        {
            await using var command = new NpgsqlCommand(
                $"insert into job_documents ({DocumentColumns}) values (@document_id, @job_id, @file_name, @content_type, @size, @sha256, @storage_path)",
                connection,
                transaction);
            AddGuid(command.Parameters, "document_id", document.DocumentId);
            AddGuid(command.Parameters, "job_id", document.JobId);
            AddText(command.Parameters, "file_name", document.FileName);
            AddText(command.Parameters, "content_type", document.ContentType);
            AddLong(command.Parameters, "size", document.Size);
            AddText(command.Parameters, "sha256", document.Sha256);
            AddText(command.Parameters, "storage_path", document.StoragePath);
            await command.ExecuteNonQueryAsync(ct);
        }

        await InsertEventAsync(connection, transaction, outbox, ct);
        await transaction.CommitAsync(ct);
        return true;
    }

    public async Task TouchJobStatusAsync(Guid jobId, string status, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand("update jobs set status = @status where job_id = @job_id", connection);
        AddText(command.Parameters, "status", status);
        AddGuid(command.Parameters, "job_id", jobId);
        await command.ExecuteNonQueryAsync(ct);
    }

    public async Task<IReadOnlyList<DocumentRecord>> ListDocumentsAsync(Guid jobId, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand($"select {DocumentColumns} from job_documents where job_id = @job_id order by file_name", connection);
        AddGuid(command.Parameters, "job_id", jobId);

        var documents = new List<DocumentRecord>();
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            documents.Add(new DocumentRecord
            {
                DocumentId = reader.GetGuid(0),
                JobId = reader.GetGuid(1),
                FileName = reader.GetString(2),
                ContentType = reader.GetString(3),
                Size = reader.GetInt64(4),
                Sha256 = reader.GetString(5),
                StoragePath = reader.GetString(6)
            });
        }

        return documents;
    }

    public async Task AddStepAsync(StepRecord step, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand(
            $"insert into job_steps ({StepColumns}) values (@step_id, @job_id, @code, @title, @state, @started_at, @finished_at, @detail)",
            connection);
        AddGuid(command.Parameters, "step_id", step.StepId);
        AddGuid(command.Parameters, "job_id", step.JobId);
        AddText(command.Parameters, "code", step.Code);
        AddText(command.Parameters, "title", step.Title);
        AddText(command.Parameters, "state", step.State);
        AddTime(command.Parameters, "started_at", step.StartedAt);
        AddTimeOrNull(command.Parameters, "finished_at", step.FinishedAt);
        AddText(command.Parameters, "detail", step.Detail);
        await command.ExecuteNonQueryAsync(ct);
    }

    public async Task<IReadOnlyList<StepRecord>> ListStepsAsync(Guid jobId, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand($"select {StepColumns} from job_steps where job_id = @job_id order by started_at", connection);
        AddGuid(command.Parameters, "job_id", jobId);

        var steps = new List<StepRecord>();
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            steps.Add(new StepRecord
            {
                StepId = reader.GetGuid(0),
                JobId = reader.GetGuid(1),
                Code = reader.GetString(2),
                Title = reader.GetString(3),
                State = reader.GetString(4),
                StartedAt = ReadTime(reader, 5),
                FinishedAt = reader.IsDBNull(6) ? null : ReadTime(reader, 6),
                Detail = reader.IsDBNull(7) ? null : reader.GetString(7)
            });
        }

        return steps;
    }

    public async Task SaveResultAsync(JobRecord job, string resultJson, IReadOnlyList<ArtifactRecord> artifacts, IReadOnlyList<DecisionRecord> decisions, IReadOnlyList<OutboxEventRecord> outbox, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        var transaction = (NpgsqlTransaction)await connection.BeginTransactionAsync(ct);

        await using (var command = new NpgsqlCommand(
            "update jobs set status = @status, kb_version = @kb_version, recommendation = @recommendation, margin_percent = @margin_percent where job_id = @job_id",
            connection,
            transaction))
        {
            AddText(command.Parameters, "status", job.Status);
            AddText(command.Parameters, "kb_version", job.KbVersion);
            AddText(command.Parameters, "recommendation", job.Recommendation);
            AddDoubleOrNull(command.Parameters, "margin_percent", job.MarginPercent);
            AddGuid(command.Parameters, "job_id", job.JobId);
            await command.ExecuteNonQueryAsync(ct);
        }

        await using (var command = new NpgsqlCommand(
            "insert into job_results (job_id, payload, created_at) values (@job_id, @payload, @created_at) on conflict (job_id) do update set payload = excluded.payload, created_at = excluded.created_at",
            connection,
            transaction))
        {
            AddGuid(command.Parameters, "job_id", job.JobId);
            AddJson(command.Parameters, "payload", resultJson);
            AddTime(command.Parameters, "created_at", DateTimeOffset.UtcNow);
            await command.ExecuteNonQueryAsync(ct);
        }

        foreach (var artifact in artifacts)
        {
            await using var command = new NpgsqlCommand(
                $"insert into artifacts ({ArtifactColumns}) values (@artifact_id, @job_id, @kind, @file_name, @content_type, @version, @size, @sha256, @storage_path, @created_at)",
                connection,
                transaction);
            AddGuid(command.Parameters, "artifact_id", artifact.ArtifactId);
            AddGuid(command.Parameters, "job_id", artifact.JobId);
            AddText(command.Parameters, "kind", artifact.Kind);
            AddText(command.Parameters, "file_name", artifact.FileName);
            AddText(command.Parameters, "content_type", artifact.ContentType);
            AddInt(command.Parameters, "version", artifact.Version);
            AddLong(command.Parameters, "size", artifact.Size);
            AddText(command.Parameters, "sha256", artifact.Sha256);
            AddText(command.Parameters, "storage_path", artifact.StoragePath);
            AddTime(command.Parameters, "created_at", artifact.CreatedAt);
            await command.ExecuteNonQueryAsync(ct);
        }

        foreach (var decision in decisions)
        {
            await using var command = new NpgsqlCommand(
                $"insert into human_decisions ({DecisionColumns}) values (@decision_id, @job_id, @reason_code, @role, @created_at, @deadline, @timeout_hours, @decision, @comment, @decided_by, @decided_at) on conflict (decision_id) do nothing",
                connection,
                transaction);
            AddDecisionParameters(command, decision);
            await command.ExecuteNonQueryAsync(ct);
        }

        foreach (var item in outbox)
        {
            await InsertEventAsync(connection, transaction, item, ct);
        }

        await transaction.CommitAsync(ct);
    }

    public async Task<string?> GetResultJsonAsync(Guid jobId, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand("select payload::text from job_results where job_id = @job_id", connection);
        AddGuid(command.Parameters, "job_id", jobId);
        var value = await command.ExecuteScalarAsync(ct);
        return value as string;
    }

    public async Task<IReadOnlyList<ArtifactRecord>> ListArtifactsAsync(Guid jobId, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand(
            $"select {ArtifactColumns} from artifacts where job_id = @job_id order by kind, version",
            connection);
        AddGuid(command.Parameters, "job_id", jobId);

        var artifacts = new List<ArtifactRecord>();
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            artifacts.Add(MapArtifact(reader));
        }

        return artifacts;
    }

    public async Task<ArtifactRecord?> GetArtifactAsync(Guid artifactId, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand($"select {ArtifactColumns} from artifacts where artifact_id = @artifact_id", connection);
        AddGuid(command.Parameters, "artifact_id", artifactId);

        await using var reader = await command.ExecuteReaderAsync(ct);
        return await reader.ReadAsync(ct) ? MapArtifact(reader) : null;
    }

    public async Task<int> CountArtifactsByKindAsync(Guid jobId, string kind, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand(
            "select count(*) from artifacts where job_id = @job_id and kind = @kind",
            connection);
        AddGuid(command.Parameters, "job_id", jobId);
        AddText(command.Parameters, "kind", kind);
        var value = await command.ExecuteScalarAsync(ct);
        return Convert.ToInt32(value);
    }

    public async Task<IReadOnlyList<DecisionRecord>> ListDecisionsAsync(Guid jobId, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand(
            $"select {DecisionColumns} from human_decisions where job_id = @job_id order by created_at",
            connection);
        AddGuid(command.Parameters, "job_id", jobId);

        var decisions = new List<DecisionRecord>();
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            decisions.Add(MapDecision(reader));
        }

        return decisions;
    }

    public async Task<IReadOnlyList<DecisionRecord>> ListOpenDecisionsAsync(int limit, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand(
            $"select {DecisionColumns} from human_decisions where decision = @pending order by deadline limit @limit",
            connection);
        AddText(command.Parameters, "pending", DecisionValue.Pending);
        AddInt(command.Parameters, "limit", Math.Max(1, limit));

        var decisions = new List<DecisionRecord>();
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            decisions.Add(MapDecision(reader));
        }

        return decisions;
    }

    public async Task<DecisionRecord?> GetDecisionAsync(Guid decisionId, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand(
            $"select {DecisionColumns} from human_decisions where decision_id = @decision_id",
            connection);
        AddGuid(command.Parameters, "decision_id", decisionId);

        await using var reader = await command.ExecuteReaderAsync(ct);
        return await reader.ReadAsync(ct) ? MapDecision(reader) : null;
    }

    public async Task<DecisionApplyResult> ApplyDecisionAsync(
        Guid decisionId,
        string decision,
        string? comment,
        string actor,
        string actorRole,
        Func<ApplyContext, ApplyOutcome> outcomeFactory,
        AuditRecord audit,
        CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        var transaction = (NpgsqlTransaction)await connection.BeginTransactionAsync(ct);

        DecisionRecord? card = null;
        await using (var command = new NpgsqlCommand(
            $"update human_decisions set decision = @decision, comment = @comment, decided_by = @actor, decided_at = @now where decision_id = @decision_id and decision = @pending returning {DecisionColumns}",
            connection,
            transaction))
        {
            AddText(command.Parameters, "decision", decision);
            AddText(command.Parameters, "comment", comment);
            AddText(command.Parameters, "actor", actor);
            AddTime(command.Parameters, "now", DateTimeOffset.UtcNow);
            AddGuid(command.Parameters, "decision_id", decisionId);
            AddText(command.Parameters, "pending", DecisionValue.Pending);

            await using var reader = await command.ExecuteReaderAsync(ct);
            if (await reader.ReadAsync(ct))
            {
                card = MapDecision(reader);
            }
        }

        if (card is null)
        {
            await transaction.RollbackAsync(ct);
            return new DecisionApplyResult(false, null, null);
        }

        JobRecord? job = null;
        await using (var command = new NpgsqlCommand($"select {JobColumns} from jobs where job_id = @job_id for update", connection, transaction))
        {
            AddGuid(command.Parameters, "job_id", card.JobId);
            await using var reader = await command.ExecuteReaderAsync(ct);
            if (await reader.ReadAsync(ct))
            {
                job = MapJob(reader);
            }
        }

        if (job is null)
        {
            await transaction.RollbackAsync(ct);
            return new DecisionApplyResult(false, null, null);
        }

        int remaining;
        await using (var command = new NpgsqlCommand(
            "select count(*) from human_decisions where job_id = @job_id and decision = @pending",
            connection,
            transaction))
        {
            AddGuid(command.Parameters, "job_id", card.JobId);
            AddText(command.Parameters, "pending", DecisionValue.Pending);
            remaining = Convert.ToInt32(await command.ExecuteScalarAsync(ct));
        }

        var outcome = outcomeFactory(new ApplyContext(job, card, remaining));

        await using (var command = new NpgsqlCommand("update jobs set status = @status where job_id = @job_id", connection, transaction))
        {
            AddText(command.Parameters, "status", outcome.NewStatus);
            AddGuid(command.Parameters, "job_id", job.JobId);
            await command.ExecuteNonQueryAsync(ct);
        }

        foreach (var item in outcome.Events)
        {
            await InsertEventAsync(connection, transaction, item, ct);
        }

        audit.JobId ??= job.JobId;
        await InsertAuditAsync(connection, transaction, audit, ct);
        await transaction.CommitAsync(ct);

        return new DecisionApplyResult(true, card, outcome.NewStatus);
    }

    public async Task<IReadOnlyList<DecisionRecord>> ListDueDecisionsAsync(DateTimeOffset now, int limit, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand(
            $"select {DecisionColumns} from human_decisions where decision = @pending and deadline <= @now order by deadline limit @limit",
            connection);
        AddText(command.Parameters, "pending", DecisionValue.Pending);
        AddTime(command.Parameters, "now", now);
        AddInt(command.Parameters, "limit", Math.Max(1, limit));

        var decisions = new List<DecisionRecord>();
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            decisions.Add(MapDecision(reader));
        }

        return decisions;
    }

    public async Task<bool> ExpireDecisionAndEscalateAsync(
        Guid decisionId,
        string expiredBy,
        DateTimeOffset now,
        DecisionRecord escalated,
        OutboxEventRecord outbox,
        AuditRecord audit,
        CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        var transaction = (NpgsqlTransaction)await connection.BeginTransactionAsync(ct);

        int updated;
        await using (var command = new NpgsqlCommand(
            "update human_decisions set decision = @timeout, decided_by = @actor, decided_at = @now, comment = @comment where decision_id = @decision_id and decision = @pending",
            connection,
            transaction))
        {
            AddText(command.Parameters, "timeout", DecisionValue.Timeout);
            AddText(command.Parameters, "actor", expiredBy);
            AddTime(command.Parameters, "now", now);
            AddText(command.Parameters, "comment", $"Срок ожидания истёк {now:yyyy-MM-dd HH:mm} UTC. Карточка передана руководителю подразделения.");
            AddGuid(command.Parameters, "decision_id", decisionId);
            AddText(command.Parameters, "pending", DecisionValue.Pending);
            updated = await command.ExecuteNonQueryAsync(ct);
        }

        if (updated == 0)
        {
            await transaction.RollbackAsync(ct);
            return false;
        }

        escalated.CreatedAt = escalated.CreatedAt == default ? now : escalated.CreatedAt;
        escalated.Deadline = escalated.Deadline == default ? now.AddHours(escalated.TimeoutHours) : escalated.Deadline;

        await using (var command = new NpgsqlCommand(
            $"insert into human_decisions ({DecisionColumns}) values (@decision_id, @job_id, @reason_code, @role, @created_at, @deadline, @timeout_hours, @decision, @comment, @decided_by, @decided_at) on conflict (decision_id) do nothing",
            connection,
            transaction))
        {
            AddDecisionParameters(command, escalated);
            await command.ExecuteNonQueryAsync(ct);
        }

        await InsertEventAsync(connection, transaction, outbox, ct);
        await InsertAuditAsync(connection, transaction, audit, ct);
        await transaction.CommitAsync(ct);
        return true;
    }

    public async Task<IReadOnlyList<JobRecord>> ListJobsAwaitingDeliveryAsync(int limit, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand(
            $"select {JobColumns} from jobs where status = @status and telegram_chat_id is not null and artifacts_delivered_at is null order by created_at limit @limit",
            connection);
        AddText(command.Parameters, "status", JobStatus.Completed);
        AddInt(command.Parameters, "limit", Math.Max(1, limit));

        var jobs = new List<JobRecord>();
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            jobs.Add(MapJob(reader));
        }

        return jobs;
    }

    public async Task MarkArtifactsDeliveredAsync(Guid jobId, DateTimeOffset at, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand("update jobs set artifacts_delivered_at = @at where job_id = @job_id", connection);
        AddTime(command.Parameters, "at", at);
        AddGuid(command.Parameters, "job_id", jobId);
        await command.ExecuteNonQueryAsync(ct);
    }

    public async Task WriteAuditAsync(AuditRecord audit, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await InsertAuditAsync(connection, null, audit, ct);
    }

    public async Task<IReadOnlyList<AuditRecord>> ListAuditAsync(Guid? jobId, int limit, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        var sql = jobId is null
            ? "select id, occurred_at, actor, role, action, job_id, details from audit_entries order by occurred_at desc limit @limit"
            : "select id, occurred_at, actor, role, action, job_id, details from audit_entries where job_id = @job_id order by occurred_at desc limit @limit";
        await using var command = new NpgsqlCommand(sql, connection);
        AddInt(command.Parameters, "limit", Math.Max(1, limit));
        if (jobId is not null)
        {
            AddGuid(command.Parameters, "job_id", jobId.Value);
        }

        var entries = new List<AuditRecord>();
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            entries.Add(new AuditRecord
            {
                Id = reader.GetInt64(0),
                OccurredAt = ReadTime(reader, 1),
                Actor = reader.GetString(2),
                Role = reader.GetString(3),
                Action = reader.GetString(4),
                JobId = reader.IsDBNull(5) ? null : reader.GetGuid(5),
                Details = reader.IsDBNull(6) ? null : reader.GetString(6)
            });
        }

        return entries;
    }

    public async Task<KbVersionRecord?> GetActiveKbVersionAsync(CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand(
            $"select {KbVersionColumns} from kb_versions where status = 'active' limit 1",
            connection);

        await using var reader = await command.ExecuteReaderAsync(ct);
        return await reader.ReadAsync(ct) ? MapKbVersion(reader) : null;
    }

    public async Task<IReadOnlyList<KbVersionRecord>> ListKbVersionsAsync(CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand(
            $"select {KbVersionColumns} from kb_versions order by created_at desc",
            connection);

        var versions = new List<KbVersionRecord>();
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            versions.Add(MapKbVersion(reader));
        }

        return versions;
    }

    public async Task<bool> ActivateKbVersionAsync(string version, string actor, string? comment, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        var transaction = (NpgsqlTransaction)await connection.BeginTransactionAsync(ct);

        KbVersionRecord? target = null;
        await using (var command = new NpgsqlCommand($"select {KbVersionColumns} from kb_versions where version = @version for update", connection, transaction))
        {
            AddText(command.Parameters, "version", version);
            await using var reader = await command.ExecuteReaderAsync(ct);
            if (await reader.ReadAsync(ct))
            {
                target = MapKbVersion(reader);
            }
        }

        if (target is null)
        {
            await transaction.RollbackAsync(ct);
            return false;
        }

        KbVersionRecord? active = null;
        await using (var command = new NpgsqlCommand($"select {KbVersionColumns} from kb_versions where status = 'active' for update", connection, transaction))
        {
            await using var reader = await command.ExecuteReaderAsync(ct);
            if (await reader.ReadAsync(ct))
            {
                active = MapKbVersion(reader);
            }
        }

        var action = active is not null && target.CreatedAt < active.CreatedAt ? "rollback" : "activate";

        await using (var command = new NpgsqlCommand("update kb_versions set status = 'archived' where status = 'active'", connection, transaction))
        {
            await command.ExecuteNonQueryAsync(ct);
        }

        await using (var command = new NpgsqlCommand(
            "update kb_versions set status = 'active', activated_at = @now, activated_by = @actor where version = @version",
            connection,
            transaction))
        {
            AddTime(command.Parameters, "now", DateTimeOffset.UtcNow);
            AddText(command.Parameters, "actor", actor);
            AddText(command.Parameters, "version", version);
            await command.ExecuteNonQueryAsync(ct);
        }

        await using (var command = new NpgsqlCommand(
            "insert into kb_activations (version, action, actor, occurred_at, comment) values (@version, @action, @actor, @occurred_at, @comment)",
            connection,
            transaction))
        {
            AddText(command.Parameters, "version", version);
            AddText(command.Parameters, "action", action);
            AddText(command.Parameters, "actor", actor);
            AddTime(command.Parameters, "occurred_at", DateTimeOffset.UtcNow);
            AddText(command.Parameters, "comment", comment);
            await command.ExecuteNonQueryAsync(ct);
        }

        await transaction.CommitAsync(ct);
        return true;
    }

    public async Task<IReadOnlyList<KbActivationRecord>> ListKbActivationsAsync(int limit, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand(
            "select id, version, action, actor, occurred_at, comment from kb_activations order by occurred_at desc limit @limit",
            connection);
        AddInt(command.Parameters, "limit", Math.Max(1, limit));

        var activations = new List<KbActivationRecord>();
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            activations.Add(new KbActivationRecord
            {
                Id = reader.GetInt64(0),
                Version = reader.GetString(1),
                Action = reader.GetString(2),
                Actor = reader.GetString(3),
                OccurredAt = ReadTime(reader, 4),
                Comment = reader.IsDBNull(5) ? null : reader.GetString(5)
            });
        }

        return activations;
    }

    public async Task EnsureKbVersionAsync(KbVersionRecord version, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand(
            $"insert into kb_versions ({KbVersionColumns}) values (@version, @status, @created_at, @activated_at, @activated_by, @rules_count, @notes) on conflict (version) do nothing",
            connection);
        AddText(command.Parameters, "version", version.Version);
        AddText(command.Parameters, "status", version.Status);
        AddTime(command.Parameters, "created_at", version.CreatedAt);
        AddTimeOrNull(command.Parameters, "activated_at", version.ActivatedAt);
        AddText(command.Parameters, "activated_by", version.ActivatedBy);
        AddInt(command.Parameters, "rules_count", version.RulesCount);
        AddText(command.Parameters, "notes", version.Notes);
        await command.ExecuteNonQueryAsync(ct);
    }

    public async Task<IReadOnlyList<OutboxEventRecord>> DequeueOutboxAsync(int limit, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand(
            "select event_id, topic, payload::text, created_at, published_at, attempts, last_error from outbox_events where published_at is null order by event_id limit @limit",
            connection);
        AddInt(command.Parameters, "limit", Math.Max(1, limit));

        var events = new List<OutboxEventRecord>();
        await using var reader = await command.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            events.Add(new OutboxEventRecord
            {
                EventId = reader.GetInt64(0),
                Topic = reader.GetString(1),
                PayloadJson = reader.GetString(2),
                CreatedAt = ReadTime(reader, 3),
                PublishedAt = reader.IsDBNull(4) ? null : ReadTime(reader, 4),
                Attempts = reader.GetInt32(5),
                LastError = reader.IsDBNull(6) ? null : reader.GetString(6)
            });
        }

        return events;
    }

    public async Task MarkOutboxPublishedAsync(long eventId, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand(
            "update outbox_events set published_at = @now, attempts = attempts + 1, last_error = null where event_id = @event_id",
            connection);
        AddTime(command.Parameters, "now", DateTimeOffset.UtcNow);
        AddLong(command.Parameters, "event_id", eventId);
        await command.ExecuteNonQueryAsync(ct);
    }

    public async Task MarkOutboxFailedAsync(long eventId, string error, CancellationToken ct)
    {
        await using var connection = await _dataSource.OpenConnectionAsync(ct);
        await using var command = new NpgsqlCommand(
            "update outbox_events set attempts = attempts + 1, last_error = @error where event_id = @event_id",
            connection);
        AddText(command.Parameters, "error", error);
        AddLong(command.Parameters, "event_id", eventId);
        await command.ExecuteNonQueryAsync(ct);
    }

    public ValueTask DisposeAsync() => _dataSource.DisposeAsync();

    private static async Task InsertEventAsync(NpgsqlConnection connection, NpgsqlTransaction? transaction, OutboxEventRecord item, CancellationToken ct)
    {
        await using var command = new NpgsqlCommand(
            "insert into outbox_events (topic, payload, created_at) values (@topic, @payload, @created_at)",
            connection,
            transaction);
        AddText(command.Parameters, "topic", item.Topic);
        AddJson(command.Parameters, "payload", item.PayloadJson);
        AddTime(command.Parameters, "created_at", item.CreatedAt == default ? DateTimeOffset.UtcNow : item.CreatedAt);
        await command.ExecuteNonQueryAsync(ct);
    }

    private static async Task InsertAuditAsync(NpgsqlConnection connection, NpgsqlTransaction? transaction, AuditRecord audit, CancellationToken ct)
    {
        await using var command = new NpgsqlCommand(
            "insert into audit_entries (occurred_at, actor, role, action, job_id, details) values (@occurred_at, @actor, @role, @action, @job_id, @details)",
            connection,
            transaction);
        AddTime(command.Parameters, "occurred_at", audit.OccurredAt == default ? DateTimeOffset.UtcNow : audit.OccurredAt);
        AddText(command.Parameters, "actor", audit.Actor);
        AddText(command.Parameters, "role", audit.Role);
        AddText(command.Parameters, "action", audit.Action);
        AddGuidOrNull(command.Parameters, "job_id", audit.JobId);
        AddText(command.Parameters, "details", audit.Details);
        await command.ExecuteNonQueryAsync(ct);
    }

    private static void AddJobParameters(NpgsqlCommand command, JobRecord job)
    {
        AddGuid(command.Parameters, "job_id", job.JobId);
        AddText(command.Parameters, "idempotency_key", job.IdempotencyKey);
        AddText(command.Parameters, "source", job.Source);
        AddTime(command.Parameters, "created_at", job.CreatedAt);
        AddText(command.Parameters, "status", job.Status);
        AddText(command.Parameters, "kb_version", job.KbVersion);
        AddJson(command.Parameters, "lot_ids", JsonSerializer.Serialize(job.LotIds, JsonOptions));
        AddText(command.Parameters, "profile_ref", job.ProfileRef);
        AddText(command.Parameters, "telegram_chat_id", job.TelegramChatId);
        AddText(command.Parameters, "recommendation", job.Recommendation);
        AddDoubleOrNull(command.Parameters, "margin_percent", job.MarginPercent);
        AddTimeOrNull(command.Parameters, "artifacts_delivered_at", job.ArtifactsDeliveredAt);
    }

    private static void AddDecisionParameters(NpgsqlCommand command, DecisionRecord decision)
    {
        AddGuid(command.Parameters, "decision_id", decision.DecisionId);
        AddGuid(command.Parameters, "job_id", decision.JobId);
        AddText(command.Parameters, "reason_code", decision.ReasonCode);
        AddText(command.Parameters, "role", decision.Role);
        AddTime(command.Parameters, "created_at", decision.CreatedAt == default ? DateTimeOffset.UtcNow : decision.CreatedAt);
        AddTime(command.Parameters, "deadline", decision.Deadline);
        AddDouble(command.Parameters, "timeout_hours", decision.TimeoutHours);
        AddText(command.Parameters, "decision", string.IsNullOrEmpty(decision.Decision) ? DecisionValue.Pending : decision.Decision);
        AddText(command.Parameters, "comment", decision.Comment);
        AddText(command.Parameters, "decided_by", decision.DecidedBy);
        AddTimeOrNull(command.Parameters, "decided_at", decision.DecidedAt);
    }

    private static JobRecord MapJob(NpgsqlDataReader reader) => new()
    {
        JobId = reader.GetGuid(0),
        IdempotencyKey = reader.GetString(1),
        Source = reader.GetString(2),
        CreatedAt = ReadTime(reader, 3),
        Status = reader.GetString(4),
        KbVersion = reader.GetString(5),
        LotIds = DeserializeStringList(reader.GetString(6)),
        ProfileRef = reader.GetString(7),
        TelegramChatId = reader.IsDBNull(8) ? null : reader.GetString(8),
        Recommendation = reader.IsDBNull(9) ? null : reader.GetString(9),
        MarginPercent = reader.IsDBNull(10) ? null : reader.GetDouble(10),
        ArtifactsDeliveredAt = reader.IsDBNull(11) ? null : ReadTime(reader, 11)
    };

    private static DecisionRecord MapDecision(NpgsqlDataReader reader) => new()
    {
        DecisionId = reader.GetGuid(0),
        JobId = reader.GetGuid(1),
        ReasonCode = reader.GetString(2),
        Role = reader.GetString(3),
        CreatedAt = ReadTime(reader, 4),
        Deadline = ReadTime(reader, 5),
        TimeoutHours = reader.GetDouble(6),
        Decision = reader.GetString(7),
        Comment = reader.IsDBNull(8) ? null : reader.GetString(8),
        DecidedBy = reader.IsDBNull(9) ? null : reader.GetString(9),
        DecidedAt = reader.IsDBNull(10) ? null : ReadTime(reader, 10)
    };

    private static ArtifactRecord MapArtifact(NpgsqlDataReader reader) => new()
    {
        ArtifactId = reader.GetGuid(0),
        JobId = reader.GetGuid(1),
        Kind = reader.GetString(2),
        FileName = reader.GetString(3),
        ContentType = reader.GetString(4),
        Version = reader.GetInt32(5),
        Size = reader.GetInt64(6),
        Sha256 = reader.GetString(7),
        StoragePath = reader.GetString(8),
        CreatedAt = ReadTime(reader, 9)
    };

    private static KbVersionRecord MapKbVersion(NpgsqlDataReader reader) => new()
    {
        Version = reader.GetString(0),
        Status = reader.GetString(1),
        CreatedAt = ReadTime(reader, 2),
        ActivatedAt = reader.IsDBNull(3) ? null : ReadTime(reader, 3),
        ActivatedBy = reader.IsDBNull(4) ? null : reader.GetString(4),
        RulesCount = reader.GetInt32(5),
        Notes = reader.IsDBNull(6) ? null : reader.GetString(6)
    };

    private static List<string> DeserializeStringList(string json)
    {
        try
        {
            return JsonSerializer.Deserialize<List<string>>(json, JsonOptions) ?? new List<string>();
        }
        catch (JsonException)
        {
            return new List<string>();
        }
    }

    private static DateTimeOffset ReadTime(NpgsqlDataReader reader, int ordinal)
    {
        var value = reader.GetDateTime(ordinal);
        return new DateTimeOffset(DateTime.SpecifyKind(value, DateTimeKind.Utc));
    }

    private static void AddText(NpgsqlParameterCollection parameters, string name, string? value)
    {
        parameters.Add(new NpgsqlParameter(name, NpgsqlDbType.Text) { Value = (object?)value ?? DBNull.Value });
    }

    private static void AddGuid(NpgsqlParameterCollection parameters, string name, Guid value)
    {
        parameters.Add(new NpgsqlParameter(name, NpgsqlDbType.Uuid) { Value = value });
    }

    private static void AddGuidOrNull(NpgsqlParameterCollection parameters, string name, Guid? value)
    {
        parameters.Add(new NpgsqlParameter(name, NpgsqlDbType.Uuid) { Value = (object?)value ?? DBNull.Value });
    }

    private static void AddInt(NpgsqlParameterCollection parameters, string name, int value)
    {
        parameters.Add(new NpgsqlParameter(name, NpgsqlDbType.Integer) { Value = value });
    }

    private static void AddLong(NpgsqlParameterCollection parameters, string name, long value)
    {
        parameters.Add(new NpgsqlParameter(name, NpgsqlDbType.Bigint) { Value = value });
    }

    private static void AddDouble(NpgsqlParameterCollection parameters, string name, double value)
    {
        parameters.Add(new NpgsqlParameter(name, NpgsqlDbType.Double) { Value = value });
    }

    private static void AddDoubleOrNull(NpgsqlParameterCollection parameters, string name, double? value)
    {
        parameters.Add(new NpgsqlParameter(name, NpgsqlDbType.Double) { Value = (object?)value ?? DBNull.Value });
    }

    private static void AddTime(NpgsqlParameterCollection parameters, string name, DateTimeOffset value)
    {
        parameters.Add(new NpgsqlParameter(name, NpgsqlDbType.TimestampTz) { Value = value.UtcDateTime });
    }

    private static void AddTimeOrNull(NpgsqlParameterCollection parameters, string name, DateTimeOffset? value)
    {
        parameters.Add(new NpgsqlParameter(name, NpgsqlDbType.TimestampTz) { Value = (object?)value?.UtcDateTime ?? DBNull.Value });
    }

    private static void AddJson(NpgsqlParameterCollection parameters, string name, string json)
    {
        parameters.Add(new NpgsqlParameter(name, NpgsqlDbType.Jsonb) { Value = json });
    }
}
