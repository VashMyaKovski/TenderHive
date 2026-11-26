using TenderHive.Contracts;
using TenderHive.Gateway.Services;

namespace TenderHive.Gateway.Endpoints;

public static class DecisionEndpoints
{
    public static IEndpointRouteBuilder MapDecisionEndpoints(this IEndpointRouteBuilder app)
    {
        var decisions = app.MapGroup("/api/decisions");

        decisions.MapGet("/", async (int? limit, JobService service, CancellationToken ct) =>
                Results.Ok(await service.ListOpenDecisionsAsync(limit ?? 100, ct)))
            .RequireAuthorization();

        decisions.MapPost("/{decisionId:guid}", async (Guid decisionId, DecisionRequest request, HttpContext context, JobService service, CancellationToken ct) =>
                Results.Ok(await service.DecideAsync(decisionId, request.Decision, request.Comment, context.User.ToUserContext(), ct)))
            .RequireAuthorization()
            .DisableAntiforgery();

        return app;
    }
}
