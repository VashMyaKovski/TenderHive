using System.IO.Compression;
using System.Text;
using TenderHive.Gateway.Domain;
using TenderHive.Gateway.Services;

namespace TenderHive.Gateway.Tests;

public sealed class ArtifactExporterTests
{
    private readonly ArtifactExporter _exporter = new();

    private static ArtifactRecord Markdown() => new()
    {
        ArtifactId = Guid.NewGuid(),
        JobId = Guid.NewGuid(),
        Kind = "checklist",
        FileName = "checklist.md",
        ContentType = "text/markdown",
        Version = 2,
        Size = 0,
        Sha256 = string.Empty,
        StoragePath = "documents/x/checklist.md",
        CreatedAt = DateTimeOffset.UtcNow
    };

    private static ArtifactRecord Csv() => new()
    {
        ArtifactId = Guid.NewGuid(),
        JobId = Guid.NewGuid(),
        Kind = "estimate",
        FileName = "estimate.csv",
        ContentType = "text/csv",
        Version = 1,
        Size = 0,
        Sha256 = string.Empty,
        StoragePath = "documents/x/estimate.csv",
        CreatedAt = DateTimeOffset.UtcNow
    };

    [Fact]
    public void Delivery_plan_sends_documents_as_docx_and_pdf_and_estimate_as_xlsx()
    {
        Assert.Equal(new[] { ArtifactFormat.Docx, ArtifactFormat.Pdf }, ArtifactFormats.DeliveryPlan(Markdown()));
        Assert.Equal(new[] { ArtifactFormat.Xlsx }, ArtifactFormats.DeliveryPlan(Csv()));
    }

    [Fact]
    public void Docx_contains_word_document_with_russian_text()
    {
        var content = Encoding.UTF8.GetBytes("# Чек-лист требований\n- Q-01 выполнен\n- Q-02 нарушен\n");

        var export = _exporter.Export(Markdown(), content, ArtifactFormat.Docx);

        Assert.Equal("checklist-v2.docx", export.FileName);
        Assert.Equal(ArtifactFormats.ContentType(ArtifactFormat.Docx), export.ContentType);
        var xml = ReadEntry(export.Content, "word/document.xml");
        Assert.Contains("Чек-лист требований", xml);
        Assert.Contains("Q-02 нарушен", xml);
    }

    [Fact]
    public void Xlsx_contains_sheet_with_estimate_rows()
    {
        var content = Encoding.UTF8.GetBytes("позиция;количество;цена\nкоммутатор;24;4200\n");

        var export = _exporter.Export(Csv(), content, ArtifactFormat.Xlsx);

        Assert.Equal("estimate-v1.xlsx", export.FileName);
        Assert.Equal(ArtifactFormats.ContentType(ArtifactFormat.Xlsx), export.ContentType);
        var sheet = ReadEntry(export.Content, "xl/worksheets/sheet1.xml");
        Assert.Contains("коммутатор", sheet);
        Assert.Contains(">4200<", sheet);
    }

    [Fact]
    public void Pdf_export_is_a_pdf_document()
    {
        var content = Encoding.UTF8.GetBytes("# Проект заявки\nРаздел 1. Сведения о претенденте\n");

        var export = _exporter.Export(Markdown(), content, ArtifactFormat.Pdf);

        Assert.Equal("checklist-v2.pdf", export.FileName);
        Assert.Equal("application/pdf", export.ContentType);
        Assert.StartsWith("%PDF", Encoding.ASCII.GetString(export.Content, 0, 5));
        var tail = Encoding.ASCII.GetString(export.Content, export.Content.Length - 32, 32);
        Assert.Contains("%%EOF", tail);
    }

    [Fact]
    public void Unknown_format_is_rejected()
    {
        Assert.False(ArtifactFormats.TryNormalize("doc", out _));
        Assert.True(ArtifactFormats.TryNormalize(null, out var original));
        Assert.Equal(ArtifactFormats.Original, original);
        Assert.True(ArtifactFormats.TryNormalize("XLSX", out var xlsx));
        Assert.Equal("xlsx", xlsx);
    }

    private static string ReadEntry(byte[] archive, string entryName)
    {
        using var stream = new MemoryStream(archive);
        using var zip = new ZipArchive(stream, ZipArchiveMode.Read);
        var entry = zip.GetEntry(entryName) ?? throw new InvalidOperationException($"Нет части {entryName} в файле.");
        using var reader = new StreamReader(entry.Open(), Encoding.UTF8);
        return reader.ReadToEnd();
    }
}
