using Microsoft.AspNetCore.Components.Server.ProtectedBrowserStorage;
using Microsoft.JSInterop;
using TenderHive.Contracts;

namespace TenderHive.Web.Services;

public sealed class SessionState(ProtectedSessionStorage storage)
{
    private const string SessionKey = "tenderhive-login";
    private LoginResponse? _current;

    public string? Token => _current?.Token;
    public string? Username => _current?.Username;
    public string? DisplayName => _current?.DisplayName;
    public string? Role => _current?.Role;
    public bool IsAuthenticated => _current is { ExpiresAt: var expiry } && expiry > DateTimeOffset.UtcNow;

    public async Task RestoreAsync()
    {
        try
        {
            var saved = await storage.GetAsync<LoginResponse>(SessionKey);
            _current = saved.Success ? saved.Value : null;
            if (!IsAuthenticated && _current is not null)
            {
                await SignOutAsync();
            }
        }
        catch (System.Security.Cryptography.CryptographicException)
        {
            await SignOutAsync();
        }
        catch (JSException)
        {
            // Authentication can still be established for this circuit if session storage is unavailable.
            _current = null;
        }
    }

    public async Task SignInAsync(LoginResponse response)
    {
        _current = response;
        try
        {
            await storage.SetAsync(SessionKey, response);
        }
        catch (JSException)
        {
            // Leave the current circuit signed in when session storage is unavailable.
        }
    }

    public async Task SignOutAsync()
    {
        _current = null;
        try
        {
            await storage.DeleteAsync(SessionKey);
        }
        catch (JSException)
        {
            // The current circuit is already signed out.
        }
    }

    public bool IsInRole(string role) => string.Equals(Role, role, StringComparison.Ordinal);

    public bool IsAnyRole(params string[] roles) => Role is not null && roles.Contains(Role, StringComparer.Ordinal);
}
