using System.Text.Json;

namespace TenderHive.Gateway.Services;

/// <summary>
/// Единый контракт сериализации: имена полей в snake_case (<c>job_id</c>, <c>reason_code</c>),
/// чтобы клиенты на Python и C# работали с одной и той же схемой.
/// </summary>
public static class JsonContract
{
    public static readonly JsonSerializerOptions Options = new(JsonSerializerDefaults.Web)
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true
    };
}
