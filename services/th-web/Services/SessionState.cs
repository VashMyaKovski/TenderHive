using TenderHive.Contracts;

namespace TenderHive.Web.Services;

/// <summary>Состояние входа пользователя в пределах схемы Blazor Server.</summary>
public sealed class SessionState
{
    public string? Token { get; private set; }

    public string? Username { get; private set; }

    public string? DisplayName { get; private set; }

    public string? Role { get; private set; }

    public DateTimeOffset? ExpiresAt { get; private set; }

    public bool IsAuthenticated => Token is not null && ExpiresAt > DateTimeOffset.UtcNow;

    public void SignIn(LoginResponse response)
    {
        Token = response.Token;
        Username = response.Username;
        DisplayName = response.DisplayName;
        Role = response.Role;
        ExpiresAt = response.ExpiresAt;
    }

    public void SignOut()
    {
        Token = null;
        Username = null;
        DisplayName = null;
        Role = null;
        ExpiresAt = null;
    }

    public bool IsInRole(string role) => string.Equals(Role, role, StringComparison.Ordinal);

    public bool IsAnyRole(params string[] roles) => Role is not null && roles.Contains(Role, StringComparer.Ordinal);
}
