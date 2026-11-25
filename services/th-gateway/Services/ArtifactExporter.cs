using System.Globalization;
using System.Text;
using DocumentFormat.OpenXml;
using DocumentFormat.OpenXml.Packaging;
using QuestPDF.Drawing;
using QuestPDF.Fluent;
using QuestPDF.Infrastructure;
using Qpdf = QuestPDF.Fluent;
using Ss = DocumentFormat.OpenXml.Spreadsheet;
using Word = DocumentFormat.OpenXml.Wordprocessing;
using TenderHive.Gateway.Domain;

namespace TenderHive.Gateway.Services;

/// <summary>Форматы выдачи артефактов: DOCX и PDF для документов, XLSX для расчётов.</summary>
public enum ArtifactFormat
{
    Docx,
    Xlsx,
    Pdf
}

/// <summary>Готовый к выдаче файл: содержимое, имя и тип.</summary>
public sealed record ArtifactExport(byte[] Content, string FileName, string ContentType);

/// <summary>Правила выбора формата и разбор исходного содержимого артефакта.</summary>
public static class ArtifactFormats
{
    public const string Original = "original";

    public static bool TryParse(string? value, out ArtifactFormat format)
    {
        switch ((value ?? string.Empty).Trim().ToLowerInvariant())
        {
            case "docx":
                format = ArtifactFormat.Docx;
                return true;
            case "xlsx":
                format = ArtifactFormat.Xlsx;
                return true;
            case "pdf":
                format = ArtifactFormat.Pdf;
                return true;
            default:
                format = ArtifactFormat.Docx;
                return false;
        }
    }

    /// <summary>Нормализует запрошенный формат: пустое значение — исходный файл.</summary>
    public static bool TryNormalize(string? value, out string normalized)
    {
        normalized = Original;
        var text = (value ?? string.Empty).Trim().ToLowerInvariant();
        if (text.Length == 0 || text == Original)
        {
            return true;
        }

        if (TryParse(text, out var format))
        {
            normalized = Extension(format);
            return true;
        }

        return false;
    }

    public static string Extension(ArtifactFormat format) => format switch
    {
        ArtifactFormat.Docx => "docx",
        ArtifactFormat.Xlsx => "xlsx",
        _ => "pdf"
    };

    public static string ContentType(ArtifactFormat format) => format switch
    {
        ArtifactFormat.Docx => "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ArtifactFormat.Xlsx => "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        _ => "application/pdf"
    };

    public static bool IsCsv(ArtifactRecord artifact) =>
        artifact.ContentType.Contains("csv", StringComparison.OrdinalIgnoreCase) ||
        artifact.FileName.EndsWith(".csv", StringComparison.OrdinalIgnoreCase);

    public static bool IsPdf(ArtifactRecord artifact) =>
        artifact.ContentType.Contains("pdf", StringComparison.OrdinalIgnoreCase) ||
        artifact.FileName.EndsWith(".pdf", StringComparison.OrdinalIgnoreCase);

    /// <summary>Что отдать по артефакту: документы — DOCX и PDF, расчёты — XLSX, готовый PDF — как есть.</summary>
    public static IReadOnlyList<ArtifactFormat> DeliveryPlan(ArtifactRecord artifact)
    {
        if (IsPdf(artifact))
        {
            return new[] { ArtifactFormat.Pdf };
        }

        return IsCsv(artifact)
            ? new[] { ArtifactFormat.Xlsx }
            : new[] { ArtifactFormat.Docx, ArtifactFormat.Pdf };
    }
}

/// <summary>
/// Экспорт артефактов: текст и Markdown превращаются в DOCX и PDF, CSV — в XLSX.
/// Форматы DOCX, PDF и XLSX нужны для выдачи файлами в Telegram и скачивания из дашборда.
/// </summary>
public sealed class ArtifactExporter
{
    private const string FontFamily = "Roboto";
    private const string FontAsset = "Assets/Roboto-Regular.ttf";

    private static readonly object FontGate = new();
    private static bool _fontReady;

