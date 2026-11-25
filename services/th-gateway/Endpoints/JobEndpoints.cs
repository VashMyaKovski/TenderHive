using System.Text;
using System.Text.Json;
using TenderHive.Contracts;
using TenderHive.Gateway.Configuration;
using TenderHive.Gateway.Services;
using TenderHive.Gateway.Storage;

namespace TenderHive.Gateway.Endpoints;

public static class JobEndpoints
{
    private static readonly JsonSerializerOptions Json = JsonContract.Options;

    public static IEndpointRouteBuilder MapJobEndpoints(this IEndpointRouteBuilder app)
    {
        var jobs = app.MapGroup("/api/jobs");

        jobs.MapPost("/", CreateJobAsync)
            .RequireAuthorization(Policies.JobCreate)
            .DisableAntiforgery();

        jobs.MapGet("/", async (int? limit, JobService service, CancellationToken ct) =>
                Results.Ok(await service.ListJobsAsync(limit ?? 50, ct)))
            .RequireAuthorization();

        jobs.MapGet("/{jobId:guid}", async (Guid jobId, JobService service, CancellationToken ct) =>
                Results.Ok(await service.GetJobDetailAsync(jobId, ct)))
            .RequireAuthorization();

        jobs.MapGet("/{jobId:guid}/result", async (Guid jobId, JobService service, CancellationToken ct) =>
            {
                var result = await service.GetResultAsync(jobId, ct);
                return result is null ? Results.NoContent() : Results.Ok(result);
            })
            .RequireAuthorization();

        jobs.MapGet("/{jobId:guid}/artifacts", async (Guid jobId, JobService service, CancellationToken ct) =>
                Results.Ok(await service.ListArtifactsAsync(jobId, ct)))
            .RequireAuthorization();

        jobs.MapGet("/{jobId:guid}/artifacts/{artifactId:guid}/link", async (Guid jobId, Guid artifactId, JobService service, ArtifactLinkService links, CancellationToken ct) =>
                Results.Ok(await service.GetArtifactLinkAsync(jobId, artifactId, links, ct)))
            .RequireAuthorization();

        jobs.MapGet("/{jobId:guid}/decisions", async (Guid jobId, JobService service, CancellationToken ct) =>
                Results.Ok(await service.ListDecisionsAsync(jobId, ct)))
            .RequireAuthorization();

        jobs.MapGet("/{jobId:guid}/audit", async (Guid jobId, int? limit, JobService service, CancellationToken ct) =>
                Results.Ok(await service.ListAuditAsync(jobId, limit ?? 100, ct)))
            .RequireAuthorization();

        jobs.MapGet("/{jobId:guid}/documents/{documentId:guid}/content", async (Guid jobId, Guid documentId, IJobStore store, IArtifactStorage storage, CancellationToken ct) =>
            {
                var documents = await store.ListDocumentsAsync(jobId, ct);
                var document = documents.FirstOrDefault(item => item.DocumentId == documentId)
                    ?? throw new ApiException(404, "Документ не найден в составе задания.");
                var content = storage.Read(document.StoragePath)
                    ?? throw new ApiException(404, "Файл документа отсутствует в хранилище.");
                return Results.File(content, document.ContentType, document.FileName);
            })
            .RequireAuthorization(Policies.JobDocuments);

        jobs.MapPost("/{jobId:guid}/result", SubmitResultAsync)
            .RequireAuthorization(Policies.Service)
            .DisableAntiforgery();

        jobs.MapPost("/{jobId:guid}/steps", async (Guid jobId, StepReportDto report, HttpContext context, JobService service, CancellationToken ct) =>
            {
                await service.ReportStepAsync(jobId, report, context.User.ToUserContext(), ct);
                return Results.Accepted();
            })
            .RequireAuthorization(Policies.Service)
            .DisableAntiforgery();

        return app;
    }

