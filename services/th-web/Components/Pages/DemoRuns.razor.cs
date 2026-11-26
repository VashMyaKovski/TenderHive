using System.Globalization;
using System.Net;
using System.Text;
using System.Text.Json;
using Microsoft.AspNetCore.Components;
using Microsoft.AspNetCore.Components.Forms;
using Microsoft.JSInterop;
using TenderHive.Contracts;
using TenderHive.Web.Services;

namespace TenderHive.Web.Components.Pages;

public partial class DemoRuns
{
    private const int MaxFiles = 5;
    private const int MaxFileBytes = 2 * 1024 * 1024;
    private const int MaxBatchBytes = 5 * 1024 * 1024;
    private static readonly JsonSerializerOptions IndentedJson = new() { WriteIndented = true };
    private static readonly CultureInfo DashboardCulture = CultureInfo.GetCultureInfo("ru-BY");

    [Parameter] public Guid? RunId { get; set; }

    [Inject] private IConfiguration Configuration { get; set; } = default!;
    [Inject] private GatewayClient Gateway { get; set; } = default!;
    [Inject] private SessionState Session { get; set; } = default!;
    [Inject] private NavigationManager Navigation { get; set; } = default!;
    [Inject] private IJSRuntime JS { get; set; } = default!;

    private CancellationTokenSource? page;
    private Guid? loadedId;
    private int runRevision;
    private List<DemoRunDto> runs = [];
    private readonly List<DemoAttachment> files = [];
    private readonly List<RequirementDraft> requirementDrafts = [];
    private DemoRunDto? run;
    private bool readingFiles, refreshing, busy, gone, accessDenied, manualSample;
    private string? formError, loadError, actionError;
    private string title = string.Empty;
    private string context = string.Empty;
    private decimal? quantity, available, unitCost, offerPrice, penalty;
    private DateOnly? deliveryDeadline;
    private int? validity;
    private string comment = string.Empty;
    private decimal? confirmedQuantity, additionalCost;
    private DateOnly? confirmedDeliveryDate;

    protected override async Task OnParametersSetAsync()
    {
        if (!Configuration.GetValue<bool>("DEMO_DASHBOARD_ENABLED"))
        {
            return;
        }

        if (!Session.IsAuthenticated)
        {
            NavigateToLogin();
            return;
        }

        if (page is not null && loadedId == RunId)
        {
            return;
        }

        DisposePage();
        page = new CancellationTokenSource();
        var token = page.Token;
        loadedId = RunId;
        run = null;
        gone = false;
        accessDenied = false;
        busy = false;
        refreshing = false;
        readingFiles = false;
        loadError = null;
        actionError = null;
        comment = string.Empty;
        confirmedQuantity = null;
        additionalCost = null;
        confirmedDeliveryDate = null;
        requirementDrafts.Clear();

        await RefreshAsync(token);
        if (RunId is not null && !token.IsCancellationRequested)
        {
            _ = PollAsync(token);
        }
    }

    private void NavigateToLogin() =>
        Navigation.NavigateTo("/login?returnUrl=" + Uri.EscapeDataString(new Uri(Navigation.Uri).PathAndQuery), replace: true);

    private async Task PollAsync(CancellationToken token)
    {
        using var timer = new PeriodicTimer(TimeSpan.FromSeconds(2));
        try
        {
            while (!gone && await timer.WaitForNextTickAsync(token))
            {
                if (run?.Phase is "done" or "failed" or "declined" or "cancelled" or "needs_clarification")
                {
                    return;
                }

                if (!busy && !accessDenied)
                {
                    await RefreshAsync(token);
                    if (!token.IsCancellationRequested)
                    {
                        await InvokeAsync(StateHasChanged);
                    }
                }
            }
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested)
        {
        }
    }

    private Task ReloadAsync() => RefreshAsync(page?.Token ?? CancellationToken.None);

