using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;
using TenderHive.Gateway.Configuration;

namespace TenderHive.Gateway.Telegram;

public interface ITelegramClient
{
    bool Enabled { get; }

    Task<IReadOnlyList<TelegramUpdate>> GetUpdatesAsync(long offset, CancellationToken ct);

    Task SendMessageAsync(long chatId, string text, CancellationToken ct);

    /// <returns><c>true</c>, если Bot API подтвердил отправку файла.</returns>
    Task<bool> SendDocumentAsync(long chatId, string fileName, byte[] content, string caption, CancellationToken ct);

    Task<byte[]?> DownloadFileAsync(string fileId, CancellationToken ct);
}

/// <summary>Обращения к Bot API без сторонних библиотек: только HTTP и JSON.</summary>
public sealed class TelegramClient : ITelegramClient
{
    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);

    private readonly HttpClient _http;
    private readonly GatewayOptions _options;
    private readonly ILogger<TelegramClient> _logger;

    public TelegramClient(IHttpClientFactory factory, GatewayOptions options, ILogger<TelegramClient> logger)
    {
        _http = factory.CreateClient(nameof(TelegramClient));
        _http.Timeout = TimeSpan.FromSeconds(60);
        _options = options;
        _logger = logger;
    }

    public bool Enabled => _options.TelegramEnabled;

    public async Task<IReadOnlyList<TelegramUpdate>> GetUpdatesAsync(long offset, CancellationToken ct)
    {
        var url = $"{Method("getUpdates")}?offset={offset}&timeout=25&allowed_updates=%5B%22message%22%5D";
        using var response = await _http.GetAsync(url, ct);
        if (!response.IsSuccessStatusCode)
        {
            _logger.LogWarning("getUpdates вернул код {StatusCode}", (int)response.StatusCode);
            return Array.Empty<TelegramUpdate>();
        }

        var payload = await response.Content.ReadFromJsonAsync<TelegramResponse<List<TelegramUpdate>>>(Json, ct);
        return payload?.Result is null ? Array.Empty<TelegramUpdate>() : payload.Result;
    }

    public async Task SendMessageAsync(long chatId, string text, CancellationToken ct)
    {
        using var content = JsonContent.Create(new { chat_id = chatId, text, disable_web_page_preview = true }, options: Json);
        using var response = await _http.PostAsync(Method("sendMessage"), content, ct);
        if (!response.IsSuccessStatusCode)
        {
            _logger.LogWarning("sendMessage вернул код {StatusCode}", (int)response.StatusCode);
        }
    }

    public async Task<bool> SendDocumentAsync(long chatId, string fileName, byte[] content, string caption, CancellationToken ct)
    {
        using var form = new MultipartFormDataContent();
        form.Add(new StringContent(chatId.ToString()), "chat_id");
        if (!string.IsNullOrWhiteSpace(caption))
        {
            form.Add(new StringContent(caption), "caption");
        }

        var file = new ByteArrayContent(content);
        file.Headers.ContentType = new MediaTypeHeaderValue("application/octet-stream");
        form.Add(file, "document", fileName);

        using var response = await _http.PostAsync(Method("sendDocument"), form, ct);
        if (response.IsSuccessStatusCode)
        {
            return true;
        }

        var body = await response.Content.ReadAsStringAsync(ct);
        _logger.LogWarning("sendDocument вернул код {StatusCode}: {Body}", (int)response.StatusCode, body);
        return false;
    }

    public async Task<byte[]?> DownloadFileAsync(string fileId, CancellationToken ct)
    {
        using var lookup = await _http.GetAsync($"{Method("getFile")}?file_id={Uri.EscapeDataString(fileId)}", ct);
        if (!lookup.IsSuccessStatusCode)
        {
            return null;
        }

        var payload = await lookup.Content.ReadFromJsonAsync<TelegramResponse<TelegramFile>>(Json, ct);
        var filePath = payload?.Result?.FilePath;
        if (string.IsNullOrWhiteSpace(filePath))
        {
            return null;
        }

        using var download = await _http.GetAsync($"https://api.telegram.org/file/bot{_options.TelegramBotToken}/{filePath}", ct);
        return download.IsSuccessStatusCode ? await download.Content.ReadAsByteArrayAsync(ct) : null;
    }

    private string Method(string name) => $"https://api.telegram.org/bot{_options.TelegramBotToken}/{name}";
}
