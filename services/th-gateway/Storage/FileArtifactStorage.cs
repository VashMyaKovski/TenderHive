namespace TenderHive.Gateway.Storage;

/// <summary>Файловое хранилище документов и артефактов (каталог из <c>ARTIFACT_BUCKET</c>).</summary>
public interface IArtifactStorage
{
    string Save(string container, Guid ownerId, string fileName, byte[] content);

    byte[]? Read(string relativePath);

    bool Exists(string relativePath);
}

public sealed class FileArtifactStorage : IArtifactStorage
{
    private readonly string _root;

    public FileArtifactStorage(string root)
    {
        _root = Path.GetFullPath(root);
        Directory.CreateDirectory(_root);
    }

    public string Save(string container, Guid ownerId, string fileName, byte[] content)
    {
        var relative = $"{container}/{ownerId:N}/{Sanitize(fileName)}";
        var fullPath = ToFullPath(relative);
        Directory.CreateDirectory(Path.GetDirectoryName(fullPath)!);
        File.WriteAllBytes(fullPath, content);
        return relative;
    }

    public byte[]? Read(string relativePath)
    {
        var fullPath = ToFullPath(relativePath);
        return File.Exists(fullPath) ? File.ReadAllBytes(fullPath) : null;
    }

    public bool Exists(string relativePath) => File.Exists(ToFullPath(relativePath));

    private string ToFullPath(string relativePath)
    {
        var normalized = relativePath.Replace('/', Path.DirectorySeparatorChar);
        var fullPath = Path.GetFullPath(Path.Combine(_root, normalized));
        if (!fullPath.StartsWith(_root, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException("Путь выходит за пределы хранилища артефактов.");
        }

        return fullPath;
    }

    private static string Sanitize(string fileName)
    {
        var invalid = Path.GetInvalidFileNameChars();
        var chars = fileName.Select(c => invalid.Contains(c) || c == '/' || c == '\\' ? '_' : c).ToArray();
        var result = new string(chars).Trim();
        return result.Length == 0 ? "file.bin" : result;
    }
}
