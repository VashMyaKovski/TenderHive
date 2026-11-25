using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Security.Cryptography;
using TenderHive.Contracts;
using TenderHive.Gateway.Services;

namespace TenderHive.Gateway.Tests;

public sealed class DemoRunsTests
{
    private static readonly UserContext Owner = new("specialist", AppRole.TenderSpecialist);
    private static readonly UserContext Reviewer = new("lawyer", AppRole.Lawyer);
    private static readonly UserContext Stranger = new("other-specialist", AppRole.TenderSpecialist);

    [Fact]
    public async Task Only_owner_and_assigned_reviewer_can_read_an_open_run()
    {
        using var fixture = new DemoFixture();

        var ownerView = await fixture.Runs.GetAsync(fixture.Run.Id, Owner, CancellationToken.None);
        var reviewerView = await fixture.Runs.GetAsync(fixture.Run.Id, Reviewer, CancellationToken.None);
        var strangerError = await Assert.ThrowsAsync<ApiException>(
            () => fixture.Runs.GetAsync(fixture.Run.Id, Stranger, CancellationToken.None));
        var wrongRole = await Assert.ThrowsAsync<ApiException>(() => fixture.Runs.DecideAsync(
            fixture.Run.Id, new DemoDecisionInput(fixture.Run.Card!.Id, "APPROVED"), Owner, CancellationToken.None));

        Assert.False(ownerView.Card!.CanDecide);
        Assert.True(reviewerView.Card!.CanDecide);
        Assert.Equal(404, strangerError.StatusCode);
        Assert.Equal(403, wrongRole.StatusCode);
        Assert.Empty(await fixture.Runs.ListAsync(Stranger, CancellationToken.None));
        Assert.Equal(0, fixture.DecisionCount);
    }

    [Fact]
    public async Task Stale_and_repeated_decisions_conflict_and_only_one_reaches_workbench()
    {
        using var fixture = new DemoFixture();
        var decision = new DemoDecisionInput(fixture.Run.Card!.Id, "APPROVED");

        var stale = await Assert.ThrowsAsync<ApiException>(() => fixture.Runs.DecideAsync(
            fixture.Run.Id, decision with { CardId = Guid.NewGuid() }, Reviewer, CancellationToken.None));
        await fixture.Runs.DecideAsync(fixture.Run.Id, decision, Reviewer, CancellationToken.None);
        var duplicate = await Assert.ThrowsAsync<ApiException>(() => fixture.Runs.DecideAsync(
            fixture.Run.Id, decision, Reviewer, CancellationToken.None));

        Assert.Equal(409, stale.StatusCode);
        Assert.Equal(409, duplicate.StatusCode);
        Assert.Equal(1, fixture.DecisionCount);
        Assert.Null((await fixture.Runs.GetAsync(fixture.Run.Id, Reviewer, CancellationToken.None)).Card);
    }

    [Fact]
    public async Task Only_done_runs_yield_the_original_files_to_the_owner()
    {
        using var fixture = new DemoFixture();
        var early = await Assert.ThrowsAsync<ApiException>(() => fixture.Runs.DownloadAsync(
            fixture.Run.Id, "offer.pdf", Owner, CancellationToken.None));
        Assert.Equal(404, early.StatusCode);
        Assert.Equal(0, fixture.DownloadCount);

        fixture.CompleteRun();
        var hidden = await Assert.ThrowsAsync<ApiException>(() => fixture.Runs.DownloadAsync(
            fixture.Run.Id, "offer.pdf", Reviewer, CancellationToken.None));
        Assert.Equal(404, hidden.StatusCode);
        Assert.Equal(0, fixture.DownloadCount);

        var run = await fixture.Runs.GetAsync(fixture.Run.Id, Owner, CancellationToken.None);
        Assert.Equal($"/api/demo/runs/{run.Id:D}/artifacts/offer.pdf", Assert.Single(run.Artifacts).Url);
        var file = await fixture.Runs.DownloadAsync(run.Id, "offer.pdf", Owner, CancellationToken.None);
        Assert.Equal(fixture.FileBytes, file.Bytes);
        Assert.Equal(Convert.ToHexStringLower(SHA256.HashData(fixture.FileBytes)), file.Sha256);

        fixture.SendInvalidHash = true;
        var badHash = await Assert.ThrowsAsync<ApiException>(() => fixture.Runs.DownloadAsync(
            fixture.Run.Id, "offer.pdf", Owner, CancellationToken.None));
        Assert.Equal(502, badHash.StatusCode);
    }

