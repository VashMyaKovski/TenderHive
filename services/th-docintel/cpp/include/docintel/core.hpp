#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <string>
#include <vector>

namespace tenderhive::docintel {

struct Block {
    std::string block_id;
    std::uint32_t page;
    std::string text;
    std::array<double, 4> bbox;
    std::string kind;
    double ocr_confidence;
};

struct RequirementTemplate {
    std::string id;
    std::string kind;
    bool mandatory;
    bool requires_value;
    double confidence;
    std::vector<std::string> patterns;
    std::vector<std::string> exclude_patterns;
    std::vector<std::string> context_patterns;
};

struct TemplateSet {
    std::string version;
    std::vector<RequirementTemplate> templates;
};

struct NumberValue {
    std::string value;
    std::string unit;
    std::string kind;
    std::string condition;
    std::string role;
    std::string source_block_id;
    std::uint32_t page;
    std::array<double, 4> bbox;
    double confidence;
    std::size_t start_offset;
};

struct Requirement {
    std::string template_id;
    std::string text;
    std::string kind;
    bool mandatory;
    std::string value;
    std::string unit;
    std::string source_block_id;
    std::uint32_t page;
    std::array<double, 4> bbox;
    double confidence;
    bool needs_review;
};

struct Extraction {
    std::vector<Requirement> requirements;
    std::vector<NumberValue> numbers;
    bool incomplete;
    std::string status_reason;
};

std::string normalize_text(const std::string& text);
TemplateSet load_template_set(const std::filesystem::path& directory, const std::string& version);
Extraction extract_requirements(
    const std::vector<Block>& blocks,
    const TemplateSet& template_set,
    double confidence_floor);

}  // namespace tenderhive::docintel