    private async Task RefreshAsync(CancellationToken token)
    {
        if (refreshing || token.IsCancellationRequested)
        {
            return;
        }

        refreshing = true;
        var revision = runRevision;
        try
        {
            if (RunId is { } id)
            {
                var result = await Gateway.GetDemoRunAsync(id, token);
                if (!token.IsCancellationRequested && revision == runRevision)
                {
                    accessDenied = false;
                    SetRun(result);
                }
            }
            else
            {
                var result = await Gateway.ListDemoRunsAsync(token);
                if (!token.IsCancellationRequested)
                {
                    runs = result;
                }
            }
            if (!token.IsCancellationRequested)
            {
                loadError = null;
            }
        }
        catch (GatewayException exception) when (!token.IsCancellationRequested &&
            exception.StatusCode == (int)HttpStatusCode.Unauthorized)
        {
            await Session.SignOutAsync();
            NavigateToLogin();
        }
        catch (GatewayException exception)
        {
            if (!token.IsCancellationRequested)
            {
                gone = exception.StatusCode == (int)HttpStatusCode.Gone;
                accessDenied = exception.StatusCode is (int)HttpStatusCode.Forbidden or (int)HttpStatusCode.NotFound;
                if (gone || accessDenied)
                {
                    run = null;
                }
                loadError = gone
                    ? "Этот прогон больше недоступен. Запустите новый анализ."
                    : exception.Message;
            }
        }
        catch (HttpRequestException)
        {
            if (!token.IsCancellationRequested)
            {
                loadError = "Шлюз недоступен. Попробуйте ещё раз.";
            }
        }
        catch (OperationCanceledException)
        {
            if (!token.IsCancellationRequested)
            {
                loadError = "Шлюз не ответил вовремя. Попробуйте ещё раз.";
            }
        }
        finally
        {
            if (!token.IsCancellationRequested)
            {
                refreshing = false;
            }
        }
    }

    private async Task ReadFilesAsync(InputFileChangeEventArgs args)
    {
        var token = page?.Token ?? CancellationToken.None;
        readingFiles = true;
        manualSample = false;
        formError = null;
        files.Clear();
        try
        {
            if (args.FileCount is < 1 or > MaxFiles)
            {
                formError = "Выберите от одного до пяти документов.";
                return;
            }

            var selected = args.GetMultipleFiles(MaxFiles);
            if (selected.Sum(item => item.Size) > MaxBatchBytes ||
                selected.Any(item => item.Size is <= 0 or > MaxFileBytes))
            {
                formError = "Проверьте размер документов. Каждый файл должен быть непустым и не превышать 2 МБ, комплект — 5 МБ.";
                return;
            }

            foreach (var file in selected)
            {
                var suffix = Path.GetExtension(file.Name).ToLowerInvariant();
                var mimeType = suffix switch
                {
                    ".pdf" => "application/pdf",
                    ".docx" => "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    ".png" => "image/png",
                    ".jpg" or ".jpeg" => "image/jpeg",
                    ".tif" or ".tiff" => "image/tiff",
                    ".bmp" => "image/bmp",
                    ".webp" => "image/webp",
                    _ => null
                };
                if (mimeType is null)
                {
                    formError = "Принимаются PDF, DOCX, PNG, JPEG, TIFF, BMP и WEBP.";
                    files.Clear();
                    return;
                }

                await using var input = file.OpenReadStream(MaxFileBytes, token);
                using var content = new MemoryStream();
                await input.CopyToAsync(content, token);
                files.Add(new DemoAttachment(file.Name, mimeType, Convert.ToBase64String(content.ToArray())));
            }
        }
        catch (Exception exception) when (exception is IOException or JSException)
        {
            files.Clear();
            formError = "Документы не удалось прочитать. Выберите их ещё раз.";
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested)
        {
            files.Clear();
        }
        finally
        {
            readingFiles = false;
        }
    }

    private void FillSample()
    {
        title = "Хозяйственные товары · лот 3";
        quantity = 996;
        available = 1000;
        validity = 60;
        unitCost = 4.50m;
        offerPrice = 5577.60m;
        penalty = 0.15m;
        deliveryDeadline = DateOnly.FromDateTime(DateTime.UtcNow.AddDays(30));
        context = "The published draft includes a daily penalty for late delivery.";
        formError = null;

        manualSample = true;
        files.Clear();
        const string sample = "Tender 341041674, lot 3. Required quantity: 996 rolls. Offer validity at least 60 working days. The published draft includes a daily penalty for late delivery.";
        files.Add(new DemoAttachment("пример-лота-3.txt", "text/plain", Convert.ToBase64String(Encoding.UTF8.GetBytes(sample))));
    }

