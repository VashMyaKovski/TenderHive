using TenderHive.Contracts;

namespace TenderHive.Gateway.Services;

public sealed class DemoRuns(DemoWorkbenchClient workbench, DemoRunRegistry registry)
{
    public async Task<DemoRunDto> StartAsync(DemoRunInput input, UserContext actor, CancellationToken ct)
    {
        if (actor.Role is not (AppRole.TenderSpecialist or AppRole.Procurement or AppRole.DepartmentHead))
        {
            throw new ApiException(403, "Эта роль не может запускать анализ.");
        }

        var run = await workbench.StartAsync(input, ct);
        registry.Add(run.Id, actor);
        return Present(run, actor, owner: true);
    }

    public async Task<IReadOnlyList<DemoRunDto>> ListAsync(UserContext actor, CancellationToken ct)
    {
        var result = new List<DemoRunDto>();
        foreach (var (id, access) in registry.Latest())
        {
            var run = await workbench.GetAsync(id, ct);
            if (run is not null && CanRead(run, access, actor))
            {
                result.Add(Present(run, actor, access.IsOwner(actor)));
            }
        }
        return result;
    }

    public async Task<DemoRunDto> GetAsync(Guid id, UserContext actor, CancellationToken ct)
    {
        var access = Find(id);
        var run = await GetVisibleAsync(id, access, actor, ct);
        return Present(run, actor, access.IsOwner(actor));
    }

    public async Task<DemoRunDto> DecideAsync(Guid id, DemoDecisionInput input, UserContext actor, CancellationToken ct)
    {
        if (input.Decision is not ("APPROVED" or "REJECTED") || input.CardId == Guid.Empty ||
            input.Comment?.Length > 1000 ||
            (input.Decision == "REJECTED" && string.IsNullOrWhiteSpace(input.Comment)))
        {
            throw new ApiException(422, "Укажите карточку, решение и причину отклонения.");
        }

        var access = Find(id);
        await access.Decisions.WaitAsync(ct);
        try
        {
            var run = await GetVisibleAsync(id, access, actor, ct);
            if (run.Card is null || run.Card.Id != input.CardId)
            {
                throw new ApiException(409, "Карточка уже изменилась или закрыта.");
            }
            if (!CanDecide(run.Card, actor))
            {
                throw new ApiException(403, "Эта карточка назначена другой роли.");
            }

            var result = await workbench.DecideAsync(id, input with { Comment = input.Comment ?? "" }, ct);
            registry.RecordReviewer(id, actor);
            return Present(result, actor, access.IsOwner(actor));
        }
        finally
        {
            access.Decisions.Release();
        }
    }

    public async Task<DemoDownload> DownloadAsync(Guid id, string name, UserContext actor, CancellationToken ct)
    {
        var access = Find(id);
        if (!access.IsOwner(actor))
        {
            throw new ApiException(404, "Файл комплекта не найден.");
        }
        var run = await GetVisibleAsync(id, access, actor, ct);
        if (run.Status != "DONE" || run.Phase != "done" || DemoWorkbenchClient.ArtifactContentType(name) is null ||
            !run.Artifacts.Any(artifact => artifact.Name == name))
        {
            throw new ApiException(404, "Файл комплекта пока недоступен.");
        }
        return await workbench.DownloadAsync(id, name, ct);
    }

    private DemoRunAccess Find(Guid id) => registry.Find(id)
        ?? throw new ApiException(404, "Прогон не найден.");

    private async Task<DemoRunDto> GetVisibleAsync(Guid id, DemoRunAccess access, UserContext actor, CancellationToken ct)
    {
        var run = await workbench.GetAsync(id, ct);
        if (run is null && access.CanReadWithoutOpenCard(actor))
        {
            throw new ApiException(410, "Прогон истёк. Запустите анализ снова.");
        }
        if (run is null || !CanRead(run, access, actor))
        {
            throw new ApiException(404, "Прогон не найден.");
        }
        return run;
    }

    private static bool CanRead(DemoRunDto run, DemoRunAccess access, UserContext actor) =>
        access.CanReadWithoutOpenCard(actor) || CanDecide(run.Card, actor);

    private static bool CanDecide(DemoCardDto? card, UserContext actor) =>
        card?.Role switch
        {
            "TENDER_SPECIALIST" => actor.Role == AppRole.TenderSpecialist,
            "SUPPLY" => actor.Role == AppRole.Procurement,
            "LAWYER" => actor.Role == AppRole.Lawyer,
            "DIVISION_HEAD" => actor.Role == AppRole.DepartmentHead,
            _ => false
        };

    private static DemoRunDto Present(DemoRunDto run, UserContext actor, bool owner) => run with
    {
        Card = run.Card is null ? null : run.Card with { CanDecide = CanDecide(run.Card, actor) },
        Artifacts = owner && run is { Status: "DONE", Phase: "done" }
            ? run.Artifacts.Where(file => DemoWorkbenchClient.ArtifactContentType(file.Name) is not null)
                .Select(file => file with { Url = $"/api/demo/runs/{run.Id:D}/artifacts/{file.Name}" }).ToArray()
            : []
    };
}
