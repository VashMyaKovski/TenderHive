using System.Text.Json;

namespace TenderHive.Gateway.Services;

/// <summary>Owns local access grants. Run state is read directly from Workbench.</summary>
public sealed class DemoRunRegistry
{
    private const int MaxHistory = 128;
    private readonly object _lock = new();
    private readonly string _file;
    private readonly Dictionary<Guid, DemoRunAccess> _runs = [];
    private readonly Queue<Guid> _history = [];

    public DemoRunRegistry(string file)
    {
        _file = file;
        Directory.CreateDirectory(Path.GetDirectoryName(_file)!);
        if (!File.Exists(_file))
        {
            Save([]);
            return;
        }

        using var stored = File.OpenRead(_file);
        var grants = JsonSerializer.Deserialize<DemoAccessGrant[]>(stored, JsonContract.Options)
            ?? throw new InvalidOperationException("The demo access grants file is invalid.");
        foreach (var grant in grants.TakeLast(MaxHistory))
        {
            _runs.Add(grant.Id, new DemoRunAccess(grant.Owner, grant.Reviewers));
            _history.Enqueue(grant.Id);
        }
    }

    public void Add(Guid id, UserContext owner)
    {
        lock (_lock)
        {
            Save(Grants().Append(new DemoAccessGrant(id, owner, [])).TakeLast(MaxHistory));
            if (_history.Count == MaxHistory)
            {
                _runs.Remove(_history.Dequeue());
            }
            _runs.Add(id, new DemoRunAccess(owner, []));
            _history.Enqueue(id);
        }
    }

    public void RecordReviewer(Guid id, UserContext reviewer)
    {
        lock (_lock)
        {
            if (!_runs.TryGetValue(id, out var access) || access.CanReadWithoutOpenCard(reviewer))
            {
                return;
            }

            Save(Grants().Select(grant => grant.Id == id
                ? grant with { Reviewers = [.. grant.Reviewers, reviewer] }
                : grant));
            access.RecordReviewer(reviewer);
        }
    }

    public DemoRunAccess? Find(Guid id)
    {
        lock (_lock)
        {
            return _runs.GetValueOrDefault(id);
        }
    }

    public (Guid Id, DemoRunAccess Access)[] Latest()
    {
        lock (_lock)
        {
            return _history.Reverse().Select(id => (id, _runs[id])).ToArray();
        }
    }

    private IEnumerable<DemoAccessGrant> Grants() =>
        _history.Select(id => _runs[id].Snapshot(id));

    private void Save(IEnumerable<DemoAccessGrant> grants)
    {
        var temp = $"{_file}.{Guid.NewGuid():N}.tmp";
        try
        {
            var options = new FileStreamOptions { Mode = FileMode.CreateNew, Access = FileAccess.Write };
            if (!OperatingSystem.IsWindows())
            {
                options.UnixCreateMode = UnixFileMode.UserRead | UnixFileMode.UserWrite;
            }

            using (var stream = new FileStream(temp, options))
            {
                JsonSerializer.Serialize(stream, grants, JsonContract.Options);
                stream.Flush(flushToDisk: true);
            }
            File.Move(temp, _file, overwrite: true);
        }
        finally
        {
            File.Delete(temp);
        }
    }
}

internal sealed record DemoAccessGrant(Guid Id, UserContext Owner, UserContext[] Reviewers);

public sealed class DemoRunAccess(UserContext owner, IEnumerable<UserContext> reviewers)
{
    private readonly HashSet<UserContext> _reviewers = [.. reviewers];

    public SemaphoreSlim Decisions { get; } = new(1, 1);

    public bool IsOwner(UserContext actor) => owner == actor;

    public bool CanReadWithoutOpenCard(UserContext actor)
    {
        lock (_reviewers)
        {
            return IsOwner(actor) || _reviewers.Contains(actor);
        }
    }

    internal void RecordReviewer(UserContext actor)
    {
        lock (_reviewers)
        {
            _reviewers.Add(actor);
        }
    }

    internal DemoAccessGrant Snapshot(Guid id)
    {
        lock (_reviewers)
        {
            return new DemoAccessGrant(id, owner, [.. _reviewers]);
        }
    }
}
