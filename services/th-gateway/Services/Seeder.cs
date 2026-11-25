using TenderHive.Contracts;
using TenderHive.Gateway.Configuration;
using TenderHive.Gateway.Domain;
using TenderHive.Gateway.Security;
using TenderHive.Gateway.Storage;

namespace TenderHive.Gateway.Services;

/// <summary>
/// Начальное наполнение: по пользователю на роль (пароль из <c>DEMO_PASSWORD</c>) и три версии базы знаний.
/// В рабочем контуре пользователи приходят из службы каталогов, а версии — из <c>th-kb</c>.
/// </summary>
public static class Seeder
{
    public static async Task SeedAsync(IJobStore store, GatewayOptions options, PasswordHasher hasher, ILogger logger, CancellationToken ct)
    {
        var users = new (string Username, string DisplayName, string Role)[]
        {
            ("specialist", "Тендерный специалист", AppRole.TenderSpecialist),
            ("procurement", "Снабженец", AppRole.Procurement),
            ("lawyer", "Юрист", AppRole.Lawyer),
            ("head", "Руководитель подразделения", AppRole.DepartmentHead),
            ("kbadmin", "Администратор базы знаний", AppRole.KbAdmin),
            ("orchestrator", "Оркестратор (сервис)", AppRole.Service)
        };

        foreach (var user in users)
        {
            if (await store.FindUserAsync(user.Username, ct) is not null)
            {
                continue;
            }

            await store.AddUserAsync(new UserRecord
            {
                UserId = Guid.NewGuid(),
                Username = user.Username,
                DisplayName = user.DisplayName,
                Role = user.Role,
                PasswordHash = hasher.Hash(options.DemoPassword)
            }, ct);

            logger.LogInformation("Создан пользователь {Username} с ролью {Role}", user.Username, user.Role);
        }

        var versions = new (string Version, string Status, int DaysAgo, int Rules, string Notes)[]
        {
            ("kb-2026.09.1", "archived", 60, 84, "Первая сборка правил допуска и расчёта сметы."),
            ("kb-2026.10.1", "archived", 30, 96, "Добавлены правила штрафных санкций и обеспечения по постановлению № 168."),
            ("kb-2026.11.1", "active", 5, 104, "Уточнены пороги маржинальности и перечень требований к обеспечению заявки.")
        };

        foreach (var version in versions)
        {
            await store.EnsureKbVersionAsync(new KbVersionRecord
            {
                Version = version.Version,
                Status = version.Status,
                CreatedAt = DateTimeOffset.UtcNow.AddDays(-version.DaysAgo),
                ActivatedAt = version.Status == "active" ? DateTimeOffset.UtcNow.AddDays(-version.DaysAgo) : null,
                ActivatedBy = version.Status == "active" ? "kbadmin" : null,
                RulesCount = version.Rules,
                Notes = version.Notes
            }, ct);
        }
    }
}
