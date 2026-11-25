using System.Security.Cryptography;
using System.Text;
using TenderHive.Gateway.Configuration;

namespace TenderHive.Gateway.Services;

/// <summary>
/// Короткоживущая подписанная ссылка на файл артефакта: дашборд не обращается к базе,
/// а браузер скачивает файл по ссылке, не передавая токен в адресе.
/// </summary>
public sealed class ArtifactLinkService
{
    private readonly GatewayOptions _options;

    public ArtifactLinkService(GatewayOptions options)
    {
        _options = options;
    }

    public string Create(Guid artifactId, out DateTimeOffset expiresAt)
    {
        expiresAt = DateTimeOffset.UtcNow.AddMinutes(_options.ArtifactLinkTtlMinutes);
        var expiresUnix = expiresAt.ToUnixTimeSeconds();
        var signature = Sign(artifactId, expiresUnix);
        return $"{_options.GatewayPublicUrl}/api/artifacts/{artifactId}?exp={expiresUnix}&sig={signature}";
    }

    public bool Validate(Guid artifactId, long expiresUnix, string signature)
    {
        if (DateTimeOffset.UtcNow.ToUnixTimeSeconds() > expiresUnix)
        {
            return false;
        }

        var expected = Encoding.UTF8.GetBytes(Sign(artifactId, expiresUnix));
        var actual = Encoding.UTF8.GetBytes(signature);
        return expected.Length == actual.Length && CryptographicOperations.FixedTimeEquals(expected, actual);
    }

    private string Sign(Guid artifactId, long expiresUnix)
    {
        using var hmac = new HMACSHA256(Encoding.UTF8.GetBytes(_options.JwtSecret));
        var hash = hmac.ComputeHash(Encoding.UTF8.GetBytes($"{artifactId:N}:{expiresUnix}"));
        return Convert.ToBase64String(hash).TrimEnd('=').Replace('+', '-').Replace('/', '_');
    }
}