    private static async Task<IResult> CreateJobAsync(HttpContext context, JobService service, GatewayOptions options, CancellationToken ct)
    {
        if (!context.Request.HasFormContentType)
        {
            throw new ApiException(415, "Ожидается multipart/form-data: файлы комплекта и поля source, mailbox, message_id.");
        }

        var form = await context.Request.ReadFormAsync(ct);
        if (form.Files.Count == 0)
        {
            throw new ApiException(400, "Комплект не принят: файлы не переданы.");
        }

        var files = new List<JobService.IncomingFile>(form.Files.Count);
        long total = 0;
        foreach (var file in form.Files)
        {
            if (file.Length > options.MaxUploadBytes)
            {
                throw new ApiException(413, $"Файл «{file.FileName}» больше допустимого размера.");
            }

            total += file.Length;
            using var buffer = new MemoryStream();
            await file.CopyToAsync(buffer, ct);
            files.Add(new JobService.IncomingFile(
                file.Name,
                string.IsNullOrWhiteSpace(file.FileName) ? "document.bin" : file.FileName!,
                string.IsNullOrWhiteSpace(file.ContentType) ? "application/octet-stream" : file.ContentType!,
                buffer.ToArray()));
        }

        if (total > options.MaxUploadBytes * 4)
        {
            throw new ApiException(413, "Комплект превышает допустимый суммарный размер.");
        }

        var input = new JobService.CreateJobInput(
            form["source"].ToString(),
            form["mailbox"].ToString(),
            form["message_id"].ToString(),
            form["profile_ref"].ToString(),
            form["telegram_chat_id"].ToString(),
            files);

        var outcome = await service.CreateJobAsync(input, context.User.ToUserContext(), ct);
        if (!outcome.Created)
        {
            context.Response.Headers["X-Idempotent-Replay"] = "true";
            return Results.Ok(outcome.Job);
        }

        return Results.Created($"/api/jobs/{outcome.Job.JobId}", outcome.Job);
    }

    private static async Task<IResult> SubmitResultAsync(Guid jobId, HttpContext context, JobService service, CancellationToken ct)
    {
        if (!context.Request.HasFormContentType)
        {
            throw new ApiException(415, "Ожидается multipart/form-data: часть result и файлы артефактов.");
        }

        var form = await context.Request.ReadFormAsync(ct);
        var payload = form["result"].ToString();
        if (string.IsNullOrWhiteSpace(payload))
        {
            // Часть result допускается и как вложенный файл: клиентам проще передать JSON файлом в UTF-8.
            var jsonPart = form.Files.FirstOrDefault(file => string.Equals(file.Name, "result", StringComparison.OrdinalIgnoreCase));
            if (jsonPart is not null)
            {
                using var reader = new StreamReader(jsonPart.OpenReadStream(), Encoding.UTF8);
                payload = await reader.ReadToEndAsync(ct);
            }
        }

        if (string.IsNullOrWhiteSpace(payload))
        {
            throw new ApiException(400, "Часть result с описанием результата не передана.");
        }

        ResultSubmissionDto? submission;
        try
        {
            submission = JsonSerializer.Deserialize<ResultSubmissionDto>(payload, Json);
        }
        catch (JsonException exception)
        {
            throw new ApiException(400, "Часть result не является корректным JSON: " + exception.Message);
        }

        if (submission is null)
        {
            throw new ApiException(400, "Часть result пуста.");
        }

        var files = new List<JobService.IncomingFile>(form.Files.Count);
        foreach (var file in form.Files)
        {
            if (string.Equals(file.Name, "result", StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            using var buffer = new MemoryStream();
            await file.CopyToAsync(buffer, ct);
            files.Add(new JobService.IncomingFile(
                file.Name,
                string.IsNullOrWhiteSpace(file.FileName) ? "artifact.bin" : file.FileName!,
                string.IsNullOrWhiteSpace(file.ContentType) ? "application/octet-stream" : file.ContentType!,
                buffer.ToArray()));
        }

        var job = await service.SubmitResultAsync(jobId, submission, files, context.User.ToUserContext(), ct);
        return Results.Ok(job);
    }
}
