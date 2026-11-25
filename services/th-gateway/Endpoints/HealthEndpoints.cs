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

        app.MapGet("/api/artifacts/{artifactId:guid}", async (Guid artifactId, long exp, string sig, string? format, ArtifactLinkService links, ArtifactExporter exporter, IJobStore store, IArtifactStorage storage, CancellationToken ct) =>
            {
                if (!ArtifactFormats.TryNormalize(format, out var normalized))
                {
                    throw new ApiException(400, "Допустимые форматы выдачи артефакта: original, docx, pdf, xlsx.");
                }

                if (!links.Validate(artifactId, exp, normalized, sig))
                {
                    throw new ApiException(403, "Ссылка на файл недействительна или истекла: запросите новую в дашборде.");
                }

                var artifact = await store.GetArtifactAsync(artifactId, ct)
                    ?? throw new ApiException(404, "Артефакт не найден.");
                var content = storage.Read(artifact.StoragePath)
                    ?? throw new ApiException(404, "Файл артефакта отсутствует в хранилище.");

                if (normalized == ArtifactFormats.Original || !ArtifactFormats.TryParse(normalized, out var target))
                {
                    return Results.File(content, artifact.ContentType, artifact.FileName);
                }

                var export = exporter.Export(artifact, content, target);
                return Results.File(export.Content, export.ContentType, export.FileName);
            })
            .AllowAnonymous();

        return app;
    }
}