    public ArtifactExport Export(ArtifactRecord artifact, byte[] content, ArtifactFormat format)
    {
        var text = Decode(content);
        var baseName = SafeName(Path.GetFileNameWithoutExtension(artifact.FileName)) + "-v" + artifact.Version;

        if (format == ArtifactFormat.Pdf && ArtifactFormats.IsPdf(artifact))
        {
            return new ArtifactExport(content, baseName + ".pdf", ArtifactFormats.ContentType(ArtifactFormat.Pdf));
        }

        var blocks = ArtifactFormats.IsCsv(artifact)
            ? BlocksFromCsv(text)
            : BlocksFromMarkdown(text);

        return format switch
        {
            ArtifactFormat.Docx => new ArtifactExport(
                BuildDocx(Heading(artifact), blocks),
                baseName + ".docx",
                ArtifactFormats.ContentType(ArtifactFormat.Docx)),
            ArtifactFormat.Xlsx => new ArtifactExport(
                BuildXlsx(artifact, text),
                baseName + ".xlsx",
                ArtifactFormats.ContentType(ArtifactFormat.Xlsx)),
            _ => new ArtifactExport(
                BuildPdf(Heading(artifact), blocks),
                baseName + ".pdf",
                ArtifactFormats.ContentType(ArtifactFormat.Pdf))
        };
    }

    private static string Heading(ArtifactRecord artifact) => $"TenderHive — {artifact.Kind}, версия {artifact.Version}";

    private static string Decode(byte[] content)
    {
        using var stream = new MemoryStream(content);
        using var reader = new StreamReader(stream, Encoding.UTF8, detectEncodingFromByteOrderMarks: true);
        return reader.ReadToEnd();
    }

    private static string SafeName(string name)
    {
        var builder = new StringBuilder(name.Length);
        foreach (var symbol in name)
        {
            builder.Append(Path.GetInvalidFileNameChars().Contains(symbol) ? '-' : symbol);
        }

        var result = builder.ToString().Trim();
        return result.Length == 0 ? "artifact" : result;
    }

    private enum BlockKind
    {
        Title,
        Heading,
        Text,
        Bullet
    }

    private sealed record Block(BlockKind Kind, string Text);

    private static List<Block> BlocksFromMarkdown(string text)
    {
        var blocks = new List<Block>();
        foreach (var raw in text.Replace("\r\n", "\n").Split('\n'))
        {
            var line = raw.TrimEnd();
            if (line.Length == 0)
            {
                blocks.Add(new Block(BlockKind.Text, string.Empty));
                continue;
            }

            if (line.StartsWith("# ", StringComparison.Ordinal))
            {
                blocks.Add(new Block(BlockKind.Title, line[2..].Trim()));
                continue;
            }

            if (line.StartsWith("##", StringComparison.Ordinal))
            {
                blocks.Add(new Block(BlockKind.Heading, line.TrimStart('#').Trim()));
                continue;
            }

            if (line.StartsWith("- ", StringComparison.Ordinal) || line.StartsWith("* ", StringComparison.Ordinal))
            {
                blocks.Add(new Block(BlockKind.Bullet, line[2..].Trim()));
                continue;
            }

            if (line.StartsWith('|'))
            {
                var cells = line.Trim('|').Split('|')
                    .Select(cell => cell.Trim())
                    .Where(cell => cell.Length > 0 && !cell.All(symbol => symbol is '-' or ':'))
                    .ToArray();
                if (cells.Length > 0)
                {
                    blocks.Add(new Block(BlockKind.Text, string.Join(" — ", cells)));
                }

                continue;
            }

            blocks.Add(new Block(BlockKind.Text, line.Trim()));
        }

        while (blocks.Count > 0 && blocks[^1].Text.Length == 0)
        {
            blocks.RemoveAt(blocks.Count - 1);
        }

        return blocks;
    }

