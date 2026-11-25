using TenderHive.Contracts;
using TenderHive.Gateway.Configuration;
using TenderHive.Gateway.Services;
using TenderHive.Gateway.Storage;

namespace TenderHive.Gateway.Telegram;

/// <summary>
/// Выдача артефактов файлами в Telegram: документы уходят в DOCX и PDF, расчёт — в XLSX.
/// Отметка о выдаче ставится только после успешной отправки каждого файла, поэтому
/// незавершённая выдача повторяется на следующем проходе.
/// </summary>
public sealed class ArtifactNotifier : BackgroundService
{
    private readonly IJobStore _store;
    private readonly IArtifactStorage _storage;
    private readonly ArtifactExporter _exporter;
    private readonly ITelegramClient _client;
    private readonly GatewayOptions _options;
    private readonly ILogger<ArtifactNotifier> _logger;

    public ArtifactNotifier(
        IJobStore store,
        IArtifactStorage storage,
        ArtifactExporter exporter,
        ITelegramClient client,
        GatewayOptions options,
        ILogger<ArtifactNotifier> logger)
    {
        _store = store;
        _storage = storage;
        _exporter = exporter;
        _client = client;
        _options = options;
        _logger = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        if (!_client.Enabled)
        {
            _logger.LogWarning("TELEGRAM_BOT_TOKEN не задан: выдача артефактов в Telegram выключена.");
            return;
        }

        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                await DeliverAsync(stoppingToken);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                _logger.LogError(exception, "Ошибка выдачи артефактов в Telegram");
            }

            try
            {
                await Task.Delay(TimeSpan.FromSeconds(_options.DeliveryPollSeconds), stoppingToken);
            }
            catch (OperationCanceledException)
            {
                break;
            }
        }
    }

    private async Task DeliverAsync(CancellationToken ct)
    {
        var jobs = await _store.ListJobsAwaitingDeliveryAsync(20, ct);
        foreach (var job in jobs)
        {
            if (!long.TryParse(job.TelegramChatId, out var chatId))
            {
                continue;
            }

            var artifacts = await _store.ListArtifactsAsync(job.JobId, ct);
            if (artifacts.Count == 0)
            {
                continue;
            }

            await _client.SendMessageAsync(chatId,
                $"Задание {job.JobId}\nСтатус: {job.Status}\nРекомендация: {job.Recommendation ?? "не сформирована"}\nАртефактов в пакете: {artifacts.Count}",
                ct);

            var delivered = true;
            foreach (var artifact in artifacts)
            {
                var content = _storage.Read(artifact.StoragePath);
                if (content is null)
                {
                    _logger.LogWarning("Файл артефакта {ArtifactId} отсутствует в хранилище", artifact.ArtifactId);
                    delivered = false;
                    continue;
                }

                foreach (var format in ArtifactFormats.DeliveryPlan(artifact))
                {
                    ArtifactExport export;
                    try
                    {
                        export = _exporter.Export(artifact, content, format);
                    }
                    catch (Exception exception)
                    {
                        _logger.LogError(exception, "Не удалось подготовить {Format} для артефакта {ArtifactId}", format, artifact.ArtifactId);
                        delivered = false;
                        continue;
                    }

                    var caption = $"{artifact.Kind}, {ArtifactFormats.Extension(format)}, версия {artifact.Version}, {job.JobId}";
                    if (!await _client.SendDocumentAsync(chatId, export.FileName, export.Content, caption, ct))
                    {
                        delivered = false;
                    }
                }
            }

            if (delivered)
            {
                await _store.MarkArtifactsDeliveredAsync(job.JobId, DateTimeOffset.UtcNow, ct);
            }
        }
    }
}
