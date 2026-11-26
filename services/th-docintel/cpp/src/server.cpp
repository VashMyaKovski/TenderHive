#include "docintel/core.hpp"

#include <grpcpp/grpcpp.h>

#include <array>
#include <chrono>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <limits>
#include <memory>
#include <mutex>
#include <regex>
#include <stdexcept>
#include <string>
#include <system_error>
#include <unordered_map>
#include <utility>
#include <vector>

#include "docintel.grpc.pb.h"

namespace {

namespace wire = tenderhive::docintel::v1;
using tenderhive::docintel::Block;

std::string required_environment(const char* name) {
    const auto* value = std::getenv(name);
    if (value == nullptr || *value == '\0') {
        throw std::runtime_error(std::string(name) + " is required");
    }
    return value;
}

int environment_integer(const char* name) {
    const auto value = required_environment(name);
    std::size_t parsed = 0;
    const auto result = std::stoi(value, &parsed);
    if (parsed != value.size() || result <= 0) {
        throw std::runtime_error(std::string(name) + " must be a positive integer");
    }
    return result;
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

bool environment_boolean(const char* name) {
    auto value = required_environment(name);
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char character) {
        return static_cast<char>(std::tolower(character));
    });
    if (value == "true") {
        return true;
    }
    if (value == "false") {
        return false;
    }
    throw std::runtime_error(std::string(name) + " must be true or false");
}

bool is_uuid(const std::string& value) {
    static const std::regex pattern(
        R"(^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$)");
    return std::regex_match(value, pattern);
}

bool is_template_version(const std::string& value) {
    static const std::regex pattern(R"(^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$)");
    return std::regex_match(value, pattern);
}

std::array<double, 4> bbox_from_wire(const wire::BoundingBox& bbox) {
    return {bbox.x0(), bbox.y0(), bbox.x1(), bbox.y1()};
}

void bbox_to_wire(const std::array<double, 4>& source, wire::BoundingBox* target) {
    target->set_x0(source[0]);
    target->set_y0(source[1]);
    target->set_x1(source[2]);
    target->set_y1(source[3]);
}

class DocIntelService final : public wire::DocIntel::Service {
public:
    DocIntelService(
        std::string template_directory,
        double confidence_floor,
        bool cache_templates,
        std::size_t template_cache_entries)
        : template_directory_(std::move(template_directory)),
          confidence_floor_(confidence_floor),
          cache_templates_(cache_templates),
          template_cache_entries_(template_cache_entries) {}

    grpc::Status CheckHealth(
        grpc::ServerContext*, const wire::HealthRequest*, wire::HealthResponse* response) override {
        response->set_ready(true);
        return grpc::Status::OK;
    }

    grpc::Status ExtractRequirements(
        grpc::ServerContext*, const wire::ExtractRequirementsRequest* request,
        wire::ExtractRequirementsResponse* response) override {
        if (!is_uuid(request->job_id()) || !is_uuid(request->job_doc_id())) {
            return {grpc::StatusCode::INVALID_ARGUMENT, "job and document identifiers must be UUIDs"};
        }
        if (request->blocks().empty()) {
            return {grpc::StatusCode::INVALID_ARGUMENT, "at least one document block is required"};
        }
        if (request->template_set_version().empty()) {
            return {grpc::StatusCode::INVALID_ARGUMENT, "template set version is required"};
        }

        std::vector<Block> blocks;
        blocks.reserve(static_cast<std::size_t>(request->blocks_size()));
        for (const auto& source : request->blocks()) {
            if (source.bbox().x1() < source.bbox().x0() || source.bbox().y1() < source.bbox().y0()) {
                return {grpc::StatusCode::INVALID_ARGUMENT, "block coordinates are invalid"};
            }
            std::string kind;
            switch (source.kind()) {
                case wire::BLOCK_KIND_TEXT:
                    kind = "text";
                    break;
                case wire::BLOCK_KIND_TABLE_CELL:
                    kind = "table_cell";
                    break;
                case wire::BLOCK_KIND_TABLE_ROW:
                    kind = "table_row";
                    break;
                default:
                    return {grpc::StatusCode::INVALID_ARGUMENT, "block kind is invalid"};
            }
            blocks.push_back({
                source.block_id(), source.page(), source.text(), bbox_from_wire(source.bbox()),
                std::move(kind), source.ocr_confidence()});
        }

        try {
            const auto templates = templates_for(request->template_set_version());
            const auto extraction = tenderhive::docintel::extract_requirements(
                blocks, templates, confidence_floor_);
            response->set_job_id(request->job_id());
            for (const auto& requirement : extraction.requirements) {
                auto* target = response->add_requirements();
                target->set_template_id(requirement.template_id);
                target->set_text(requirement.text);
                target->set_kind(requirement.kind);
                target->set_mandatory(requirement.mandatory);
                target->set_value(requirement.value);
                target->set_unit(requirement.unit);
                target->set_source_block_id(requirement.source_block_id);
                target->set_page(requirement.page);
                bbox_to_wire(requirement.bbox, target->mutable_bbox());
                target->set_confidence(requirement.confidence);
                target->set_needs_review(requirement.needs_review);
            }
            for (const auto& number : extraction.numbers) {
                auto* target = response->add_numbers();
                target->set_value(number.value);
                target->set_unit(number.unit);
                target->set_kind(number.kind);
                target->set_condition(number.condition);
                target->set_role(number.role);
                target->set_source_block_id(number.source_block_id);
                target->set_page(number.page);
                bbox_to_wire(number.bbox, target->mutable_bbox());
                target->set_confidence(number.confidence);
            }
            response->set_status(
                extraction.incomplete ? wire::EXTRACTION_STATUS_INCOMPLETE : wire::EXTRACTION_STATUS_OK);
            response->set_status_reason(extraction.status_reason);
            return grpc::Status::OK;
        } catch (const std::out_of_range&) {
            return {grpc::StatusCode::NOT_FOUND, "template set is not available"};
        } catch (const std::invalid_argument&) {
            return {grpc::StatusCode::INVALID_ARGUMENT, "template set or document block is invalid"};
        } catch (const std::exception&) {
            return {grpc::StatusCode::INTERNAL, "document extraction failed"};
        }
    }

private:
    struct CachedTemplateSet {
        std::filesystem::file_time_type last_modified;
        tenderhive::docintel::TemplateSet templates;
    };

