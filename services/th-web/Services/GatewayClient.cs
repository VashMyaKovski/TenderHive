using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json.Serialization;
using TenderHive.Contracts;

namespace TenderHive.Web.Services;

/// <summary>Ошибка шлюза: код и причина, переданная сервером.</summary>
public sealed class GatewayException : Exception
{
    public GatewayException(int statusCode, string message) : base(message)
    {
        StatusCode = statusCode;
    }

    public int StatusCode { get; }
}

public sealed record KbVersionsResponse(
    [property: JsonPropertyName("active_version")] string? ActiveVersion,
    [property: JsonPropertyName("versions")] IReadOnlyList<KbVersionDto> Versions);

/// <summary>
/// Клиент к API шлюза. Дашборд не обращается к базе напрямую: любая операция проходит
/// через шлюз, который проверяет роль и объект доступа.
/// </summary>
public sealed class GatewayClient
{
    private static readonly System.Text.Json.JsonSerializerOptions Json = new(System.Text.Json.JsonSerializerDefaults.Web)
    {
        PropertyNamingPolicy = System.Text.Json.JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true
    };

    private readonly HttpClient _http;
    private readonly SessionState _session;

    public GatewayClient(IHttpClientFactory factory, SessionState session, IConfiguration configuration)
    {
        _session = session;
        _http = factory.CreateClient(nameof(GatewayClient));

        var baseUrl = configuration["GATEWAY_BASE_URL"];
        if (string.IsNullOrWhiteSpace(baseUrl))
        {
            throw new InvalidOperationException("GATEWAY_BASE_URL не задан: дашборду нужен адрес API шлюза.");
        }

        _http.BaseAddress = new Uri(baseUrl.TrimEnd('/') + "/");
        _http.Timeout = TimeSpan.FromSeconds(60);
    }

    public Task<LoginResponse> LoginAsync(string username, string password) =>
        SendAsync<LoginResponse>(HttpMethod.Post, "api/auth/login", new LoginRequest(username, password));

    public Task<List<JobDto>> GetJobsAsync() =>
        SendAsync<List<JobDto>>(HttpMethod.Get, "api/jobs?limit=100", null);

    public Task<JobDetailDto> GetJobAsync(Guid jobId) =>
        SendAsync<JobDetailDto>(HttpMethod.Get, $"api/jobs/{jobId}", null);

    public Task<JobResultDto?> GetResultAsync(Guid jobId) =>
        SendNullableAsync<JobResultDto>(HttpMethod.Get, $"api/jobs/{jobId}/result");

    public Task<List<ArtifactDto>> GetArtifactsAsync(Guid jobId) =>
        SendAsync<List<ArtifactDto>>(HttpMethod.Get, $"api/jobs/{jobId}/artifacts", null);

    public Task<ArtifactLinkDto> GetArtifactLinkAsync(Guid jobId, Guid artifactId, string? format = null) =>
        SendAsync<ArtifactLinkDto>(
            HttpMethod.Get,
            $"api/jobs/{jobId}/artifacts/{artifactId}/link" + (string.IsNullOrWhiteSpace(format) ? string.Empty : $"?format={format}"),
            null);

    public Task<List<HumanDecisionDto>> GetDecisionsAsync(Guid jobId) =>
        SendAsync<List<HumanDecisionDto>>(HttpMethod.Get, $"api/jobs/{jobId}/decisions", null);

    public Task<List<HumanDecisionDto>> GetOpenDecisionsAsync() =>
        SendAsync<List<HumanDecisionDto>>(HttpMethod.Get, "api/decisions", null);

    public Task<DecisionResultDto> DecideAsync(Guid decisionId, string decision, string? comment) =>
        SendAsync<DecisionResultDto>(HttpMethod.Post, $"api/decisions/{decisionId}", new DecisionRequest(decision, comment));

    public Task<List<AuditEntryDto>> GetAuditAsync(Guid jobId) =>
        SendAsync<List<AuditEntryDto>>(HttpMethod.Get, $"api/jobs/{jobId}/audit", null);