    private async Task StartAsync()
    {
        if (busy || readingFiles)
        {
            return;
        }

        formError = null;
        if (files.Count == 0 || string.IsNullOrWhiteSpace(title))
        {
            formError = "Укажите название закупки и добавьте документы.";
            return;
        }

        var token = page?.Token ?? CancellationToken.None;
        busy = true;
        try
        {
            var input = new DemoRunInput(title.Trim(), [.. files]);
            if (manualSample)
            {
                if (!TryReadFacts(out var facts, out formError))
                {
                    return;
                }
                input = input with { Facts = facts, Context = context.Trim() };
            }
            var created = await Gateway.StartDemoRunAsync(input, token);
            if (!token.IsCancellationRequested)
            {
                files.Clear();
                manualSample = false;
                Navigation.NavigateTo($"/demo/{created.Id}");
            }
        }
        catch (GatewayException exception)
        {
            formError = exception.Message;
        }
        catch (HttpRequestException)
        {
            formError = "Не удалось связаться со шлюзом. Попробуйте ещё раз.";
        }
        catch (OperationCanceledException)
        {
            if (!token.IsCancellationRequested)
            {
                formError = "Шлюз не ответил вовремя. Проверьте список прогонов.";
            }
        }
        finally
        {
            if (!token.IsCancellationRequested)
            {
                busy = false;
            }
        }
    }

    private bool TryReadFacts(out DemoFacts? facts, out string? error)
    {
        facts = null;
        if (string.IsNullOrWhiteSpace(context) || context.Trim().Length > 2000 ||
            quantity is null or <= 0 || available is null or < 0 || validity is null or < 0 or > 3650 ||
            unitCost is null or < 0 || offerPrice is null or <= 0 || penalty is < 0 or > 100)
        {
            error = "Проверьте параметры закупки, данные компании и фрагмент для оценки риска.";
            return false;
        }
        if (available < quantity && deliveryDeadline is null)
        {
            error = "Укажите крайний срок поставки, чтобы согласовать дозаказ.";
            return false;
        }

        facts = new DemoFacts(quantity.Value, validity.Value, unitCost.Value, offerPrice.Value,
            available.Value, penalty, deliveryDeadline);
        error = null;
        return true;
    }

    private async Task ConfirmExtractionAsync()
    {
        if (run is not { Phase: "awaiting_extraction_confirmation", CanReviewExtraction: true } active)
        {
            return;
        }
        if (!TryReadFacts(out var facts, out actionError) || facts is null)
        {
            return;
        }
        if (requirementDrafts.Any(item => string.IsNullOrWhiteSpace(item.Text)))
        {
            actionError = "Заполните текст каждого требования.";
            return;
        }

        var input = new DemoExtractionConfirmation(
            facts, context.Trim(), requirementDrafts.Select(item => item.ToCorrection()).ToArray());
        await RunActionAsync(async token =>
        {
            var updated = await Gateway.ConfirmDemoExtractionAsync(active.Id, input, token);
            if (!token.IsCancellationRequested)
            {
                runRevision++;
                SetRun(updated);
            }
        });
    }

    private async Task RetryExtractionAsync()
    {
        if (run is not { Phase: "extraction_failed", CanReviewExtraction: true } active)
        {
            return;
        }
        await RunActionAsync(async token =>
        {
            var updated = await Gateway.RetryDemoExtractionAsync(active.Id, token);
            if (!token.IsCancellationRequested)
            {
                runRevision++;
                SetRun(updated);
            }
        });
    }