    std::string template_directory_;
    double confidence_floor_;
    bool cache_templates_;
    std::size_t template_cache_entries_;
    std::mutex templates_mutex_;
    std::unordered_map<std::string, CachedTemplateSet> templates_cache_;

    tenderhive::docintel::TemplateSet templates_for(const std::string& version) {
        if (!is_template_version(version)) {
            throw std::invalid_argument("invalid template set version");
        }
        if (!cache_templates_) {
            return tenderhive::docintel::load_template_set(template_directory_, version);
        }

        const auto path = std::filesystem::path(template_directory_) / (version + ".json");
        std::error_code error;
        const auto last_modified = std::filesystem::last_write_time(path, error);
        if (error) {
            if (error == std::errc::no_such_file_or_directory) {
                throw std::out_of_range("template set not found");
            }
            throw std::runtime_error("unable to inspect template set");
        }

        std::lock_guard<std::mutex> guard(templates_mutex_);
        const auto cached = templates_cache_.find(version);
        if (cached != templates_cache_.end() && cached->second.last_modified == last_modified) {
            return cached->second.templates;
        }

        auto templates = tenderhive::docintel::load_template_set(template_directory_, version);
        if (cached == templates_cache_.end() && templates_cache_.size() >= template_cache_entries_) {
            templates_cache_.clear();
        }
        templates_cache_.insert_or_assign(version, CachedTemplateSet{last_modified, templates});
        return templates;
    }
};

int run_healthcheck(const std::string& address) {
    auto channel = grpc::CreateChannel(address, grpc::InsecureChannelCredentials());
    auto stub = wire::DocIntel::NewStub(channel);
    grpc::ClientContext context;
    wire::HealthRequest request;
    wire::HealthResponse response;
    const auto deadline = std::chrono::system_clock::now() + std::chrono::seconds(3);
    context.set_deadline(deadline);
    const auto status = stub->CheckHealth(&context, request, &response);
    return status.ok() && response.ready() ? 0 : 1;
}

}  // namespace

int main(int argc, char** argv) {
    try {
        const auto host = required_environment("DOCINTEL_GRPC_BIND_HOST");
        const auto port = environment_integer("DOCINTEL_GRPC_PORT");
        const auto address = host + ":" + std::to_string(port);
        if (argc == 2 && std::string(argv[1]) == "healthcheck") {
            return run_healthcheck("127.0.0.1:" + std::to_string(port));
        }
        if (argc != 1) {
            std::cerr << "Usage: docintel-core [healthcheck]\n";
            return 2;
        }
        const auto template_directory = required_environment("DOCINTEL_TEMPLATE_DIR");
        const auto confidence_floor = environment_fraction("DOCINTEL_CONFIDENCE_FLOOR");
        const auto cache_templates = environment_boolean("DOCINTEL_CACHE_TEMPLATES");
        const auto template_cache_entries = environment_integer("DOCINTEL_TEMPLATE_CACHE_ENTRIES");
        const auto max_message_bytes = environment_integer("DOCINTEL_GRPC_MAX_MESSAGE_BYTES");

        DocIntelService service(
            template_directory,
            confidence_floor,
            cache_templates,
            static_cast<std::size_t>(template_cache_entries));
        grpc::ServerBuilder builder;
        builder.SetMaxReceiveMessageSize(max_message_bytes);
        builder.SetMaxSendMessageSize(max_message_bytes);
        builder.AddListeningPort(address, grpc::InsecureServerCredentials());
        builder.RegisterService(&service);
        auto server = builder.BuildAndStart();
        if (!server) {
            std::cerr << "Unable to start the document intelligence gRPC service\n";
            return 1;
        }
        std::cout << "th-docintel-core listening on " << address << '\n';
        server->Wait();
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "th-docintel-core configuration error: " << error.what() << '\n';
        return 2;
    }
}
