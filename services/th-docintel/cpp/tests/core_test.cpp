#include "docintel/core.hpp"

#include <gtest/gtest.h>

#include <array>
#include <string>
#include <utility>
#include <vector>

namespace tenderhive::docintel {
namespace {

TemplateSet test_templates() {
    return {
        "test-v1",
        {{"penalty", "penalty", true, true, 1.0, {"штраф", "пеня", "неустойка"}, {}, {}}}};
}

Block block(std::string text, double confidence = 1.0) {
    return {"00000000-0000-4000-8000-000000000001", 1, std::move(text), {0, 0, 100, 20},
            "text", confidence};
}

TEST(DocIntelCore, NormalizesRussianTextAndUnicodeSpacing) {
    EXPECT_EQ(normalize_text("  ШТРАФ\xC2\xA0  0,15 %  "), "штраф 0,15 %");
}

TEST(DocIntelCore, MatchesTemplatesAndNormalizesPercentages) {
    const auto extraction = extract_requirements(
        {block("Пеня составляет 0,15 % в день, но не более 10 %")}, test_templates(), 0.75);

    ASSERT_EQ(extraction.requirements.size(), 1);
    EXPECT_EQ(extraction.requirements[0].kind, "penalty");
    EXPECT_EQ(extraction.requirements[0].value, "0.0015");
    EXPECT_EQ(extraction.requirements[0].unit, "fraction_per_day");
    EXPECT_FALSE(extraction.requirements[0].needs_review);
    ASSERT_EQ(extraction.numbers.size(), 2);
    EXPECT_EQ(extraction.numbers[0].value, "0.0015");
    EXPECT_EQ(extraction.numbers[1].value, "0.1");
    EXPECT_EQ(extraction.numbers[1].role, "upper_bound");
}

TEST(DocIntelCore, LowOcrConfidenceRequiresReview) {
    const auto extraction = extract_requirements(
        {block("Штраф не более 100 BYN", 0.62)}, test_templates(), 0.75);

    ASSERT_EQ(extraction.requirements.size(), 1);
    EXPECT_TRUE(extraction.requirements[0].needs_review);
    EXPECT_TRUE(extraction.incomplete);
    EXPECT_EQ(extraction.status_reason, "REVIEW_REQUIRED");
}

TEST(DocIntelCore, DoesNotTreatWordSubstringsAsMatches) {
    const auto extraction = extract_requirements(
        {block("Расписание документации")}, test_templates(), 0.75);

    EXPECT_TRUE(extraction.requirements.empty());
    EXPECT_TRUE(extraction.incomplete);
    EXPECT_EQ(extraction.status_reason, "NO_REQUIREMENTS_MATCHED");
}

TEST(DocIntelCore, ParsesAndValidatesNumericDates) {
    const TemplateSet templates{"test-v1", {{"deadline", "deadline", true, false, 1.0, {"срок"}, {}, {}}}};
    const auto extraction = extract_requirements(
        {block("Срок исполнения до 12.07.2026")}, templates, 0.75);

    ASSERT_EQ(extraction.numbers.size(), 1);
    ASSERT_EQ(extraction.requirements.size(), 1);
    EXPECT_EQ(extraction.requirements[0].value, "2026-07-12");
    EXPECT_EQ(extraction.numbers[0].kind, "date");
    EXPECT_EQ(extraction.numbers[0].value, "2026-07-12");
}

TEST(DocIntelCore, AssociatesValuesWithTheirNearestRequirementTemplate) {
    const TemplateSet templates{
        "test-v1",
        {
            {"penalty", "penalty", true, true, 1.0, {"штраф"}, {}, {}},
            {"deadline", "deadline", true, true, 1.0, {"срок поставки"}, {}, {}},
        }};
    const auto extraction = extract_requirements(
        {block("Штраф 0,15 % в день; срок поставки 30 календарных дней")}, templates, 0.75);

    ASSERT_EQ(extraction.requirements.size(), 2);
    EXPECT_EQ(extraction.requirements[0].kind, "penalty");
    EXPECT_EQ(extraction.requirements[0].value, "0.0015");
    EXPECT_EQ(extraction.requirements[1].kind, "deadline");
    EXPECT_EQ(extraction.requirements[1].value, "30");
    EXPECT_EQ(extraction.requirements[1].unit, "calendar_days");
}

}  // namespace
}  // namespace tenderhive::docintel
