namespace TenderHive.Gateway.Configuration;

/// <summary>
/// Минимальный загрузчик <c>.env</c>: значения попадают в переменные окружения и читаются
/// штатным <c>AddEnvironmentVariables</c>. Уже заданные переменные не перетираются.
/// </summary>
public static class EnvFile
{
    public static void Load(params string[] startDirectories)
    {
        var path = Locate(startDirectories);
        if (path is null)
        {
            return;
        }

        foreach (var raw in File.ReadAllLines(path))
        {
            var line = raw.Trim();
            if (line.Length == 0 || line.StartsWith('#'))
            {
                continue;
            }

            var separator = line.IndexOf('=');
            if (separator <= 0)
            {
                continue;
            }

            var key = line[..separator].Trim();
            var value = line[(separator + 1)..].Trim();
            if (value.Length >= 2 &&
                ((value[0] == '"' && value[^1] == '"') || (value[0] == '\'' && value[^1] == '\'')))
            {
                value = value[1..^1];
            }

            if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable(key)))
            {
                Environment.SetEnvironmentVariable(key, value);
            }
        }
    }

    private static string? Locate(IEnumerable<string> startDirectories)
    {
        foreach (var start in startDirectories)
        {
            var directory = new DirectoryInfo(start);
            for (var depth = 0; depth < 6 && directory is not null; depth++)
            {
                var candidate = Path.Combine(directory.FullName, ".env");
                if (File.Exists(candidate))
                {
                    return candidate;
                }

                directory = directory.Parent;
            }
        }

        return null;
    }
}
