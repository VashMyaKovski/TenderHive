using System.Security.Claims;
using TenderHive.Contracts;

namespace TenderHive.Gateway.Services;

public sealed record UserContext(string Username, string Role);

public static class UserContextExtensions
{
    public static UserContext ToUserContext(this ClaimsPrincipal principal)
    {
        var username = principal.FindFirstValue(ClaimTypes.Name) ?? "unknown";
        var role = principal.FindFirstValue(ClaimTypes.Role) ?? string.Empty;
        return new UserContext(username, role);
    }
}
