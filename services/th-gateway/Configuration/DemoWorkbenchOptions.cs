namespace TenderHive.Gateway.Configuration;

public sealed class DemoWorkbenchOptions
{
    private DemoWorkbenchOptions(Uri origin, string token, string grantsFile)
    {
        Origin = origin;
        Token = token;
        GrantsFile = grantsFile;
    }

    public Uri Origin { get; }
    public string Token { get; }
    public string GrantsFile { get; }

    public static DemoWorkbenchOptions? FromConfiguration(IConfiguration configuration)
    {
        var url = configuration["DEMO_WORKBENCH_URL"];
        var token = configuration["DEMO_WORKBENCH_TOKEN"];
        var grantsFile = configuration["DEMO_ACCESS_GRANTS_FILE"];
        if (string.IsNullOrWhiteSpace(url) && string.IsNullOrWhiteSpace(token) && string.IsNullOrWhiteSpace(grantsFile))
        {
            return null;
        }

        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri) ||
            uri.Scheme is not ("http" or "https") ||
            uri.AbsolutePath != "/" ||
            uri.UserInfo.Length != 0 ||
            uri.Query.Length != 0 ||
            uri.Fragment.Length != 0)
        {
            throw new InvalidOperationException("DEMO_WORKBENCH_URL must be an HTTP service origin.");
        }

        if (token is null || token.Length < 32 || token.Any(character => character is <= ' ' or >= '\u007f'))
        {
            throw new InvalidOperationException("DEMO_WORKBENCH_TOKEN must contain at least 32 visible ASCII characters.");
        }

        if (string.IsNullOrWhiteSpace(grantsFile) || !Path.IsPathFullyQualified(grantsFile))
        {
            throw new InvalidOperationException("DEMO_ACCESS_GRANTS_FILE must be an absolute file path.");
        }

        return new DemoWorkbenchOptions(uri, token, Path.GetFullPath(grantsFile));
    }
}
