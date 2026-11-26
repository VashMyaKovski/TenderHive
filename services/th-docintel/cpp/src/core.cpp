#include "docintel/core.hpp"

#include <algorithm>
#include <array>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <limits>
#include <map>
#include <queue>
#include <regex>
#include <sstream>
#include <stdexcept>
#include <string_view>
#include <unordered_set>

#include <nlohmann/json.hpp>

namespace tenderhive::docintel {
namespace {

using Json = nlohmann::json;

struct CodePoint {
    std::uint32_t value;
    std::size_t length;
};

CodePoint decode_utf8(const std::string& text, std::size_t offset) {
    const auto first = static_cast<unsigned char>(text[offset]);
    if (first < 0x80U) {
        return {first, 1};
    }
    if ((first & 0xE0U) == 0xC0U && offset + 1 < text.size()) {
        const auto second = static_cast<unsigned char>(text[offset + 1]);
        return {static_cast<std::uint32_t>(((first & 0x1FU) << 6U) | (second & 0x3FU)), 2};
    }
    if ((first & 0xF0U) == 0xE0U && offset + 2 < text.size()) {
        const auto second = static_cast<unsigned char>(text[offset + 1]);
        const auto third = static_cast<unsigned char>(text[offset + 2]);
        return {
            static_cast<std::uint32_t>(
                ((first & 0x0FU) << 12U) | ((second & 0x3FU) << 6U) | (third & 0x3FU)),
            3};
    }
    if ((first & 0xF8U) == 0xF0U && offset + 3 < text.size()) {
        const auto second = static_cast<unsigned char>(text[offset + 1]);
        const auto third = static_cast<unsigned char>(text[offset + 2]);
        const auto fourth = static_cast<unsigned char>(text[offset + 3]);
        return {
            static_cast<std::uint32_t>(
                ((first & 0x07U) << 18U) | ((second & 0x3FU) << 12U) | ((third & 0x3FU) << 6U) |
                (fourth & 0x3FU)),
            4};
    }
    return {first, 1};
}

void append_utf8(std::string& target, std::uint32_t value) {
    if (value < 0x80U) {
        target.push_back(static_cast<char>(value));
    } else if (value < 0x800U) {
        target.push_back(static_cast<char>(0xC0U | (value >> 6U)));
        target.push_back(static_cast<char>(0x80U | (value & 0x3FU)));
    } else if (value < 0x10000U) {
        target.push_back(static_cast<char>(0xE0U | (value >> 12U)));
        target.push_back(static_cast<char>(0x80U | ((value >> 6U) & 0x3FU)));
        target.push_back(static_cast<char>(0x80U | (value & 0x3FU)));
    } else {
        target.push_back(static_cast<char>(0xF0U | (value >> 18U)));
        target.push_back(static_cast<char>(0x80U | ((value >> 12U) & 0x3FU)));
        target.push_back(static_cast<char>(0x80U | ((value >> 6U) & 0x3FU)));
        target.push_back(static_cast<char>(0x80U | (value & 0x3FU)));
    }
}

bool is_space(std::uint32_t value) {
    return value == 0x09U || value == 0x0AU || value == 0x0DU || value == 0x20U ||
        value == 0x00A0U || (value >= 0x2000U && value <= 0x200AU) || value == 0x202FU ||
        value == 0x3000U;
}

bool is_word_character(std::uint32_t value) {
    return (value >= 'a' && value <= 'z') || (value >= '0' && value <= '9') ||
        (value >= 0x0430U && value <= 0x044FU) || value == 0x0451U || value == '_';
}

std::string normalize_pattern(const std::string& value) {
    return normalize_text(value);
}

bool contains_any(const std::string& text, const std::vector<std::string>& patterns) {
    return std::any_of(patterns.begin(), patterns.end(), [&text](const std::string& pattern) {
        return !pattern.empty() && text.find(pattern) != std::string::npos;
    });
}

bool valid_version(const std::string& version) {
    static const std::regex version_pattern(R"(^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$)");
    return std::regex_match(version, version_pattern);
}

std::vector<std::string> read_string_array(const Json& source, const char* key) {
    if (!source.contains(key) || !source.at(key).is_array()) {
        throw std::invalid_argument("template field is missing or is not an array");
    }
    std::vector<std::string> values;
    for (const auto& value : source.at(key)) {
        if (!value.is_string()) {
            throw std::invalid_argument("template pattern must be a string");
        }
        auto normalized = normalize_pattern(value.get<std::string>());
        if (!normalized.empty()) {
            values.push_back(std::move(normalized));
        }
    }
    return values;
}

struct TrieNode {
    std::map<unsigned char, std::size_t> next;
    std::size_t failure = 0;
    std::vector<std::size_t> output;
};

class AhoCorasick {
public:
    explicit AhoCorasick(const std::vector<RequirementTemplate>& templates) {
        nodes_.emplace_back();
        for (std::size_t index = 0; index < templates.size(); ++index) {
            for (const auto& pattern : templates[index].patterns) {
                insert(pattern, index);
            }
        }
        build_failure_links();
    }