    private static List<Block> BlocksFromCsv(string text)
    {
        var blocks = new List<Block>();
        foreach (var row in CsvRows(text))
        {
            blocks.Add(new Block(BlockKind.Text, string.Join(" · ", row)));
        }

        return blocks;
    }

    private static List<List<string>> CsvRows(string text)
    {
        var rows = new List<List<string>>();
        foreach (var line in text.Replace("\r\n", "\n").Split('\n'))
        {
            if (line.Trim().Length == 0)
            {
                continue;
            }

            rows.Add(SplitCsvLine(line));
        }

        return rows;
    }

    private static List<string> SplitCsvLine(string line)
    {
        var delimiter = line.Contains(';') ? ';' : ',';
        var cells = new List<string>();
        var current = new StringBuilder();
        var quoted = false;

        for (var index = 0; index < line.Length; index++)
        {
            var symbol = line[index];
            if (symbol == '"')
            {
                if (quoted && index + 1 < line.Length && line[index + 1] == '"')
                {
                    current.Append('"');
                    index++;
                }
                else
                {
                    quoted = !quoted;
                }

                continue;
            }

            if (symbol == delimiter && !quoted)
            {
                cells.Add(current.ToString().Trim());
                current.Clear();
                continue;
            }

            current.Append(symbol);
        }

        cells.Add(current.ToString().Trim());
        return cells;
    }

    private static byte[] BuildDocx(string heading, IReadOnlyList<Block> blocks)
    {
        using var stream = new MemoryStream();
        using (var document = WordprocessingDocument.Create(stream, WordprocessingDocumentType.Document))
        {
            var main = document.AddMainDocumentPart();
            main.Document = new Word.Document();
            var body = main.Document.AppendChild(new Word.Body());

            body.AppendChild(DocxParagraph(new Block(BlockKind.Title, heading), bold: true, size: 32));

            foreach (var block in blocks)
            {
                body.AppendChild(block.Kind switch
                {
                    BlockKind.Title => DocxParagraph(block, bold: true, size: 30),
                    BlockKind.Heading => DocxParagraph(block, bold: true, size: 26),
                    BlockKind.Bullet => DocxParagraph(new Block(BlockKind.Text, "• " + block.Text), bold: false, size: 22, indent: 360),
                    _ => DocxParagraph(block, bold: false, size: 22)
                });
            }

            body.AppendChild(new Word.SectionProperties(
                new Word.PageSize { Width = 11906, Height = 16838 },
                new Word.PageMargin { Top = 1134, Bottom = 1134, Left = 1134, Right = 1134 }));
        }

        return stream.ToArray();
    }

    private static Word.Paragraph DocxParagraph(Block block, bool bold, int size, int indent = 0)
    {
        var properties = new Word.ParagraphProperties(
            new Word.SpacingBetweenLines { After = "120" });
        if (indent > 0)
        {
            properties.Append(new Word.Indentation { Left = indent.ToString(CultureInfo.InvariantCulture) });
        }

        var runProperties = new Word.RunProperties(
            new Word.RunFonts { Ascii = "Segoe UI", HighAnsi = "Segoe UI" },
            new Word.FontSize { Val = size.ToString(CultureInfo.InvariantCulture) });
        if (bold)
        {
            runProperties.Append(new Word.Bold());
        }

        var run = new Word.Run(runProperties, new Word.Text(block.Text) { Space = SpaceProcessingModeValues.Preserve });
        return new Word.Paragraph(properties, run);
    }

