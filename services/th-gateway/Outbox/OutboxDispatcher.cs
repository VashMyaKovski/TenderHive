using Confluent.Kafka;
using TenderHive.Gateway.Configuration;
using TenderHive.Gateway.Storage;

namespace TenderHive.Gateway.Outbox;

/// <summary>
/// Публикация событий из таблицы outbox. Событие пишется в одной транзакции с изменением состояния,
/// поэтому падение сервиса не приводит к потере события: неопубликованные записи берутся повторно.
/// </summary>
public sealed class OutboxDispatcher : BackgroundService
{
    private readonly IJobStore _store;
    private readonly GatewayOptions _options;
    private readonly ILogger<OutboxDispatcher> _logger;
    private IProducer<string, string>? _producer;

    public OutboxDispatcher(IJobStore store, GatewayOptions options, ILogger<OutboxDispatcher> logger)
    {
        _store = store;
        _options = options;
        _logger = logger;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        if (_options.KafkaEnabled)
        {
            var config = new ProducerConfig
            {
                BootstrapServers = string.Join(',', _options.KafkaBrokerList),
                Acks = Acks.All,
                EnableIdempotence = true
            };
            _producer = new ProducerBuilder<string, string>(config).Build();
            _logger.LogInformation("Публикация событий в Kafka: {Brokers}", config.BootstrapServers);
        }
        else
        {
            _logger.LogWarning("KAFKA_BROKERS не задан: события outbox выводятся в журнал и помечаются опубликованными.");
        }

        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                await DispatchAsync(stoppingToken);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                _logger.LogError(exception, "Ошибка публикации событий outbox");
            }

            try
            {
                await Task.Delay(TimeSpan.FromSeconds(_options.OutboxPollSeconds), stoppingToken);
            }
            catch (OperationCanceledException)
            {
                break;
            }
        }

        _producer?.Flush(TimeSpan.FromSeconds(5));
        _producer?.Dispose();
    }

    private async Task DispatchAsync(CancellationToken ct)
    {
        var events = await _store.DequeueOutboxAsync(50, ct);
        foreach (var item in events)
        {
            if (_producer is null)
            {
                _logger.LogInformation("outbox {Topic}: {Payload}", item.Topic, item.PayloadJson);
                await _store.MarkOutboxPublishedAsync(item.EventId, ct);
                continue;
            }

            try
            {
                await _producer.ProduceAsync(
                    item.Topic,
                    new Message<string, string> { Key = item.Topic, Value = item.PayloadJson },
                    ct);
                await _store.MarkOutboxPublishedAsync(item.EventId, ct);
            }
            catch (Exception exception)
            {
                _logger.LogError(exception, "Не удалось опубликовать событие {EventId} в тему {Topic}", item.EventId, item.Topic);
                await _store.MarkOutboxFailedAsync(item.EventId, exception.Message, ct);
            }
        }
    }
}
