using System.Security.Cryptography;
using System.Text;
using TenderHive.Gateway.Configuration;

namespace TenderHive.Gateway.Services;

/// <summary>
/// Короткоживущая подписанная ссылка на файл артефакта: дашборд не обращается к базе,
/// а браузер скачивает файл по ссылке, не передавая токен в адресе. Формат выдачи
/// входит в подпись, поэтому подменить его в адресе нельзя.
/// </summary>
public sealed class ArtifactLinkService
{
    private readonly GatewayOptions _options;

    public ArtifactLinkService(GatewayOptions options)
    {
        _options = options;
    }

    /// <param name="format">Нормализованный формат выдачи: <c>original</c>, <c>docx</c>, <c>pdf</c> или <c>xlsx</c>.</param>
    public string Create(Guid artifactId, string format, out DateTimeOffset expiresAt)
    {
        var normalized = string.IsNullOrWhiteSpace(format) ? ArtifactFormats.Original : format;
        expiresAt = DateTimeOffset.UtcNow.AddMinutes(_options.ArtifactLinkTtlMinutes);
        var expiresUnix = expiresAt.ToUnixTimeSeconds();
        var signature = Sign(artifactId, expiresUnix, normalized);
        return $"{_options.GatewayPublicUrl}/api/artifacts/{artifactId}?exp={expiresUnix}&sig={signature}&format={normalized}";
    }

    public bool Validate(Guid artifactId, long expiresUnix, string format, string signature)
    {
        if (DateTimeOffset.UtcNow.ToUnixTimeSeconds() > expiresUnix)
        {
            return false;
        }

        var expected = Encoding.UTF8.GetBytes(Sign(artifactId, expiresUnix, format));
        var actual = Encoding.UTF8.GetBytes(signature);
        return expected.Length == actual.Length && CryptographicOperations.FixedTimeEquals(expected, actual);
    }

    private string Sign(Guid artifactId, long expiresUnix, string format)
    {
        using var hmac = new HMACSHA256(Encoding.UTF8.GetBytes(_options.JwtSecret));
        var hash = hmac.ComputeHash(Encoding.UTF8.GetBytes($"{artifactId:N}:{expiresUnix}:{format}"));
        return Convert.ToBase64String(hash).TrimEnd('=').Replace('+', '-').Replace('/', '_');
    }
}