    private async Task DecideAsync(Guid cardId, string decision)
    {
        if (run is not { Card: { } card } active || card.Id != cardId || busy)
        {
            return;
        }

        if (decision == "REJECTED" && string.IsNullOrWhiteSpace(comment))
        {
            actionError = "Укажите причину отклонения в комментарии.";
            return;
        }

        if (card.RequiresComment && string.IsNullOrWhiteSpace(comment))
        {
            actionError = "Укажите обоснование согласования.";
            return;
        }

        DemoSupplyConfirmation? supply = null;
        if (decision == "APPROVED" && card.Reason == "RESOURCE_GAP")
        {
            if (card.Supply?.DeliveryDeadline is null || confirmedQuantity is null or <= 0 ||
                confirmedDeliveryDate is null || additionalCost is null or < 0)
            {
                actionError = "Укажите объём, дату и дополнительные затраты на поставку.";
                return;
            }

            supply = new DemoSupplyConfirmation(confirmedQuantity.Value, confirmedDeliveryDate.Value, additionalCost.Value);
        }

        await RunActionAsync(async token =>
        {
            var updated = await Gateway.DecideDemoRunAsync(
                active.Id, new DemoDecisionInput(cardId, decision, comment.Trim(), supply), token);
            if (!token.IsCancellationRequested)
            {
                runRevision++;
                SetRun(updated);
            }
        });
    }

    private async Task DownloadAsync(Guid id, string name) => await RunActionAsync(async token =>
    {
        var download = await Gateway.DownloadDemoArtifactAsync(id, name, token);
        await using var module = await JS.InvokeAsync<IJSObjectReference>("import", token, "./download.js");
        await using var stream = new MemoryStream(download.Bytes, writable: false);
        using var reference = new DotNetStreamReference(stream);
        await module.InvokeVoidAsync("downloadFile", token, name, reference, download.ContentType);
    });

    private async Task RunActionAsync(Func<CancellationToken, Task> action)
    {
        if (busy)
        {
            return;
        }

        var token = page?.Token ?? CancellationToken.None;
        actionError = null;
        busy = true;
        try
        {
            await action(token);
        }
        catch (GatewayException exception)
        {
            actionError = exception.Message;
            if (exception.StatusCode is (int)HttpStatusCode.Conflict or (int)HttpStatusCode.Gone)
            {
                await RefreshAsync(token);
            }
        }
        catch (Exception exception) when (exception is HttpRequestException or JSException)
        {
            actionError = "Операция не выполнена. Попробуйте ещё раз.";
        }
        catch (OperationCanceledException)
        {
            if (!token.IsCancellationRequested)
            {
                actionError = "Время ожидания ответа истекло. Попробуйте ещё раз.";
            }
        }
        finally
        {
            if (!token.IsCancellationRequested)
            {
                busy = false;
            }
        }
    }

    private void SetRun(DemoRunDto updated)
    {
        if (run?.Card?.Id != updated.Card?.Id)
        {
            comment = string.Empty;
            confirmedQuantity = null;
            additionalCost = null;
            confirmedDeliveryDate = null;
        }

        if (updated.Phase == "awaiting_extraction_confirmation" &&
            (run?.Id != updated.Id || run?.Phase != updated.Phase))
        {
            requirementDrafts.Clear();
            requirementDrafts.AddRange((updated.Extraction?.Requirements ?? []).Select(item => new RequirementDraft(item)));
            quantity = updated.Facts?.Quantity;
            validity = updated.Facts?.OfferValidityWorkdays;
            unitCost = updated.Facts?.UnitCost;
            offerPrice = updated.Facts?.OfferPrice;
            available = updated.Facts?.AvailableQuantity;
            penalty = updated.Facts?.PenaltyDailyPercent;
            deliveryDeadline = updated.Facts?.DeliveryDeadline;
            context = updated.Context ?? string.Empty;
        }

        run = updated;
    }

    private void UseRiskContext(string text) => context = text.Length > 2000 ? text[..2000] : text;

    private static string DocumentLabel(DemoRunDto run, Guid fileId) =>
        run.Documents.FirstOrDefault(document => document.FileId == fileId)?.Name ?? fileId.ToString("D");

    private static string RequirementKindLabel(string kind) => kind switch
    {
        "penalty" => "Неустойка",
        "deadline" => "Срок",
        "security" => "Обеспечение",
        "qualification" => "Квалификация",
        "resource" => "Ресурсы",
        "financial" => "Финансовое условие",
        _ => "Другое требование"
    };

    private static string ExtractionStatusLabel(string status) => status switch
    {
        "OK" => "Документы обработаны",
        "INCOMPLETE" => "Нужна проверка извлечённых данных",
        "REJECTED" => "Документ отклонён",
        "FAILED" => "Обработка документов не выполнена",
        _ => status
    };

