using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.AspNetCore.Http.Features;
using TenderHive.Contracts;
using TenderHive.Gateway.Services;

namespace TenderHive.Gateway.Endpoints;

public static class DemoRunEndpoints
{
    // Workbench accepts 5 MiB of document bytes plus JSON metadata and base64 overhead.
    private const long MaxBodySize = 4L * ((5 * 1024 * 1024 + 2) / 3) + 32 * 1024;

    private static readonly JsonSerializerOptions RequestJson = new(JsonContract.Options)
    {
        RespectRequiredConstructorParameters = true,
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow
    };

    public static void MapDemoRunEndpoints(this IEndpointRouteBuilder app)
    {
        var runs = app.MapGroup("/api/demo/runs").RequireAuthorization();
        runs.AddEndpointFilter(async (context, next) =>
        {
            context.HttpContext.Response.Headers.CacheControl = "no-store";
            return await next(context);
        });

        runs.MapPost("", CreateAsync);
        runs.MapGet("", async (HttpContext context, DemoRuns service, CancellationToken ct) =>
            Results.Ok(await service.ListAsync(context.User.ToUserContext(), ct)));

        runs.MapGet("/{runId:guid}", async (Guid runId, HttpContext context, DemoRuns service, CancellationToken ct) =>
            Results.Ok(await service.GetAsync(runId, context.User.ToUserContext(), ct)));

        runs.MapPost("/{runId:guid}/decision", async (Guid runId, HttpContext context, DemoRuns service, CancellationToken ct) =>
        {
            var decision = await ReadAsync<DemoDecisionInput>(context, 4096, ct);
            return Results.Accepted(value: await service.DecideAsync(runId, decision, context.User.ToUserContext(), ct));
        });

        runs.MapPost("/{runId:guid}/extraction/confirm", async (Guid runId, HttpContext context, DemoRuns service, CancellationToken ct) =>
        {
            var input = await ReadAsync<DemoExtractionConfirmation>(context, MaxBodySize, ct);
            return Results.Accepted(value: await service.ConfirmExtractionAsync(runId, input, context.User.ToUserContext(), ct));
        });

        runs.MapPost("/{runId:guid}/extraction/retry", async (Guid runId, HttpContext context, DemoRuns service, CancellationToken ct) =>
            Results.Accepted(value: await service.RetryExtractionAsync(runId, context.User.ToUserContext(), ct)));

        runs.MapGet("/{runId:guid}/artifacts/{name}", async (Guid runId, string name, HttpContext context, DemoRuns service, CancellationToken ct) =>
        {
            var download = await service.DownloadAsync(runId, name, context.User.ToUserContext(), ct);
            context.Response.Headers["X-Artifact-SHA256"] = download.Sha256;
            return Results.File(download.Bytes, download.ContentType, name);
        });
    }

    private static async Task<IResult> CreateAsync(HttpContext context, DemoRuns service, CancellationToken ct)
    {
        var input = await ReadAsync<DemoRunInput>(context, MaxBodySize, ct);
        var run = await service.StartAsync(input, context.User.ToUserContext(), ct);
        return Results.Accepted($"/api/demo/runs/{run.Id:D}", run);
    }

    private static async Task<T> ReadAsync<T>(HttpContext context, long limit, CancellationToken ct) where T : class
    {
        if (!context.Request.HasJsonContentType())
        {
            throw new ApiException(415, "Поддерживается тип запроса application/json.");
        }
        if (context.Request.ContentLength > limit)
        {
            throw new ApiException(413, "Размер запроса превышает лимит.");
        }
        if (context.Features.Get<IHttpMaxRequestBodySizeFeature>() is { IsReadOnly: false } bodyLimit)
        {
            bodyLimit.MaxRequestBodySize = Math.Min(bodyLimit.MaxRequestBodySize ?? limit, limit);
        }

        try
        {
            return await context.Request.ReadFromJsonAsync<T>(RequestJson, ct)
                ?? throw new ApiException(422, "Укажите данные для прогона.");
        }
        catch (BadHttpRequestException error) when (error.StatusCode == 413)
        {
            throw new ApiException(413, "Размер запроса превышает лимит.");
        }
        catch (JsonException)
        {
            throw new ApiException(422, "Проверьте поля JSON-запроса.");
        }
    }
}
