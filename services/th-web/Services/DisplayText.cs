using TenderHive.Contracts;

namespace TenderHive.Web.Services;

/// <summary>Подписи статусов, ролей и оснований остановки для интерфейса.</summary>
public static class DisplayText
{
    public static string Status(string status) => status switch
    {
        JobStatus.Registered => "Зарегистрировано",
        JobStatus.Queued => "В очереди",
        JobStatus.Processing => "В обработке",
        JobStatus.WaitingHuman => "Ожидает решения",
        JobStatus.Completed => "Готово",
        JobStatus.Rejected => "Отклонено",
        JobStatus.Failed => "Ошибка",
        _ => status
    };

    public static string StatusClass(string status) => status switch
    {
        JobStatus.WaitingHuman => "badge warn",
        JobStatus.Completed => "badge ok",
        JobStatus.Rejected => "badge bad",
        JobStatus.Failed => "badge bad",
        JobStatus.Processing => "badge info",
        _ => "badge"
    };

    public static string Role(string role) => role switch
    {
        AppRole.TenderSpecialist => "Тендерный специалист",
        AppRole.Procurement => "Снабженец",
        AppRole.Lawyer => "Юрист",
        AppRole.DepartmentHead => "Руководитель подразделения",
        AppRole.KbAdmin => "Администратор базы знаний",
        AppRole.Service => "Сервисный клиент",
        _ => role
    };

    public static string Reason(string reasonCode) => reasonCode switch
    {
        ReasonCode.PenaltyAboveThreshold => "Штраф выше порога",
        ReasonCode.MarginBelowThreshold => "Маржа ниже порога",
        ReasonCode.ResourceGap => "Недостаточно ресурсов",
        ReasonCode.RequirementsIncomplete => "Требования неполны или противоречивы",
        ReasonCode.NoSolution => "Решатель не нашёл решения",
        ReasonCode.FinalApproval => "Утверждение пакета заявки",
        _ => reasonCode
    };

    public static string Decision(string decision) => decision switch
    {
        DecisionValue.Pending => "Ожидает",
        DecisionValue.Approve => "Согласовано",
        DecisionValue.Reject => "Отклонено",
        DecisionValue.Timeout => "Таймаут",
        _ => decision
    };

    public static string Source(string source) => source switch
    {
        JobSource.Mail => "Почта",
        JobSource.Telegram => "Telegram",
        JobSource.Web => "Дашборд",
        _ => source
    };

    public static string ArtifactKind(string kind) => kind switch
    {
        "checklist" => "Чек-лист требований",
        "application" => "Проект заявки",
        "risk_report" => "Отчёт о рисках",
        "estimate" => "Смета",
        _ => kind
    };

    public static string Bytes(long size) => size switch
    {
        < 1024 => $"{size} Б",
        < 1024 * 1024 => $"{size / 1024.0:0.#} КБ",
        _ => $"{size / (1024.0 * 1024.0):0.##} МБ"
    };

    public static string Remaining(DateTimeOffset deadline)
    {
        var left = deadline - DateTimeOffset.UtcNow;
        if (left <= TimeSpan.Zero)
        {
            return "срок истёк";
        }

        return left.TotalHours >= 1
            ? $"осталось {(int)left.TotalHours} ч {left.Minutes} мин"
            : $"осталось {left.Minutes} мин";
    }

    public static string Utc(DateTimeOffset value) => value.ToString("dd.MM.yyyy HH:mm");

    public static string UtcOrEmpty(DateTimeOffset? value) => value is null ? "—" : Utc(value.Value);
}
