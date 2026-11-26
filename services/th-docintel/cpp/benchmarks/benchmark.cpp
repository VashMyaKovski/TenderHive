#include "docintel/core.hpp"

#include <sys/resource.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdlib>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {

using tenderhive::docintel::Block;
using tenderhive::docintel::RequirementTemplate;
using tenderhive::docintel::TemplateSet;

std::string required_environment(const char* name) {
    const auto* value = std::getenv(name);
    if (value == nullptr || *value == '\0') {
        throw std::runtime_error(std::string(name) + " is required");
    }
    return value;
}

std::size_t environment_size(const char* name) {
    const auto value = required_environment(name);
    std::size_t parsed = 0;
    const auto result = std::stoull(value, &parsed);
    if (parsed != value.size() || result == 0) {
        throw std::runtime_error(std::string(name) + " must be a positive integer");
    }
    return static_cast<std::size_t>(result);
}

double environment_fraction(const char* name) {
    const auto value = required_environment(name);
    std::size_t parsed = 0;
    const auto result = std::stod(value, &parsed);
    if (parsed != value.size() || !std::isfinite(result) || result < 0.0 || result > 1.0) {
        throw std::runtime_error(std::string(name) + " must be between 0 and 1");
    }
    return result;
}

std::vector<std::size_t> multipliers() {
    std::istringstream values(required_environment("DOCINTEL_BENCHMARK_TEMPLATE_MULTIPLIERS"));
    std::vector<std::size_t> result;
    std::string value;
    while (std::getline(values, value, ',')) {
        std::size_t parsed = 0;
        const auto multiplier = std::stoull(value, &parsed);
        if (parsed != value.size() || multiplier == 0) {
            throw std::runtime_error("benchmark template multipliers must be positive integers");
        }
        result.push_back(static_cast<std::size_t>(multiplier));
    }
    if (result.empty()) {
        throw std::runtime_error("at least one template multiplier is required");
    }
    return result;
}

std::size_t peak_memory_kb() {
    rusage usage{};
    if (getrusage(RUSAGE_SELF, &usage) != 0) {
        throw std::runtime_error("unable to read process memory usage");
    }
    return static_cast<std::size_t>(usage.ru_maxrss);
}

TemplateSet scale_templates(const TemplateSet& base, std::size_t multiplier) {
    TemplateSet result = base;
    result.templates.reserve(base.templates.size() * multiplier);
    for (std::size_t copy_index = 1; copy_index < multiplier; ++copy_index) {
        for (const auto& original : base.templates) {
            auto duplicate = original;
            duplicate.id += "_benchmark_" + std::to_string(copy_index);
            for (auto& pattern : duplicate.patterns) {
                pattern += " benchmarkvariant" + std::to_string(copy_index);
            }
            result.templates.push_back(std::move(duplicate));
        }
    }
    return result;
}

std::vector<Block> workload(const std::string& text, std::size_t block_count) {
    std::vector<Block> blocks;
    blocks.reserve(block_count);
    for (std::size_t index = 0; index < block_count; ++index) {
        blocks.push_back({
            "00000000-0000-4000-8000-000000000001",
            static_cast<std::uint32_t>(index + 1),
            text,
            {0.0, static_cast<double>(index), 400.0, static_cast<double>(index + 1)},
            "text",
            1.0});
    }
    return blocks;
}

}  // namespace

int main() {
    try {
        const auto templates = tenderhive::docintel::load_template_set(
            required_environment("DOCINTEL_TEMPLATE_DIR"),
            required_environment("DOCINTEL_TEMPLATE_SET_VERSION"));
        const auto confidence_floor = environment_fraction("DOCINTEL_CONFIDENCE_FLOOR");
        const auto iterations = environment_size("DOCINTEL_BENCHMARK_ITERATIONS");
        const auto block_count = environment_size("DOCINTEL_BENCHMARK_BLOCKS");
        const auto text_repeats = environment_size("DOCINTEL_BENCHMARK_TEXT_REPEATS");
        const auto sample = required_environment("DOCINTEL_BENCHMARK_TEXT");
        std::string text;
        for (std::size_t index = 0; index < text_repeats; ++index) {
            text += sample;
            text.push_back(' ');
        }
        const auto blocks = workload(text, block_count);

        std::cout << "template_count,block_count,text_bytes,iterations,elapsed_ms,peak_rss_kb\n";
        for (const auto multiplier : multipliers()) {
            const auto scaled = scale_templates(templates, multiplier);
            const auto start = std::chrono::steady_clock::now();
            for (std::size_t iteration = 0; iteration < iterations; ++iteration) {
                static_cast<void>(tenderhive::docintel::extract_requirements(
                    blocks, scaled, confidence_floor));
            }
            const auto elapsed = std::chrono::duration<double, std::milli>(
                std::chrono::steady_clock::now() - start);
            std::cout << scaled.templates.size() << ',' << blocks.size() << ',' << text.size() << ','
                      << iterations << ',' << elapsed.count() << ',' << peak_memory_kb() << '\n';
        }
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "benchmark configuration error: " << error.what() << '\n';
        return 2;
    }
}
