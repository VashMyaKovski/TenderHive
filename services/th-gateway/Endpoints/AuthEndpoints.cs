using System.Security.Cryptography;
using System.Text;
using TenderHive.Contracts;
using TenderHive.Gateway.Configuration;
using TenderHive.Gateway.Security;
using TenderHive.Gateway.Services;
using TenderHive.Gateway.Storage;

namespace TenderHive.Gateway.Endpoints;

public static class AuthEndpoints
{
    public static IEndpointRouteBuilder MapAuthEndpoints(this IEndpointRouteBuilder app)
    {
        app.MapPost("/api/auth/login", async (LoginRequest request, IJobStore store, PasswordHasher hasher, TokenService tokens, CancellationToken ct) =>
            {
                if (string.IsNullOrWhiteSpace(request.Username) || string.IsNullOrWhiteSpace(request.Password))
                {
                    throw new ApiException(400, "Не указан логин или пароль.");
                }

                var user = await store.FindUserAsync(request.Username.Trim(), ct);
                if (user is null || !hasher.Verify(request.Password, user.PasswordHash))
                {
                    throw new ApiException(401, "Неверный логин или пароль.");
                }

                var (token, expiresAt) = tokens.Issue(user.Username, user.DisplayName, user.Role);
                return Results.Ok(new LoginResponse(token, user.Username, user.DisplayName, user.Role, expiresAt));
            })
            .AllowAnonymous()
            .DisableAntiforgery();

        app.MapPost("/api/auth/service", (ServiceTokenRequest request, TokenService tokens, GatewayOptions options) =>
            {
                var provided = Encoding.UTF8.GetBytes(request.ApiKey ?? string.Empty);
                var expected = Encoding.UTF8.GetBytes(options.ServiceApiKey);
                if (provided.Length != expected.Length || !CryptographicOperations.FixedTimeEquals(provided, expected))
                {
                    throw new ApiException(401, "Неверный сервисный ключ.");
                }

                var (token, expiresAt) = tokens.Issue("th-orchestrator", "Оркестратор", AppRole.Service);
                return Results.Ok(new LoginResponse(token, "th-orchestrator", "Оркестратор", AppRole.Service, expiresAt));
            })
            .AllowAnonymous()
            .DisableAntiforgery();

        return app;
    }
}
