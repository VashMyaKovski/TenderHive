using TenderHive.Contracts;
using TenderHive.Gateway.Configuration;
using TenderHive.Gateway.Services;
using TenderHive.Gateway.Storage;

namespace TenderHive.Gateway.Endpoints;

public static class HealthEndpoints
{
    public static IEndpointRouteBuilder MapHealthEndpoints(this IEndpointRouteBuilder app)
    {
        app.MapGet("/api/health", (GatewayOptions options, IJobStore store) =>
                Results.Ok(new HealthDto(
                    "ok",
                    store.Name,
                    options.KafkaEnabled ? "kafka" : "journal",
                    options.TelegramEnabled ? "enabled" : "disabled")))
            .AllowAnonymous();

        app.MapGet("/api/artifacts/{artifactId:guid}", async (Guid artifactId, long exp, string sig, ArtifactLinkService links, IJobStore store, IArtifactStorage storage, CancellationToken ct) =>
            {
                if (!links.Validate(artifactId, exp, sig))
                {
                    throw new ApiException(403, "Ссылка на файл недействительна или истекла: запросите новую в дашборде.");
                }

                var artifact = await store.GetArtifactAsync(artifactId, ct)
                    ?? throw new ApiException(404, "Артефакт не найден.");
                var content = storage.Read(artifact.StoragePath)
                    ?? throw new ApiException(404, "Файл артефакта отсутствует в хранилище.");

                return Results.File(content, artifact.ContentType, artifact.FileName);
            })
            .AllowAnonymous();

        return app;
    }
}
