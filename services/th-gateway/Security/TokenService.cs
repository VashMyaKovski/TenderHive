using System.Security.Claims;
using System.Text;
using Microsoft.IdentityModel.JsonWebTokens;
using Microsoft.IdentityModel.Tokens;
using TenderHive.Gateway.Configuration;

namespace TenderHive.Gateway.Security;

/// <summary>Выпуск токенов доступа. Проверка токена выполняется штатным JwtBearer.</summary>
public sealed class TokenService
{
    private readonly GatewayOptions _options;
    private readonly JsonWebTokenHandler _handler = new();

    public TokenService(GatewayOptions options)
    {
        _options = options;
    }

    public (string Token, DateTimeOffset ExpiresAt) Issue(string username, string displayName, string role)
    {
        var expiresAt = DateTimeOffset.UtcNow.AddHours(_options.JwtTtlHours);
        var key = new SymmetricSecurityKey(Encoding.UTF8.GetBytes(_options.JwtSecret));
        var descriptor = new SecurityTokenDescriptor
        {
            Issuer = TokenIssuer,
            Audience = TokenAudience,
            Expires = expiresAt.UtcDateTime,
            IssuedAt = DateTime.UtcNow,
            SigningCredentials = new SigningCredentials(key, SecurityAlgorithms.HmacSha256),
            Subject = new ClaimsIdentity(new[]
            {
                new Claim(ClaimTypes.Name, username),
                new Claim(ClaimTypes.Role, role),
                new Claim("display_name", displayName)
            })
        };

        return (_handler.CreateToken(descriptor), expiresAt);
    }

    public static string TokenIssuer => "th-gateway";

    public static string TokenAudience => "tenderhive";

    public static SymmetricSecurityKey CreateSigningKey(GatewayOptions options)
    {
        return new SymmetricSecurityKey(Encoding.UTF8.GetBytes(options.JwtSecret));
    }
}
