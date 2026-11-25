using Microsoft.AspNetCore.Components;
using TenderHive.Web.Services;

namespace TenderHive.Web.Components.Pages;

public partial class Login
{
    [Inject] private GatewayClient Gateway { get; set; } = default!;
    [Inject] private SessionState Session { get; set; } = default!;
    [Inject] private NavigationManager Navigation { get; set; } = default!;

    private string username = "specialist";
    private string password = string.Empty;
    private string? error;
    private bool busy;

    [SupplyParameterFromQuery] public string? ReturnUrl { get; set; }

    private string Destination => ReturnUrl is { } path && IsLocalPath(path) ? path : "/";

    private static bool IsLocalPath(string path) =>
        path.StartsWith('/') && !path.StartsWith("//", StringComparison.Ordinal) &&
        !path.Contains('\\') && !path.Any(char.IsControl) &&
        !path.StartsWith("/login", StringComparison.OrdinalIgnoreCase);

    protected override void OnInitialized()
    {
        if (Session.IsAuthenticated)
        {
            Navigation.NavigateTo(Destination);
        }
    }

    private async Task SignInAsync()
    {
        if (busy)
        {
            return;
        }

        busy = true;
        error = null;
        try
        {
            var response = await Gateway.LoginAsync(username.Trim(), password);
            await Session.SignInAsync(response);
            Navigation.NavigateTo(Destination);
        }
        catch (GatewayException exception)
        {
            error = exception.Message;
        }
        catch (HttpRequestException)
        {
            error = "Шлюз недоступен: проверьте переменную GATEWAY_BASE_URL.";
        }
        catch (TaskCanceledException)
        {
            error = "Шлюз не ответил за отведённое время.";
        }
        finally
        {
            busy = false;
        }
    }
}
