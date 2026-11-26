using TenderHive.Web;
using TenderHive.Web.Components;
using TenderHive.Web.Services;

EnvFile.Load(AppContext.BaseDirectory, Directory.GetCurrentDirectory());

var builder = WebApplication.CreateBuilder(args);
builder.Configuration.AddEnvironmentVariables();

builder.Services.AddRazorComponents()
    .AddInteractiveServerComponents();

builder.Services.AddHttpClient();
builder.Services.AddScoped<Microsoft.AspNetCore.Components.Server.ProtectedBrowserStorage.ProtectedSessionStorage>();
builder.Services.AddScoped<SessionState>();
builder.Services.AddScoped<GatewayClient>();

var app = builder.Build();

app.MapStaticAssets();
app.UseAntiforgery();
app.MapGet("/healthz", async (IConfiguration configuration, IHttpClientFactory clients, CancellationToken ct) =>
{
    if (!configuration.GetValue<bool>("DEMO_DASHBOARD_ENABLED"))
    {
        return Results.Ok(new { status = "ok" });
    }

    if (!Uri.TryCreate(configuration["GATEWAY_BASE_URL"]?.TrimEnd('/') + "/api/health", UriKind.Absolute, out var gatewayHealth)
        || gatewayHealth.Scheme is not ("http" or "https"))
    {
        return Results.StatusCode(StatusCodes.Status503ServiceUnavailable);
    }

    using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
    timeout.CancelAfter(TimeSpan.FromMilliseconds(1500));
    using var client = clients.CreateClient("gateway-health");
    try
    {
        using var response = await client.GetAsync(gatewayHealth, HttpCompletionOption.ResponseHeadersRead, timeout.Token);
        return response.IsSuccessStatusCode
            ? Results.Ok(new { status = "ok" })
            : Results.StatusCode(StatusCodes.Status503ServiceUnavailable);
    }
    catch (Exception exception) when (exception is HttpRequestException or OperationCanceledException)
    {
        return Results.StatusCode(StatusCodes.Status503ServiceUnavailable);
    }
});

app.MapRazorComponents<App>()
    .AddInteractiveServerRenderMode();

app.Run();
