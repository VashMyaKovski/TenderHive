using Microsoft.Extensions.Configuration;

namespace TenderHive.Gateway.Configuration;

/// <summary>
/// Все параметры шлюза читаются из переменных окружения (файл <c>.env</c> загружается при старте).
/// В коде нет значений инициализации: пороги, сроки и адреса приходят из конфигурации.
/// </summary>
public sealed class GatewayOptions
{
    public string PostgresDsn { get; init; } = string.Empty;
    public string KafkaBrokers { get; init; } = string.Empty;
    public string TelegramBotToken { get; init; } = string.Empty;
    public string JwtSecret { get; init; } = string.Empty;
    public string ServiceApiKey { get; init; } = string.Empty;
    public string ArtifactBucket { get; init; } = "data/artifacts";
    public string DemoPassword { get; init; } = "tenderhive";
    public string GatewayPublicUrl { get; init; } = "http://localhost:8080";
    public string RedisUrl { get; init; } = string.Empty;
    public double JwtTtlHours { get; init; } = 8;
    public double HitlTimeoutHours { get; init; } = 24;
    public long MaxUploadBytes { get; init; } = 32L * 1024 * 1024;
    public int OutboxPollSeconds { get; init; } = 2;
    public int TimeoutSweepSeconds { get; init; } = 5;
    public int DeliveryPollSeconds { get; init; } = 5;
    public int ArtifactLinkTtlMinutes { get; init; } = 10;

    /// <summary>Без DSN шлюз поднимается в демонстрационном режиме на хранилище в памяти.</summary>
    public bool UsePostgres => !string.IsNullOrWhiteSpace(PostgresDsn);

    public bool KafkaEnabled => !string.IsNullOrWhiteSpace(KafkaBrokers);

    public bool TelegramEnabled => !string.IsNullOrWhiteSpace(TelegramBotToken);

    public string[] KafkaBrokerList => KafkaBrokers.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);

    public static GatewayOptions FromConfiguration(IConfiguration configuration)
    {
        var options = new GatewayOptions
        {
            PostgresDsn = Read(configuration, "POSTGRES_DSN"),
            KafkaBrokers = Read(configuration, "KAFKA_BROKERS"),
            TelegramBotToken = Read(configuration, "TELEGRAM_BOT_TOKEN"),
            JwtSecret = Read(configuration, "JWT_SECRET"),
            ServiceApiKey = Read(configuration, "SERVICE_API_KEY"),
            ArtifactBucket = Read(configuration, "ARTIFACT_BUCKET", "data/artifacts"),
            DemoPassword = Read(configuration, "DEMO_PASSWORD", "tenderhive"),
            GatewayPublicUrl = Read(configuration, "GATEWAY_PUBLIC_URL", "http://localhost:8080").TrimEnd('/'),
            RedisUrl = Read(configuration, "REDIS_URL"),
            JwtTtlHours = ReadDouble(configuration, "JWT_TTL_HOURS", 8),
            HitlTimeoutHours = ReadDouble(configuration, "HITL_TIMEOUT_HOURS", 24),
            MaxUploadBytes = ReadLong(configuration, "MAX_UPLOAD_MB", 32) * 1024 * 1024,
            OutboxPollSeconds = (int)ReadLong(configuration, "OUTBOX_POLL_SECONDS", 2),
            TimeoutSweepSeconds = (int)ReadLong(configuration, "HITL_SWEEP_SECONDS", 5),
            DeliveryPollSeconds = (int)ReadLong(configuration, "DELIVERY_POLL_SECONDS", 5),
            ArtifactLinkTtlMinutes = (int)ReadLong(configuration, "ARTIFACT_LINK_TTL_MINUTES", 10)
        };

        if (options.JwtSecret.Length < 32)
        {
            throw new InvalidOperationException("JWT_SECRET должен содержать не менее 32 символов: задайте его в .env.");
        }

        if (options.ServiceApiKey.Length < 8)
        {
            throw new InvalidOperationException("SERVICE_API_KEY не задан: без него оркестратор не вернёт результат задания.");
        }

        if (options.HitlTimeoutHours <= 0)
        {
            throw new InvalidOperationException("HITL_TIMEOUT_HOURS должен быть положительным числом часов.");
        }

        if (options.JwtTtlHours <= 0)
        {
            throw new InvalidOperationException("JWT_TTL_HOURS должен быть положительным числом часов.");
        }

        return options;
    }

    private static string Read(IConfiguration configuration, string key, string fallback = "")
    {
        var value = configuration[key];
        return string.IsNullOrWhiteSpace(value) ? fallback : value.Trim();
    }

    private static double ReadDouble(IConfiguration configuration, string key, double fallback)
    {
        var value = configuration[key];
        return double.TryParse(value, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var parsed)
            ? parsed
            : fallback;
    }

    private static long ReadLong(IConfiguration configuration, string key, long fallback)
    {
        var value = configuration[key];
        return long.TryParse(value, out var parsed) ? parsed : fallback;
    }
}