    public Task<KbVersionsResponse> GetKbVersionsAsync() =>
        SendAsync<KbVersionsResponse>(HttpMethod.Get, "api/kb/versions", null);

    public Task<List<KbActivationDto>> GetKbActivationsAsync() =>
        SendAsync<List<KbActivationDto>>(HttpMethod.Get, "api/kb/activations", null);

    public Task ActivateKbVersionAsync(string version, string? comment) =>
        SendAsync<object>(HttpMethod.Post, $"api/kb/versions/{Uri.EscapeDataString(version)}/activate", new KbActivateRequest(comment));

    public Task<List<DemoRunDto>> ListDemoRunsAsync(CancellationToken ct) =>
        SendAsync<List<DemoRunDto>>(HttpMethod.Get, "api/demo/runs", null, ct);

    public Task<DemoRunDto> StartDemoRunAsync(DemoRunInput input, CancellationToken ct) =>
        SendAsync<DemoRunDto>(HttpMethod.Post, "api/demo/runs", input, ct);

    public Task<DemoRunDto> GetDemoRunAsync(Guid id, CancellationToken ct) =>
        SendAsync<DemoRunDto>(HttpMethod.Get, $"api/demo/runs/{id}", null, ct);

    public Task<DemoRunDto> DecideDemoRunAsync(Guid id, DemoDecisionInput input, CancellationToken ct) =>
        SendAsync<DemoRunDto>(HttpMethod.Post, $"api/demo/runs/{id}/decision", input, ct);

    public async Task<(byte[] Bytes, string ContentType)> DownloadDemoArtifactAsync(
        Guid id, string name, CancellationToken ct)
    {
        using var request = Build(HttpMethod.Get, $"api/demo/runs/{id}/artifacts/{Uri.EscapeDataString(name)}", null);
        using var response = await _http.SendAsync(request, ct);
        if (!response.IsSuccessStatusCode)
        {
            throw await CreateExceptionAsync(response, ct);
        }

        return (
            await response.Content.ReadAsByteArrayAsync(ct),
            response.Content.Headers.ContentType?.MediaType ?? "application/octet-stream");
    }


    private async Task<T> SendAsync<T>(HttpMethod method, string path, object? body, CancellationToken ct = default)
    {
        using var request = Build(method, path, body);
        using var response = await _http.SendAsync(request, ct);
        if (!response.IsSuccessStatusCode)
        {
            throw await CreateExceptionAsync(response, ct);
        }

        var payload = await response.Content.ReadFromJsonAsync<T>(Json, ct);
        return payload ?? throw new GatewayException((int)response.StatusCode, "Шлюз вернул пустой ответ.");
    }

    private async Task<T?> SendNullableAsync<T>(HttpMethod method, string path, CancellationToken ct = default)
    {
        using var request = Build(method, path, null);
        using var response = await _http.SendAsync(request, ct);
        if (response.StatusCode == HttpStatusCode.NoContent)
        {
            return default;
        }

        if (!response.IsSuccessStatusCode)
        {
            throw await CreateExceptionAsync(response, ct);
        }

        return await response.Content.ReadFromJsonAsync<T>(Json, ct);
    }

    private HttpRequestMessage Build(HttpMethod method, string path, object? body)
    {
        var request = new HttpRequestMessage(method, path);
        if (_session.Token is not null)
        {
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _session.Token);
        }

        if (body is not null)
        {
            request.Content = JsonContent.Create(body, options: Json);
        }

        return request;
    }

    private static async Task<GatewayException> CreateExceptionAsync(HttpResponseMessage response, CancellationToken ct)
    {
        var message = $"Шлюз вернул код {(int)response.StatusCode}.";
        try
        {
            var payload = await response.Content.ReadFromJsonAsync<ErrorPayload>(Json, ct);
            if (!string.IsNullOrWhiteSpace(payload?.Error))
            {
                message = payload!.Error!;
            }
        }
        catch (Exception)
        {
            // Тело ответа не в формате ошибки шлюза: остаётся код состояния.
        }

        return new GatewayException((int)response.StatusCode, message);
    }

    private sealed record ErrorPayload([property: JsonPropertyName("error")] string? Error);
}
