using TenderHive.Web;
using TenderHive.Web.Components;
using TenderHive.Web.Services;

EnvFile.Load(AppContext.BaseDirectory, Directory.GetCurrentDirectory());

var builder = WebApplication.CreateBuilder(args);
builder.Configuration.AddEnvironmentVariables();

builder.Services.AddRazorComponents()
    .AddInteractiveServerComponents();

builder.Services.AddHttpClient();
builder.Services.AddScoped<SessionState>();
builder.Services.AddScoped<GatewayClient>();

var app = builder.Build();

app.MapStaticAssets();
app.UseAntiforgery();

app.MapRazorComponents<App>()
    .AddInteractiveServerRenderMode();

app.Run();
