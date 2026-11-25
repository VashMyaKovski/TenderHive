using System.Security.Cryptography;
using System.Text;

namespace TenderHive.Gateway.Services;

/// <summary>
/// Идемпотентный ключ комплекта: <c>sha256(ящик + Message-ID + sha256(файлов))</c>.
/// Повторная загрузка того же комплекта даёт тот же ключ и не создаёт новое задание.
/// </summary>
public static class IdempotencyKey
{
    public static string Create(string source, string mailbox, string messageId, IEnumerable<string> fileHashes)
    {
        var material = new StringBuilder();
        material.Append("source:").Append(source).Append('\n');
        material.Append("mailbox:").Append(mailbox).Append('\n');
        material.Append("message:").Append(messageId).Append('\n');
        foreach (var hash in fileHashes.OrderBy(h => h, StringComparer.Ordinal))
        {
            material.Append("file:").Append(hash.ToLowerInvariant()).Append('\n');
        }

        return Sha256Hex(Encoding.UTF8.GetBytes(material.ToString()));
    }

    public static string Sha256Hex(byte[] data) => Convert.ToHexString(SHA256.HashData(data)).ToLowerInvariant();
}
