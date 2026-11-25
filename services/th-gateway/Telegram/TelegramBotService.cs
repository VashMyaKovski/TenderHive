using TenderHive.Contracts;
using TenderHive.Gateway.Services;

namespace TenderHive.Gateway.Telegram;

/// <summary>
/// Бот входит в состав шлюза: принимает комплект файлом и превращает его в задание.
/// Повторная отправка того же файла даёт тот же идемпотентный ключ и не создаёт дубликат.
/// </summary>
public sealed class TelegramBotService : BackgroundService
{
    private static readonly UserContext BotUser = new("telegram-bot", AppRole.Service);

    private readonly ITelegramClient _client;
    private readonly JobService _jobs;
    private readonly ILogger<TelegramBotService> _logger;

    public TelegramBotService(ITelegramClient client, JobService jobs, ILogger<TelegramBotService> logger)
    {
        _client = client;
        _jobs = jobs;
        _logger = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        if (!_client.Enabled)
        {
            _logger.LogWarning("TELEGRAM_BOT_TOKEN не задан: телеграм-бот выключен, приём файлов доступен через REST API.");
            return;
        }

        _logger.LogInformation("Телеграм-бот запущен: опрос обновлений Bot API");

        var offset = 0L;
        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                var updates = await _client.GetUpdatesAsync(offset, stoppingToken);
                foreach (var update in updates)
                {
                    offset = Math.Max(offset, update.UpdateId + 1);
                    await HandleAsync(update, stoppingToken);
                }
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                _logger.LogError(exception, "Опрос Telegram завершился ошибкой");
                try
                {
                    await Task.Delay(TimeSpan.FromSeconds(3), stoppingToken);
                }
                catch (OperationCanceledException)
                {
                    break;
                }
            }
        }
    }

    private async Task HandleAsync(TelegramUpdate update, CancellationToken ct)
    {
        var message = update.Message;
        if (message?.Chat is null)
        {
            return;
        }

        var chatId = message.Chat.Id;

        if (message.Document is not null)
        {
            await HandleDocumentAsync(chatId, message, ct);
            return;
        }

        var text = message.Text?.Trim() ?? string.Empty;
        if (text.StartsWith("/start", StringComparison.OrdinalIgnoreCase))
        {
            await _client.SendMessageAsync(chatId,
                "TenderHive: подготовка заявки на тендер.\n\nОтправьте комплект документации файлом (PDF, DOCX) — задание будет зарегистрировано.\n/jobs — последние задания\n/id — идентификатор чата",
                ct);
            return;
        }

        if (text.StartsWith("/jobs", StringComparison.OrdinalIgnoreCase))
        {
            var jobs = await _jobs.ListJobsAsync(200, ct);
            var mine = jobs.Where(job => job.TelegramChatId == chatId.ToString()).Take(10).ToList();
            if (mine.Count == 0)
            {
                await _client.SendMessageAsync(chatId, "Заданий пока нет. Отправьте комплект документации файлом.", ct);
                return;
            }

            var lines = mine.Select(job => $"{job.JobId} — {job.Status} от {job.CreatedAt:dd.MM.yyyy HH:mm} UTC");
            await _client.SendMessageAsync(chatId, "Последние задания:\n" + string.Join('\n', lines), ct);
            return;
        }

        if (text.StartsWith("/id", StringComparison.OrdinalIgnoreCase))
        {
            await _client.SendMessageAsync(chatId, $"Идентификатор чата: {chatId}", ct);
            return;
        }

        await _client.SendMessageAsync(chatId, "Отправьте комплект документации файлом. Команды: /jobs, /id", ct);
    }

    private async Task HandleDocumentAsync(long chatId, TelegramMessage message, CancellationToken ct)
    {
        var document = message.Document!;
        if (string.IsNullOrEmpty(document.FileId))
        {
            await _client.SendMessageAsync(chatId, "Не удалось прочитать файл: Bot API не вернул file_id.", ct);
            return;
        }

        var content = await _client.DownloadFileAsync(document.FileId, ct);
        if (content is null || content.Length == 0)
        {
            await _client.SendMessageAsync(chatId, "Не удалось скачать файл из Telegram. Повторите отправку.", ct);
            return;
        }

        var fileName = string.IsNullOrWhiteSpace(document.FileName) ? $"document-{message.MessageId}.bin" : document.FileName!;
        var contentType = string.IsNullOrWhiteSpace(document.MimeType) ? "application/octet-stream" : document.MimeType!;

        var input = new JobService.CreateJobInput(
            JobSource.Telegram,
            $"telegram:{chatId}",
            document.FileUniqueId ?? $"message:{message.MessageId}",
            "company-default",
            chatId.ToString(),
            new[] { new JobService.IncomingFile("document", fileName, contentType, content) });

        var outcome = await _jobs.CreateJobAsync(input, BotUser, ct);

        var prefix = outcome.Created
            ? "Комплект принят. Задание зарегистрировано."
            : "Этот комплект уже зарегистрирован: повторная загрузка не создала дубликат.";

        await _client.SendMessageAsync(chatId,
            $"{prefix}\nЗадание: {outcome.Job.JobId}\nФайл: {fileName} ({content.Length} байт)\nВерсия базы знаний: {outcome.Job.KbVersion}\nСтатус: {outcome.Job.Status}\n\nАртефакты придут в этот чат после утверждения пакета.",
            ct);
    }
}