    std::vector<std::size_t> find(const std::string& text) const {
        std::unordered_set<std::size_t> matches;
        std::size_t state = 0;
        for (const auto value : text) {
            const auto character = static_cast<unsigned char>(value);
            while (state != 0 && !nodes_[state].next.contains(character)) {
                state = nodes_[state].failure;
            }
            const auto edge = nodes_[state].next.find(character);
            if (edge != nodes_[state].next.end()) {
                state = edge->second;
            }
            matches.insert(nodes_[state].output.begin(), nodes_[state].output.end());
        }
        return {matches.begin(), matches.end()};
    }

private:
    std::vector<TrieNode> nodes_;

    void insert(const std::string& pattern, std::size_t template_index) {
        std::size_t state = 0;
        for (const auto value : pattern) {
            const auto character = static_cast<unsigned char>(value);
            const auto edge = nodes_[state].next.find(character);
            if (edge == nodes_[state].next.end()) {
                const auto child = nodes_.size();
                nodes_[state].next.emplace(character, child);
                nodes_.emplace_back();
                state = child;
            } else {
                state = edge->second;
            }
        }
        nodes_[state].output.push_back(template_index);
    }

    void build_failure_links() {
        std::queue<std::size_t> pending;
        for (const auto& [character, child] : nodes_[0].next) {
            static_cast<void>(character);
            nodes_[child].failure = 0;
            pending.push(child);
        }
        while (!pending.empty()) {
            const auto state = pending.front();
            pending.pop();
            for (const auto& [character, child] : nodes_[state].next) {
                auto fallback = nodes_[state].failure;
                while (fallback != 0 && !nodes_[fallback].next.contains(character)) {
                    fallback = nodes_[fallback].failure;
                }
                const auto edge = nodes_[fallback].next.find(character);
                if (edge != nodes_[fallback].next.end() && edge->second != child) {
                    nodes_[child].failure = edge->second;
                }
                const auto& inherited = nodes_[nodes_[child].failure].output;
                nodes_[child].output.insert(nodes_[child].output.end(), inherited.begin(), inherited.end());
                pending.push(child);
            }
        }
    }
};

std::string format_decimal(double value) {
    std::ostringstream output;
    output << std::fixed << std::setprecision(8) << value;
    auto result = output.str();
    while (result.size() > 1 && result.back() == '0') {
        result.pop_back();
    }
    if (!result.empty() && result.back() == '.') {
        result.pop_back();
    }
    if (result == "-0") {
        return "0";
    }
    return result;
}

double parse_decimal(std::string value) {
    value.erase(std::remove(value.begin(), value.end(), ' '), value.end());
    const auto comma = value.rfind(',');
    const auto dot = value.rfind('.');
    if (comma != std::string::npos && dot != std::string::npos) {
        if (comma > dot) {
            value.erase(std::remove(value.begin(), value.end(), '.'), value.end());
            value[value.rfind(',')] = '.';
        } else {
            value.erase(std::remove(value.begin(), value.end(), ','), value.end());
        }
    } else if (comma != std::string::npos) {
        value[comma] = '.';
    }
    std::size_t parsed = 0;
    const auto number = std::stod(value, &parsed);
    if (parsed != value.size() || !std::isfinite(number)) {
        throw std::invalid_argument("invalid numeric value");
    }
    return number;
}

std::string unit_after(const std::string& text, std::size_t end, bool percent) {
    const auto suffix = text.substr(end, std::min<std::size_t>(40, text.size() - end));
    if (percent) {
        if (suffix.find("в день") != std::string::npos || suffix.find("за день") != std::string::npos ||
            suffix.find("ежеднев") != std::string::npos) {
            return "fraction_per_day";
        }
        return "fraction";
    }
    if (suffix.find("byn") != std::string::npos || suffix.find("руб") != std::string::npos) {
        return "BYN";
    }
    if (suffix.find("календар") != std::string::npos &&
        (suffix.find("дн") != std::string::npos || suffix.find("день") != std::string::npos)) {
        return "calendar_days";
    }
    if (suffix.find("рабоч") != std::string::npos &&
        (suffix.find("дн") != std::string::npos || suffix.find("день") != std::string::npos)) {
        return "business_days";
    }
    if (suffix.find("дн") != std::string::npos || suffix.find("день") != std::string::npos) {
        return "days";
    }
    if (suffix.find("месяц") != std::string::npos) {
        return "months";
    }
    if (suffix.find("год") != std::string::npos || suffix.find("лет") != std::string::npos) {
        return "years";
    }
    if (suffix.find("час") != std::string::npos) {
        return "hours";
    }
    return "number";
}

bool is_upper_bound(const std::string& prefix) {
    return prefix.find("не более") != std::string::npos ||
        prefix.find("не превыш") != std::string::npos || prefix.find("максимум") != std::string::npos ||
        prefix.find("предел") != std::string::npos;
}

std::string numeric_condition(const std::string& prefix) {
    if (prefix.find("не менее") != std::string::npos || prefix.find("не раньше") != std::string::npos ||
        prefix.find("минимум") != std::string::npos) {
        return "at_least";
    }
    if (is_upper_bound(prefix) || prefix.find("не позднее") != std::string::npos) {
        return "at_most";
    }
    return "exact";
}

std::vector<NumberValue> parse_numbers(const Block& block, const std::string& normalized) {
    std::vector<NumberValue> values;
    std::vector<std::pair<std::size_t, std::size_t>> date_ranges;
    static const std::regex date_pattern(
        R"((^|[^0-9])(([0-3]?[0-9])\.([01]?[0-9])\.([0-9]{4})|([0-9]{4})-([01][0-9])-([0-3][0-9]))([^0-9]|$))");
    for (auto iterator = std::sregex_iterator(normalized.begin(), normalized.end(), date_pattern);
         iterator != std::sregex_iterator(); ++iterator) {
        const auto& match = *iterator;
        int year = 0;
        int month = 0;
        int day = 0;
        if (!match[3].str().empty()) {
            day = std::stoi(match[3].str());
            month = std::stoi(match[4].str());
            year = std::stoi(match[5].str());
        } else {
            year = std::stoi(match[6].str());
            month = std::stoi(match[7].str());
            day = std::stoi(match[8].str());
        }
        const auto date_string = match[2].str();
        const auto position = static_cast<std::size_t>(match.position(2));
        const std::chrono::year_month_day date{
            std::chrono::year{year}, std::chrono::month{static_cast<unsigned int>(month)},
            std::chrono::day{static_cast<unsigned int>(day)}};
        if (!date.ok()) {
            continue;
        }
        std::ostringstream iso;
        iso << std::setfill('0') << std::setw(4) << year << '-' << std::setw(2) << month << '-'
            << std::setw(2) << day;
        values.push_back({
            iso.str(), "date", "date", "exact", "value", block.block_id, block.page, block.bbox,
            block.ocr_confidence, position});
        date_ranges.emplace_back(position, position + date_string.size());
    }

    static const std::regex number_pattern(R"(([0-9]{1,3}(?: [0-9]{3})+|[0-9]+)(?:[,.][0-9]+)?)");
    for (auto iterator = std::sregex_iterator(normalized.begin(), normalized.end(), number_pattern);
         iterator != std::sregex_iterator(); ++iterator) {
        const auto& match = *iterator;
        const auto position = static_cast<std::size_t>(match.position());
        const auto end = position + static_cast<std::size_t>(match.length());
        if (std::any_of(date_ranges.begin(), date_ranges.end(), [position](const auto& range) {
                return position >= range.first && position < range.second;
            })) {
            continue;
        }
        const auto before = position == 0 ? std::string{} : normalized.substr(0, position);
        const auto prefix = before.substr(before.size() > 40 ? before.size() - 40 : 0);
        const auto after = normalized.substr(end, std::min<std::size_t>(4, normalized.size() - end));
        const bool percent = after.find('%') != std::string::npos;
        auto numeric = parse_decimal(match.str());
        if (percent) {
            numeric /= 100.0;
        }
        values.push_back({
            format_decimal(numeric), unit_after(normalized, end, percent),
            percent ? "percent" : "number", numeric_condition(prefix),
            is_upper_bound(prefix) ? "upper_bound" : "value", block.block_id, block.page,
            block.bbox, block.ocr_confidence, position});
    }
    return values;
}

bool word_start_boundary(const std::string& text, std::size_t position) {
    if (position > 0) {
        std::size_t start = position - 1;
        while (start > 0 && (static_cast<unsigned char>(text[start]) & 0xC0U) == 0x80U) {
            --start;
        }
        if (is_word_character(decode_utf8(text, start).value)) {
            return false;
        }
    }
    return true;
}

bool pattern_matches(const std::string& text, const std::vector<std::string>& patterns) {
    for (const auto& pattern : patterns) {
        std::size_t position = text.find(pattern);
        while (position != std::string::npos) {
            if (word_start_boundary(text, position)) {
                return true;
            }
            position = text.find(pattern, position + 1);
        }
    }
    return false;
}

std::size_t template_position(
    const std::string& text, const RequirementTemplate& requirement_template) {
    auto position = std::string::npos;
    for (const auto& pattern : requirement_template.patterns) {
        auto candidate = text.find(pattern);
        while (candidate != std::string::npos) {
            if (word_start_boundary(text, candidate)) {
                position = std::min(position, candidate);
                break;
            }
            candidate = text.find(pattern, candidate + 1);
        }
    }
    return position;
}

const NumberValue* value_for_template(
    const std::vector<NumberValue>& numbers,
    const std::string& block_id,
    const RequirementTemplate& requirement_template,
    std::size_t text_position) {
    const NumberValue* closest_after = nullptr;
    const NumberValue* closest_before = nullptr;
    auto closest_after_distance = std::numeric_limits<std::size_t>::max();
    auto closest_before_distance = std::numeric_limits<std::size_t>::max();
    for (const auto& number : numbers) {
        if (number.source_block_id != block_id || number.role == "upper_bound" ||
            (number.kind == "date" && requirement_template.kind != "deadline")) {
            continue;
        }
        if (number.start_offset >= text_position) {
            const auto distance = number.start_offset - text_position;
            if (distance < closest_after_distance) {
                closest_after = &number;
                closest_after_distance = distance;
            }
        } else {
            const auto distance = text_position - number.start_offset;
            if (distance < closest_before_distance) {
                closest_before = &number;
                closest_before_distance = distance;
            }
        }
    }
    return closest_after == nullptr ? closest_before : closest_after;
}

}  // namespace

std::string normalize_text(const std::string& text) {
    std::string result;
    result.reserve(text.size());
    bool pending_space = false;
    for (std::size_t position = 0; position < text.size();) {
        auto code_point = decode_utf8(text, position);
        position += code_point.length;
        auto value = code_point.value;
        if (is_space(value)) {
            pending_space = !result.empty();
            continue;
        }
        if (value >= 'A' && value <= 'Z') {
            value += 'a' - 'A';
        } else if (value >= 0x0410U && value <= 0x042FU) {
            value += 0x20U;
        } else if (value == 0x0401U) {
            value = 0x0451U;
        } else if (value == 0x0451U) {
            value = 0x0435U;
        } else if (value >= 0x2010U && value <= 0x2015U) {
            value = '-';
        }
        if (pending_space) {
            result.push_back(' ');
            pending_space = false;
        }
        append_utf8(result, value);
    }
    return result;
}

TemplateSet load_template_set(const std::filesystem::path& directory, const std::string& version) {
    if (!valid_version(version)) {
        throw std::invalid_argument("invalid template set version");
    }
    const auto path = directory / (version + ".json");
    std::ifstream input(path);
    if (!input) {
        throw std::out_of_range("template set not found");
    }
    const auto source = Json::parse(input);
    if (!source.is_object() || !source.contains("version") || !source.at("version").is_string() ||
        source.at("version").get<std::string>() != version || !source.contains("templates") ||
        !source.at("templates").is_array()) {
        throw std::invalid_argument("template set does not match its version");
    }

    TemplateSet result{version, {}};
    std::unordered_set<std::string> ids;
    for (const auto& item : source.at("templates")) {
        if (!item.is_object()) {
            throw std::invalid_argument("template must be an object");
        }
        RequirementTemplate entry{
            item.at("id").get<std::string>(),
            item.at("kind").get<std::string>(),
            item.at("mandatory").get<bool>(),
            item.at("requires_value").get<bool>(),
            item.at("confidence").get<double>(),
            read_string_array(item, "patterns"),
            read_string_array(item, "exclude_patterns"),
            read_string_array(item, "context_patterns")};
        if (entry.id.empty() || entry.kind.empty() || entry.patterns.empty() ||
            !std::isfinite(entry.confidence) || entry.confidence < 0.0 || entry.confidence > 1.0 ||
            !ids.insert(entry.id).second) {
            throw std::invalid_argument("template definition is invalid");
        }
        result.templates.push_back(std::move(entry));
    }
    if (result.templates.empty()) {
        throw std::invalid_argument("template set is empty");
    }
    return result;
}

Extraction extract_requirements(
    const std::vector<Block>& blocks,
    const TemplateSet& template_set,
    double confidence_floor) {
    if (!std::isfinite(confidence_floor) || confidence_floor < 0.0 || confidence_floor > 1.0) {
        throw std::invalid_argument("confidence floor is outside [0, 1]");
    }
    if (blocks.empty()) {
        return {{}, {}, true, "NO_TEXT_BLOCKS"};
    }

    AhoCorasick matcher(template_set.templates);
    Extraction result{{}, {}, false, {}};
    for (const auto& block : blocks) {
        if (block.block_id.empty() || block.page == 0 || block.text.empty() ||
            !std::isfinite(block.ocr_confidence) || block.ocr_confidence < 0.0 ||
            block.ocr_confidence > 1.0) {
            throw std::invalid_argument("document block is invalid");
        }
        const auto normalized = normalize_text(block.text);
        auto numbers = parse_numbers(block, normalized);
        result.numbers.insert(result.numbers.end(), numbers.begin(), numbers.end());

        auto matches = matcher.find(normalized);
        std::sort(matches.begin(), matches.end());
        for (const auto template_index : matches) {
            const auto& entry = template_set.templates[template_index];
            if (!pattern_matches(normalized, entry.patterns) ||
                contains_any(normalized, entry.exclude_patterns) ||
                (!entry.context_patterns.empty() &&
                 !contains_any(normalized, entry.context_patterns))) {
                continue;
            }
            const auto confidence = std::min(block.ocr_confidence, entry.confidence);
            const auto offset = template_position(normalized, entry);
            const auto* number = value_for_template(numbers, block.block_id, entry, offset);
            const auto value = number == nullptr ? std::string{} : number->value;
            const auto unit = number == nullptr ? std::string{} : number->unit;
            const auto needs_review = confidence < confidence_floor || (entry.requires_value && value.empty());
            result.requirements.push_back({
                entry.id, normalized, entry.kind, entry.mandatory, value,
                unit, block.block_id, block.page, block.bbox,
                confidence, needs_review});
            result.incomplete = result.incomplete || needs_review;
        }
    }
    if (result.requirements.empty()) {
        result.incomplete = true;
        result.status_reason = "NO_REQUIREMENTS_MATCHED";
    } else if (result.incomplete) {
        result.status_reason = "REVIEW_REQUIRED";
    }
    return result;
}

}  // namespace tenderhive::docintel
