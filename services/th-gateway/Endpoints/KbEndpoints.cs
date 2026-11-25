using TenderHive.Contracts;
using TenderHive.Gateway.Services;
using TenderHive.Gateway.Storage;

namespace TenderHive.Gateway.Endpoints;

public static class KbEndpoints
{
    public static IEndpointRouteBuilder MapKbEndpoints(this IEndpointRouteBuilder app)
    {
        app.MapGet("/api/kb/versions", async (IJobStore store, CancellationToken ct) =>
            {
                var versions = await store.ListKbVersionsAsync(ct);
                var active = versions.FirstOrDefault(version => version.Status == "active");
                return Results.Ok(new
                {
                    active_version = active?.Version,
                    versions = versions.Select(version => new KbVersionDto(
                        version.Version,
                        version.Status,
                        version.CreatedAt,
                        version.ActivatedAt,
                        version.ActivatedBy,
                        version.RulesCount,
                        version.Notes))
                });
            })
            .RequireAuthorization();

        app.MapGet("/api/kb/activations", async (int? limit, IJobStore store, CancellationToken ct) =>
            {
                var activations = await store.ListKbActivationsAsync(limit ?? 50, ct);
                return Results.Ok(activations.Select(item => new KbActivationDto(item.Version, item.Action, item.Actor, item.OccurredAt, item.Comment)));
            })
            .RequireAuthorization();

        app.MapPost("/api/kb/versions/{version}/activate", async (string version, KbActivateRequest? request, HttpContext context, IJobStore store, CancellationToken ct) =>
            {
                var actor = context.User.ToUserContext();
                var activated = await store.ActivateKbVersionAsync(version, actor.Username, request?.Comment, ct);
                if (!activated)
                {
                    throw new ApiException(404, $"Версия базы знаний «{version}» не найдена.");
                }

                await store.WriteAuditAsync(new Domain.AuditRecord
                {
                    Actor = actor.Username,
                    Role = actor.Role,
                    Action = "kb.activate",
                    Details = $"version={version}, comment={request?.Comment}"
                }, ct);

                return Results.Ok(new { version, status = "active" });
            })
            .RequireAuthorization(Policies.KbAdmin)
            .DisableAntiforgery();

        return app;
    }
}
