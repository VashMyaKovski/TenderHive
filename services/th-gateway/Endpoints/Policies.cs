namespace TenderHive.Gateway.Endpoints;

public static class Policies
{
    /// <summary>Только сервисный клиент (оркестратор).</summary>
    public const string Service = "service";

    /// <summary>Регистрация задания: специалист, снабженец, руководитель, сервис (почта).</summary>
    public const string JobCreate = "jobs.create";

    /// <summary>Чтение исходных документов комплекта.</summary>
    public const string JobDocuments = "jobs.documents";

    /// <summary>Администрирование базы знаний.</summary>
    public const string KbAdmin = "kb.admin";
}
