using System.Security.Claims;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.IdentityModel.Tokens;
using TenderHive.Contracts;
using TenderHive.Gateway.Configuration;
using TenderHive.Gateway.Endpoints;
using TenderHive.Gateway.Hitl;
using TenderHive.Gateway.Outbox;
using TenderHive.Gateway.Security;
using TenderHive.Gateway.Services;
using TenderHive.Gateway.Storage;
using TenderHive.Gateway.Telegram;

EnvFile.Load(AppContext.BaseDirectory, Directory.GetCurrentDirectory());

var builder = WebApplication.CreateBuilder(args);
builder.Configuration.AddEnvironmentVariables();

var options = GatewayOptions.FromConfiguration(builder.Configuration);
var workbench = DemoWorkbenchOptions.FromConfiguration(builder.Configuration);

builder.Services.AddSingleton(options);
builder.Services.AddHttpClient();
if (workbench is not null)
{
    builder.Services.AddHttpClient(DemoWorkbenchClient.ClientName, client =>
    {
        client.BaseAddress = workbench.Origin;
        client.DefaultRequestHeaders.Add("X-Workbench-Token", workbench.Token);
        client.MaxResponseContentBufferSize = DemoWorkbenchClient.MaxResponseBytes;
    }).ConfigurePrimaryHttpMessageHandler(() => new HttpClientHandler { AllowAutoRedirect = false });
    builder.Services.AddSingleton(new DemoRunRegistry(workbench.GrantsFile));
    builder.Services.AddScoped(services => new DemoWorkbenchClient(
        services.GetRequiredService<IHttpClientFactory>().CreateClient(DemoWorkbenchClient.ClientName)));
    builder.Services.AddScoped<DemoRuns>();
}
builder.Services.ConfigureHttpJsonOptions(http =>
{
    http.SerializerOptions.PropertyNamingPolicy = System.Text.Json.JsonNamingPolicy.SnakeCaseLower;
    http.SerializerOptions.PropertyNameCaseInsensitive = true;
});
builder.Services.AddSingleton<IArtifactStorage>(_ => new FileArtifactStorage(options.ArtifactBucket));
builder.Services.AddSingleton<IJobStore>(_ => StoreFactory.Create(options));
builder.Services.AddSingleton<PasswordHasher>();
builder.Services.AddSingleton<TokenService>();
builder.Services.AddSingleton<ArtifactLinkService>();
builder.Services.AddSingleton<ArtifactExporter>();
builder.Services.AddSingleton<JobService>();
builder.Services.AddSingleton<ITelegramClient, TelegramClient>();

builder.Services.AddHostedService<OutboxDispatcher>();
builder.Services.AddHostedService<DecisionTimeoutService>();
builder.Services.AddHostedService<TelegramBotService>();
builder.Services.AddHostedService<ArtifactNotifier>();

builder.Services
    .AddAuthentication(JwtBearerDefaults.AuthenticationScheme)
    .AddJwtBearer(jwt =>
    {
        jwt.MapInboundClaims = false;
        jwt.TokenValidationParameters = new TokenValidationParameters
        {
            ValidateIssuer = true,
            ValidIssuer = TokenService.TokenIssuer,
            ValidateAudience = true,
            ValidAudience = TokenService.TokenAudience,
            ValidateIssuerSigningKey = true,
            IssuerSigningKey = TokenService.CreateSigningKey(options),
            ValidateLifetime = true,
            ClockSkew = TimeSpan.FromSeconds(30),
            RoleClaimType = ClaimTypes.Role,
            NameClaimType = ClaimTypes.Name
        };
    });

builder.Services.AddAuthorization(authorization =>
{
    authorization.AddPolicy(Policies.Service, policy => policy.RequireRole(AppRole.Service));
    authorization.AddPolicy(Policies.JobCreate, policy => policy.RequireRole(
        AppRole.TenderSpecialist,
        AppRole.Procurement,
        AppRole.DepartmentHead,
        AppRole.Service));
    authorization.AddPolicy(Policies.JobDocuments, policy => policy.RequireRole(
        AppRole.Service,
        AppRole.TenderSpecialist,
        AppRole.DepartmentHead));
    authorization.AddPolicy(Policies.KbAdmin, policy => policy.RequireRole(AppRole.KbAdmin));
});

var app = builder.Build();

app.Use(async (context, next) =>
{
    try
    {
        await next();
    }
    catch (ApiException exception)
    {
        if (context.Response.HasStarted)
        {
            throw;
        }

        context.Response.Clear();
        context.Response.StatusCode = exception.StatusCode;
        await context.Response.WriteAsJsonAsync(
            new { error = exception.Message, status = exception.StatusCode },
            context.RequestAborted);
    }
});

app.UseAuthentication();
app.UseAuthorization();

app.MapAuthEndpoints();
app.MapJobEndpoints();
app.MapDecisionEndpoints();
app.MapKbEndpoints();
app.MapHealthEndpoints();
if (workbench is not null)
{
    app.MapDemoRunEndpoints();
}

var store = app.Services.GetRequiredService<IJobStore>();
await store.InitializeAsync(CancellationToken.None);
await Seeder.SeedAsync(
    store,
    options,
    app.Services.GetRequiredService<PasswordHasher>(),
    app.Services.GetRequiredService<ILoggerFactory>().CreateLogger("Seeder"),
    CancellationToken.None);

app.Logger.LogInformation(
    "th-gateway запущен: хранилище {Store}, Kafka {Kafka}, Telegram {Telegram}, срок согласования {Timeout} ч",
    store.Name,
    options.KafkaEnabled ? "включена" : "выключена (журнал)",
    options.TelegramEnabled ? "включён" : "выключен",
    options.HitlTimeoutHours);

app.Run();