    private static string RequirementValue(JsonElement? value) =>
        value is { ValueKind: not (JsonValueKind.Null or JsonValueKind.Undefined) } scalar
            ? scalar.ToString()
            : string.Empty;

    private sealed class RequirementDraft(DemoExtractedRequirementDto requirement)
    {
        public DemoExtractedRequirementDto Original { get; } = requirement;
        public string Text { get; set; } = requirement.Text;
        public string Value { get; set; } = RequirementValue(requirement.Value);

        public DemoRequirementCorrection ToCorrection()
        {
            var value = Value.Trim();
            JsonElement? confirmedValue = value == RequirementValue(Original.Value)
                ? Original.Value
                : value.Length == 0 ? null : JsonSerializer.SerializeToElement(value);
            return new DemoRequirementCorrection(Original.RequirementId, Text.Trim(), confirmedValue);
        }
    }

    private void DisposePage()
    {
        page?.Cancel();
        page?.Dispose();
        page = null;
    }

    public void Dispose() => DisposePage();

    private static string DateLabel(DateOnly date) => date.ToString("d", DashboardCulture);

    private static string StatusLabel(string status, string phase) => (status, phase) switch
    {
        (_, "declined" or "cancelled") => "Отклонено",
        (_, "awaiting_extraction_confirmation") => "Ждёт проверки документов",
        (_, "extraction_failed") => "Ошибка обработки документов",
        ("DONE", _) => "Анализ завершён",
        ("ERROR", _) => "Анализ остановлен",
        ("WAITING_HUMAN", _) => "Ждёт согласования",
        (_, "needs_clarification") => "Нужно уточнение",
        _ => "В обработке"
    };

    private static string StatusClass(string status) => status switch
    {
        "DONE" => "badge ok",
        "ERROR" => "badge bad",
        "WAITING_HUMAN" => "badge warn",
        _ => "badge info"
    };

    private static string AgentLabel(string agent) => agent switch
    {
        "extraction" => "Извлечение",
        "risk" => "Риски",
        "legal" => "Юридический агент",
        "resource" => "Ресурсы",
        "estimate" => "Смета",
        "supply" => "Поставки",
        "document" => "Документы",
        "solver" => "Решатель",
        "orchestrator" => "Оркестратор",
        "workbench" => "Рабочее пространство",
        "operator" => "Оператор",
        _ => agent
    };

    private static string ReviewerLabel(string role) => role switch
    {
        "LAWYER" => DisplayText.Role("lawyer"),
        "TENDER_SPECIALIST" => DisplayText.Role("tender_specialist"),
        "SUPPLY" => DisplayText.Role("procurement"),
        "DIVISION_HEAD" => DisplayText.Role("department_head"),
        _ => DisplayText.Role(role)
    };

    private static string MessageLabel(string status) => status switch
    {
        "SENT" => "Запрос",
        "DONE" => "Готово",
        "NEEDS_REVIEW" => "Требуется проверка",
        "FAILED" => "Ошибка",
        _ => status
    };

    private static string RecommendationLabel(string value) => value switch
    {
        "PARTICIPATE" => "Участвовать",
        "CONDITIONAL" or "PARTICIPATE_WITH_CONDITIONS" => "Участвовать при выполнении условий",
        "NOT_PARTICIPATE" or "DO_NOT_PARTICIPATE" or "DECLINE" => "Не участвовать",
        "REVIEW" or "NEEDS_REVIEW" => "Требуется проверка",
        "INCOMPLETE" => "Недостаточно данных",
        "NO_SOLUTION" => "Решение не найдено",
        _ => value
    };

    private static string ConditionLabel(string condition) => condition switch
    {
        "UNCLEAR_REQUIREMENTS" => "Уточнить требования закупки",
        "RESOURCE_GAP" => "Подтвердить доступность ресурсов",
        "LOW_MARGIN" => "Довести маржу до порога",
        "PENALTY_ABOVE_THRESHOLD" => "Согласовать превышение порога неустойки",
        "NO_SOLUTION" => "Решение по заданным параметрам отсутствует",
        "FINAL_APPROVAL" => "Пройти итоговое согласование",
        _ => condition
    };

    private static string PrettyJson(JsonElement value) => JsonSerializer.Serialize(value, IndentedJson);
}
