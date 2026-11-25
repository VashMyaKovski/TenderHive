using TenderHive.Gateway.Services;

namespace TenderHive.Gateway.Tests;

public sealed class IdempotencyKeyTests
{
    private const string HashA = "aa11";
    private const string HashB = "bb22";

    [Fact]
    public void Same_bundle_produces_same_key()
    {
        var first = IdempotencyKey.Create("mail", "tender@company.by", "<42@company.by>", new[] { HashA, HashB });
        var second = IdempotencyKey.Create("mail", "tender@company.by", "<42@company.by>", new[] { HashA, HashB });

        Assert.Equal(first, second);
        Assert.Equal(64, first.Length);
    }

    [Fact]
    public void Different_message_id_produces_different_key()
    {
        var first = IdempotencyKey.Create("mail", "tender@company.by", "<42@company.by>", new[] { HashA });
        var second = IdempotencyKey.Create("mail", "tender@company.by", "<43@company.by>", new[] { HashA });

        Assert.NotEqual(first, second);
    }

    [Fact]
    public void File_order_does_not_change_key()
    {
        var first = IdempotencyKey.Create("telegram", "telegram:1", "file-1", new[] { HashA, HashB });
        var second = IdempotencyKey.Create("telegram", "telegram:1", "file-1", new[] { HashB, HashA });

        Assert.Equal(first, second);
    }

    [Fact]
    public void Different_source_produces_different_key()
    {
        var mail = IdempotencyKey.Create("mail", "telegram:1", "file-1", new[] { HashA });
        var telegram = IdempotencyKey.Create("telegram", "telegram:1", "file-1", new[] { HashA });

        Assert.NotEqual(mail, telegram);
    }
}
