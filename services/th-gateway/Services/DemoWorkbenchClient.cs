using System.Net;
using System.Net.Http.Json;
using System.Security.Cryptography;
using System.Text.Json;
using TenderHive.Contracts;

namespace TenderHive.Gateway.Services;

public sealed record DemoDownload(byte[] Bytes, string ContentType, string Sha256);

public sealed class DemoWorkbenchClient(HttpClient http)
{
    public const string ClientName = "demo-workbench";
    public const long MaxResponseBytes = 8 * 1024 * 1024;

    public static string? ArtifactContentType(string name) => name switch
    {
        "offer.docx" => "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "offer.pdf" => "application/pdf",
        "offer.xlsx" => "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        _ => null
    };

    public async Task<DemoRunDto> StartAsync(DemoRunInput input, CancellationToken ct)
    {
        using var response = await SendAsync(HttpMethod.Post, "api/workbench/runs", input, ct);
        return await ReadRunAsync(response, ct);
    }

    public async Task<DemoRunDto?> GetAsync(Guid id, CancellationToken ct)
    {
        using var response = await SendAsync(HttpMethod.Get, $"api/workbench/runs/{id:D}", body: null, ct);
        return response.StatusCode == HttpStatusCode.NotFound ? null : await ReadRunAsync(response, ct, id);
    }

    public async Task<DemoRunDto> DecideAsync(Guid id, DemoDecisionInput input, CancellationToken ct)
    {
        using var response = await SendAsync(HttpMethod.Post, $"api/workbench/runs/{id:D}/decision", input, ct);
        return await ReadRunAsync(response, ct, id);
    }

    public async Task<DemoDownload> DownloadAsync(Guid id, string name, CancellationToken ct)
    {
        var contentType = ArtifactContentType(name) ?? throw new ApiException(404, "Файл комплекта не найден.");
        using var response = await SendAsync(HttpMethod.Get, $"api/workbench/runs/{id:D}/artifacts/{name}", body: null, ct);
        EnsureSuccess(response);
        var data = await response.Content.ReadAsByteArrayAsync(ct);
        var sha256 = Convert.ToHexStringLower(SHA256.HashData(data));
        if (data.Length == 0 || response.Content.Headers.ContentType?.MediaType != contentType ||
            !response.Headers.TryGetValues("X-Artifact-SHA256", out var hashes) ||
            !string.Equals(hashes.FirstOrDefault(), sha256, StringComparison.OrdinalIgnoreCase))
        {
            throw new ApiException(502, "Контрольная сумма или формат файла не прошли проверку.");
        }
        return new DemoDownload(data, contentType, sha256);
    }

    private async Task<HttpResponseMessage> SendAsync(HttpMethod method, string path, object? body, CancellationToken ct)
    {
        using var request = new HttpRequestMessage(method, path);
        if (body is not null)
        {
            request.Content = JsonContent.Create(body, options: JsonContract.Options);
        }

        try
        {
            return await http.SendAsync(request, ct);
        }
        catch (HttpRequestException)
        {
            throw new ApiException(502, "Сервис анализа недоступен.");
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            throw new ApiException(504, "Сервис анализа не ответил вовремя.");
        }
    }

    private static async Task<DemoRunDto> ReadRunAsync(HttpResponseMessage response, CancellationToken ct, Guid? expectedId = null)
    {
        EnsureSuccess(response);
        try
        {
            var run = await response.Content.ReadFromJsonAsync<DemoRunDto>(JsonContract.Options, ct);
            if (run is { Title: not null, Status: not null, Phase: not null, Documents: not null, Messages: not null, Artifacts: not null } &&
                run.Id != Guid.Empty &&
                (expectedId is null || run.Id == expectedId))
            {
                return run;
            }
        }
        catch (JsonException)
        {
            throw new ApiException(502, "Сервис анализа вернул некорректный ответ.");
        }
        throw new ApiException(502, "Сервис анализа вернул некорректный ответ.");
    }

    private static void EnsureSuccess(HttpResponseMessage response)
    {
        if (!response.IsSuccessStatusCode)
        {
            throw response.StatusCode switch
            {
                HttpStatusCode.NotFound => new ApiException(404, "Данные прогона не найдены."),
                HttpStatusCode.Conflict => new ApiException(409, "Карточка уже изменилась или закрыта."),
                HttpStatusCode.RequestEntityTooLarge => new ApiException(413, "Размер запроса превышает лимит."),
                HttpStatusCode.UnprocessableEntity => new ApiException(422, "Проверьте параметры лота и содержимое документов."),
                HttpStatusCode.TooManyRequests => new ApiException(429, "Дождитесь завершения другого прогона."),
                _ => new ApiException(502, "Сервис анализа недоступен.")
            };
        }
    }
}