    private static byte[] BuildXlsx(ArtifactRecord artifact, string text)
    {
        var rows = CsvRows(text);
        using var stream = new MemoryStream();
        using (var document = SpreadsheetDocument.Create(stream, SpreadsheetDocumentType.Workbook))
        {
            var workbookPart = document.AddWorkbookPart();
            workbookPart.Workbook = new Ss.Workbook();
            var worksheetPart = workbookPart.AddNewPart<WorksheetPart>();
            var sheetData = new Ss.SheetData();
            worksheetPart.Worksheet = new Ss.Worksheet(sheetData);

            var sheets = workbookPart.Workbook.AppendChild(new Ss.Sheets());
            sheets.Append(new Ss.Sheet
            {
                Id = workbookPart.GetIdOfPart(worksheetPart),
                SheetId = 1U,
                Name = SheetName(artifact.Kind)
            });

            for (var index = 0; index < rows.Count; index++)
            {
                var rowIndex = (uint)(index + 1);
                var dataRow = new Ss.Row { RowIndex = rowIndex };
                var header = index == 0;
                for (var column = 0; column < rows[index].Count; column++)
                {
                    var cell = new Ss.Cell { CellReference = ColumnName(column) + rowIndex };
                    var value = rows[index][column];
                    if (!header && decimal.TryParse(value.Replace(',', '.'), NumberStyles.Any, CultureInfo.InvariantCulture, out var number))
                    {
                        cell.CellValue = new Ss.CellValue(number.ToString(CultureInfo.InvariantCulture));
                        cell.DataType = Ss.CellValues.Number;
                    }
                    else
                    {
                        cell.DataType = Ss.CellValues.InlineString;
                        cell.InlineString = new Ss.InlineString(new Ss.Text(value) { Space = SpaceProcessingModeValues.Preserve });
                    }

                    dataRow.Append(cell);
                }

                sheetData.Append(dataRow);
            }

            workbookPart.Workbook.Save();
        }

        return stream.ToArray();
    }

    private static string SheetName(string kind)
    {
        var name = new string(kind.Where(char.IsLetterOrDigit).ToArray());
        return name.Length == 0 ? "Artifact" : name[..Math.Min(name.Length, 28)];
    }

    private static string ColumnName(int index)
    {
        var name = string.Empty;
        var value = index + 1;
        while (value > 0)
        {
            var remainder = (value - 1) % 26;
            name = (char)('A' + remainder) + name;
            value = (value - 1) / 26;
        }

        return name;
    }

    private static byte[] BuildPdf(string heading, IReadOnlyList<Block> blocks)
    {
        EnsurePdfReady();
        var document = Qpdf.Document.Create(container =>
        {
            container.Page(page =>
            {
                page.Size(QuestPDF.Helpers.PageSizes.A4);
                page.Margin(2, QuestPDF.Infrastructure.Unit.Centimetre);
                page.DefaultTextStyle(style => style.FontFamily(FontFamily).FontSize(10.5f));

                page.Header().Column(header =>
                {
                    header.Item().Text(heading).Bold().FontSize(14);
                    header.Item().PaddingTop(2).Text("TenderHive: пакет заявки").FontSize(8).FontColor(QuestPDF.Helpers.Colors.Grey.Darken1);
                });

                page.Content().PaddingVertical(10).Column(column =>
                {
                    foreach (var block in blocks)
                    {
                        switch (block.Kind)
                        {
                            case BlockKind.Title:
                                column.Item().PaddingTop(8).Text(block.Text).Bold().FontSize(12.5f);
                                break;
                            case BlockKind.Heading:
                                column.Item().PaddingTop(6).Text(block.Text).Bold().FontSize(11.5f);
                                break;
                            case BlockKind.Bullet:
                                column.Item().PaddingLeft(10).Text("• " + block.Text);
                                break;
                            default:
                                column.Item().Text(block.Text);
                                break;
                        }
                    }
                });

                page.Footer().AlignRight().Text(text =>
                {
                    text.CurrentPageNumber();
                    text.Span(" / ");
                    text.TotalPages();
                });
            });
        });

        return document.GeneratePdf();
    }

    private static void EnsurePdfReady()
    {
        if (_fontReady)
        {
            return;
        }

        lock (FontGate)
        {
            if (_fontReady)
            {
                return;
            }

            QuestPDF.Settings.License = LicenseType.Community;
            var asset = Path.Combine(AppContext.BaseDirectory, FontAsset);
            if (File.Exists(asset))
            {
                using var font = File.OpenRead(asset);
                FontManager.RegisterFontFromStream(font);
            }

            _fontReady = true;
        }
    }
}