    [Fact]
    public async Task Missing_workbench_run_is_expired_only_for_accounts_with_a_grant()
    {
        using var fixture = new DemoFixture { RunExpired = true };

        var expired = await Assert.ThrowsAsync<ApiException>(
            () => fixture.Runs.GetAsync(fixture.Run.Id, Owner, CancellationToken.None));
        var unknown = await Assert.ThrowsAsync<ApiException>(
            () => fixture.Runs.GetAsync(fixture.Run.Id, Stranger, CancellationToken.None));

        Assert.Equal(410, expired.StatusCode);
        Assert.Equal(404, unknown.StatusCode);
        Assert.Empty(await fixture.Runs.ListAsync(Owner, CancellationToken.None));
    }

    [Fact]
    public async Task Gateway_restart_retains_owner_and_reviewers_for_live_workbench_runs()
    {
        using var fixture = new DemoFixture();
        var decision = new DemoDecisionInput(fixture.Run.Card!.Id, "APPROVED");
        await fixture.Runs.DecideAsync(fixture.Run.Id, decision, Reviewer, CancellationToken.None);

        var restartedGateway = fixture.BuildGateway();
        Assert.Equal(fixture.Run.Id, (await restartedGateway.GetAsync(
            fixture.Run.Id, Owner, CancellationToken.None)).Id);
        Assert.Equal(fixture.Run.Id, (await restartedGateway.GetAsync(
            fixture.Run.Id, Reviewer, CancellationToken.None)).Id);
        var unknown = await Assert.ThrowsAsync<ApiException>(
            () => restartedGateway.GetAsync(fixture.Run.Id, Stranger, CancellationToken.None));
        Assert.Equal(404, unknown.StatusCode);
    }

    private sealed class DemoFixture : HttpMessageHandler
    {
        private readonly HttpClient _http;
        private readonly DirectoryInfo _temp = Directory.CreateTempSubdirectory();
        private string _grantsFile => Path.Combine(_temp.FullName, "grants.json");
        public DemoRuns Runs { get; }
        public DemoRunDto Run { get; private set; }
        public byte[] FileBytes { get; } = "%PDF-test"u8.ToArray();
        public bool SendInvalidHash { get; set; }
        public bool RunExpired { get; init; }
        public int DecisionCount { get; private set; }
        public int DownloadCount { get; private set; }

        public DemoFixture()
        {
            Run = new DemoRunDto(Guid.NewGuid(), "Fixture", "WAITING_HUMAN", "waiting",
                Guid.NewGuid(), [], [], null, new DemoCardDto(Guid.NewGuid(), "review", "LAWYER", "Approve?"),
                [new DemoArtifactDto("offer.pdf", "/internal/file")], null);
            var registry = new DemoRunRegistry(_grantsFile);
            registry.Add(Run.Id, Owner);
            _http = new HttpClient(this, disposeHandler: false) { BaseAddress = new Uri("http://workbench.test") };
            Runs = new DemoRuns(new DemoWorkbenchClient(_http), registry);
        }

        public DemoRuns BuildGateway() => new(new DemoWorkbenchClient(_http), new DemoRunRegistry(_grantsFile));

        public void CompleteRun() => Run = Run with { Card = null, Status = "DONE", Phase = "done" };

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            var path = request.RequestUri!.AbsolutePath;
            if (RunExpired)
            {
                return new HttpResponseMessage(HttpStatusCode.NotFound);
            }

            if (path == $"/api/workbench/runs/{Run.Id:D}/decision")
            {
                var sent = await request.Content!.ReadFromJsonAsync<DemoDecisionInput>(JsonContract.Options, ct);
                Assert.Equal(Run.Card?.Id, sent?.CardId);
                DecisionCount++;
                Run = Run with { Card = null, Phase = "running" };
            }
            else if (path == $"/api/workbench/runs/{Run.Id:D}/artifacts/offer.pdf")
            {
                DownloadCount++;
                var file = new ByteArrayContent(FileBytes);
                file.Headers.ContentType = new MediaTypeHeaderValue("application/pdf");
                var response = new HttpResponseMessage(HttpStatusCode.OK) { Content = file };
                response.Headers.Add("X-Artifact-SHA256", SendInvalidHash
                    ? new string('0', 64)
                    : Convert.ToHexStringLower(SHA256.HashData(FileBytes)));
                return response;
            }
            else
            {
                Assert.Equal($"/api/workbench/runs/{Run.Id:D}", path);
            }

            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = JsonContent.Create(Run, options: JsonContract.Options)
            };
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                _http.Dispose();
                _temp.Delete(recursive: true);
            }
            base.Dispose(disposing);
        }
    }
}
