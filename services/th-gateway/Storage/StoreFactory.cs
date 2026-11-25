using TenderHive.Gateway.Configuration;

namespace TenderHive.Gateway.Storage;

public static class StoreFactory
{
    public static IJobStore Create(GatewayOptions options)
    {
        return options.UsePostgres
            ? new PostgresJobStore(options.PostgresDsn)
            : new InMemoryJobStore();
    }
}
