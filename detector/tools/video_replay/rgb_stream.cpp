#include "dart/green_detector.hpp"
#include "dart/target_json.hpp"
#include "dart/visual_motion.hpp"
#include "maix_image.hpp"

#include <chrono>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <memory>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include <opencv2/core.hpp>

namespace {
constexpr std::size_t max_header_bytes = 4096;
constexpr std::uint64_t max_payload_bytes = 32U * 1024U * 1024U;
constexpr std::uint64_t max_exact_json_integer = 9007199254740991ULL;
using Clock = std::chrono::steady_clock;

std::uint64_t host_time_us()
{
    return static_cast<std::uint64_t>(std::chrono::duration_cast<
        std::chrono::microseconds>(Clock::now().time_since_epoch()).count());
}

bool read_header(std::string &header)
{
    header.clear();
    char character;
    while (std::cin.get(character)) {
        if (character == '\n') {
            if (header.empty()) throw std::runtime_error("empty RGB header");
            return true;
        }
        if (header.size() == max_header_bytes)
            throw std::runtime_error("RGB header exceeds 4096 bytes");
        header.push_back(character);
    }
    if (!header.empty() || !std::cin.eof())
        throw std::runtime_error("truncated RGB header");
    return false;
}

std::uint64_t integer(const std::map<std::string, std::string> &fields, const char *name,
                      std::uint64_t maximum = max_exact_json_integer)
{
    const auto value = fields.find(name);
    if (value == fields.end())
        throw std::runtime_error(std::string("missing/noninteger ") + name);
    const auto number = std::stoull(value->second);
    if (number > maximum)
        throw std::runtime_error(std::string("invalid integer ") + name);
    return number;
}

struct Header {
    std::uint64_t epoch, sequence, timestamp_us;
    int width, height, stride;
    std::size_t bytes;
    bool camera;
};

Header parse_header(const std::string &line)
{
    // This deliberately small JSON subset has seven fixed ASCII keys, one
    // protocol string and unsigned decimal integer values. Parse the bounded
    // text directly: OpenCV FileStorage truncates >32-bit integer literals and
    // accepts trailing text, neither of which is valid for frame identities.
    std::size_t cursor = 0;
    auto whitespace = [&] {
        while (cursor < line.size() && (line[cursor] == ' ' || line[cursor] == '\r' ||
               line[cursor] == '\t')) ++cursor;
    };
    auto take = [&](char expected) {
        whitespace();
        if (cursor >= line.size() || line[cursor++] != expected)
            throw std::runtime_error("malformed RGB header JSON");
    };
    auto text = [&]() {
        take('"');
        std::string value;
        while (cursor < line.size() && line[cursor] != '"') {
            const auto character = static_cast<unsigned char>(line[cursor++]);
            if (character < 0x20 || character > 0x7e || character == '\\')
                throw std::runtime_error("RGB header strings require unescaped ASCII");
            value.push_back(static_cast<char>(character));
        }
        take('"');
        return value;
    };
    const std::set<std::string> allowed{
        "protocol", "stream_epoch", "source_sequence", "simulation_timestamp_us",
        "receipt_timestamp_us", "width", "height", "stride"};
    std::set<std::string> seen;
    std::map<std::string, std::string> fields;
    take('{');
    bool camera = false;
    for (std::size_t index = 0; index < allowed.size() - 1; ++index) {
        if (index) take(',');
        const auto name = text();
        if (!allowed.count(name) || !seen.insert(name).second)
            throw std::runtime_error("unknown/duplicate RGB header field: " + name);
        take(':');
        if (name == "protocol") {
            const auto protocol = text();
            camera = protocol == "rm27-camera-rgb-v1";
            if (!camera && protocol != "rm27-rgb-v1") throw std::runtime_error("unsupported RGB protocol");
        } else {
            whitespace();
            const auto start = cursor;
            while (cursor < line.size() && line[cursor] >= '0' && line[cursor] <= '9') ++cursor;
            if (cursor == start || (line[start] == '0' && cursor != start + 1))
                throw std::runtime_error("RGB header requires unsigned decimal JSON integers");
            fields.emplace(name, line.substr(start, cursor - start));
        }
    }
    take('}');
    whitespace();
    if (cursor != line.size()) throw std::runtime_error("trailing RGB header data");
    Header result{};
    result.epoch = integer(fields, "stream_epoch", 2147483647);
    result.sequence = integer(fields, "source_sequence");
    if (!seen.count("protocol") || seen.count(camera ? "simulation_timestamp_us" : "receipt_timestamp_us"))
        throw std::runtime_error("RGB timestamp field differs from protocol");
    result.camera = camera;
    result.timestamp_us = integer(fields, camera ? "receipt_timestamp_us" : "simulation_timestamp_us");
    result.width = static_cast<int>(integer(fields, "width", 4096));
    result.height = static_cast<int>(integer(fields, "height", 4096));
    result.stride = static_cast<int>(integer(fields, "stride", 4096 * 3));
    const auto bytes = static_cast<std::uint64_t>(result.stride) * result.height;
    if (result.width == 0 || result.height == 0 || result.stride != result.width * 3 ||
        bytes > max_payload_bytes)
        throw std::runtime_error("RGB frame must be contiguous RGB3, positive and <=32 MiB");
    result.bytes = static_cast<std::size_t>(bytes);
    return result;
}

// This optional object describes work actually performed by this frame. It
// accepts no labels and is deliberately separate from the measurement schema.
void write_stage_diagnostics(std::ostream &out, const dart::GreenLightStageDiagnostics &d)
{
    out << ",\"stage_diagnostics\":{\"schema_version\":1,\"enabled\":" << (d.enabled ? "true" : "false")
        << ",\"classical_ran\":" << (d.classical_ran ? "true" : "false")
        << ",\"source_region\":" << (d.source_region ? "true" : "false")
        << ",\"normalized_enabled\":" << (d.normalized_enabled ? "true" : "false")
        << ",\"sparse_search\":" << (d.sparse_search ? "true" : "false")
        << ",\"legacy_candidates_enabled\":" << (d.legacy_candidates_enabled ? "true" : "false")
        << ",\"saturated_core_candidates_enabled\":" << (d.saturated_core_candidates_enabled ? "true" : "false")
        << ",\"search_mode\":\"" << dart::green_search_mode_name(d.search_mode) << '"'
        << ",\"search_rect\":{\"x\":" << d.search_x << ",\"y\":" << d.search_y
        << ",\"width\":" << d.search_width << ",\"height\":" << d.search_height << '}'
        << ",\"sample_capacity\":" << d.max_samples;
#define DART_STAGE_COUNT(name) out << ",\"" #name "\":" << d.name
    DART_STAGE_COUNT(tile_seeds);
    DART_STAGE_COUNT(coarse_proposals);
    DART_STAGE_COUNT(coarse_retained);
    DART_STAGE_COUNT(refined_proposals);
    DART_STAGE_COUNT(exact_evaluations);
    DART_STAGE_COUNT(exact_passed);
    DART_STAGE_COUNT(exact_rejected_support);
    DART_STAGE_COUNT(exact_rejected_response);
    DART_STAGE_COUNT(exact_rejected_contrast);
    DART_STAGE_COUNT(exact_rejected_color);
    DART_STAGE_COUNT(peaks_considered);
    DART_STAGE_COUNT(peaks_unvisited);
    DART_STAGE_COUNT(nms_rejected);
    DART_STAGE_COUNT(appearance_evaluated);
    DART_STAGE_COUNT(appearance_passed);
    DART_STAGE_COUNT(candidate_count);
    DART_STAGE_COUNT(inside_cone_count);
    DART_STAGE_COUNT(score_passed_count);
    DART_STAGE_COUNT(samples_dropped);
#undef DART_STAGE_COUNT
    out << ",\"selected_index\":" << d.selected_index
        << ",\"tracker_state_after\":\"" << dart::track_state_name(d.tracker_state_after) << '"'
        << ",\"tracker_confirmed_before\":" << (d.tracker_confirmed_before ? "true" : "false")
        << ",\"tracker_confirmed_after\":" << (d.tracker_confirmed_after ? "true" : "false")
        << ",\"selected_passes_association_gate\":" << (d.selected_passes_association_gate ? "true" : "false")
        << ",\"reason_counts\":{";
    for (std::size_t index = 0; index < d.reason_counts.size(); ++index) {
        if (index) out << ',';
        out << '"' << dart::green_peak_stage_reason_name(static_cast<dart::GreenPeakStageReason>(index))
            << "\":" << d.reason_counts[index];
    }
    out << "},\"samples\":[";
    for (std::size_t index = 0; index < d.samples.size(); ++index) {
        if (index) out << ',';
        const auto &sample = d.samples[index];
        out << "{\"x\":" << sample.x << ",\"y\":" << sample.y
            << ",\"diameter\":" << sample.diameter << ",\"response\":" << sample.response
            << ",\"reason\":\"" << dart::green_peak_stage_reason_name(sample.reason) << "\"}";
    }
    out << "]}";
}

int run(int argc, char **argv)
{
    if (argc == 2 && std::string(argv[1]) == "--help") {
        std::cout << "Usage: dart_rgb_stream --config CONFIG [--diagnostics]\n"
                  << "Bounded rm27-rgb-v1 JSON-header + contiguous RGB3 request/reply.\n";
        return 0;
    }
    if ((argc != 3 && argc != 4) || std::string(argv[1]) != "--config" ||
        (argc == 4 && std::string(argv[3]) != "--diagnostics"))
        throw std::runtime_error("usage: dart_rgb_stream --config CONFIG [--diagnostics]");
    auto config = dart::load_application_config(argv[2]);
    config.detector.enable_stage_diagnostics = argc == 4;
    if (config.npu.enabled || config.npu.required || config.target_geometry.pose_enabled)
        throw std::runtime_error("RGB stream requires NPU and target pose disabled");
    if (config.camera.width > 4096 || config.camera.height > 4096 ||
        static_cast<std::uint64_t>(config.camera.width) * config.camera.height * 3 > max_payload_bytes)
        throw std::runtime_error("configured RGB frame exceeds protocol limits");
    cv::setNumThreads(1);
    std::unique_ptr<dart::GreenLightDetector> detector;
    std::unique_ptr<dart::VisualMotionEstimator> visual_motion;
    dart::TrackState previous_state = dart::TrackState::Lost;
    Header previous{};
    std::uint64_t stream_frames = 0, stream_gaps = 0;
    std::string header_text;
    std::vector<std::uint8_t> payload;
    while (read_header(header_text)) {
        const auto header = parse_header(header_text);
        const auto host_header_received_us = host_time_us();
        if (header.width != config.camera.width || header.height != config.camera.height)
            throw std::runtime_error("RGB dimensions differ from config; no implicit resize");
        std::uint64_t gap = 0;
        if (detector) {
            if (header.camera != previous.camera)
                throw std::runtime_error("RGB protocol changed during the stream");
            if (header.epoch < previous.epoch)
                throw std::runtime_error("stream epoch reversed");
            if (header.epoch == previous.epoch) {
                if (header.sequence <= previous.sequence || header.timestamp_us <= previous.timestamp_us)
                    throw std::runtime_error("RGB sequence/time must strictly increase within epoch");
                gap = header.sequence - previous.sequence - 1;
            }
        }
        if (!detector || header.epoch != previous.epoch) {
            detector = std::make_unique<dart::GreenLightDetector>(
                config.detector, config.armor, config.target_geometry, config.npu);
            visual_motion = std::make_unique<dart::VisualMotionEstimator>(config.visual_motion);
            previous_state = dart::TrackState::Lost;
            stream_frames = stream_gaps = 0;
        }
        payload.resize(header.bytes);
        std::cin.read(reinterpret_cast<char *>(payload.data()),
                      static_cast<std::streamsize>(payload.size()));
        if (std::cin.gcount() != static_cast<std::streamsize>(payload.size()))
            throw std::runtime_error("truncated RGB payload");
        const auto host_payload_received_us = host_time_us();
        cv::Mat rgb(header.height, header.width, CV_8UC3, payload.data(), header.stride);
        maix::image::Image frame(rgb);
        const auto started = host_time_us();
        dart::MotionPrior motion;
        const bool motion_allowed = !config.visual_motion.tracking_only ||
                                    previous_state == dart::TrackState::Tracking;
        if (!motion_allowed) visual_motion->reset();
        if (config.visual_motion.enabled && motion_allowed &&
            stream_frames % static_cast<std::uint64_t>(config.visual_motion.interval_frames) == 0)
            motion = visual_motion->update(frame, header.timestamp_us);
        auto target = detector->process(frame, header.timestamp_us, motion.valid ? &motion : nullptr);
        const auto finished = host_time_us();
        previous_state = target.green.state;
        target.source_metadata_valid = true;
        target.source_sequence = header.sequence;
        target.source_pts_raw = header.timestamp_us;
        // This field denotes host reception on device paths; simulation time
        // is deliberately not assigned to it. Actual receipt is in rgb_stream.
        target.source_received_us = 0;
        target.upstream_missing = stream_gaps += gap;
        dart::stamp_pixel_measurement_source(target, header.sequence, header.timestamp_us,
            header.timestamp_us, header.camera ? "application_sequence" : "simulated_camera_source_sequence",
            header.camera ? "capture_host_receipt_us" : "simulation_time_us", true);
        target.angles_valid = false;
        target.safe_for_control = false;
        target.pose.valid = false;
        target.yaw_rad = target.pitch_rad = 0;
        target.green.yaw_rad = target.green.pitch_rad = 0;
        target.line_of_sight_camera = {0, 0, 0};
        target.line_of_sight_rate_rad_s = {0, 0};
        target.angular_covariance = {0, 0};
        std::ostringstream extra;
        extra << ",\"rgb_stream\":{\"protocol\":\"" << (header.camera ? "rm27-camera-rgb-v1" : "rm27-rgb-v1")
              << "\",\"source_kind\":\"" << (header.camera ? "camera" : "simulation") << '"'
              << ",\"stream_epoch\":" << header.epoch << ",\"source_sequence\":" << header.sequence
              << (header.camera ? ",\"receipt_timestamp_us\":" : ",\"simulation_timestamp_us\":") << header.timestamp_us
              << ",\"timestamp_source\":\"" << (header.camera ? "capture_host_receipt_us" : "simulation_time_us") << '"'
              << ",\"width\":" << header.width
              << ",\"height\":" << header.height << ",\"stride\":" << header.stride
              << ",\"pixel_format\":\"RGB3\",\"sequence_gap\":" << gap
              << ",\"host_header_received_us\":" << host_header_received_us
              << ",\"host_payload_received_us\":" << host_payload_received_us
              << ",\"host_processing_start_us\":" << started
              << ",\"host_processing_end_us\":" << finished
              << ",\"processing_ms\":" << std::fixed << std::setprecision(6)
              << (finished - started) / 1000.0 << '}';
        if (config.detector.enable_stage_diagnostics)
            write_stage_diagnostics(extra, detector->last_stage_diagnostics());
        dart::write_target_estimate_json(std::cout, target, nullptr, extra.str());
        std::cout << '\n' << std::flush;
        if (!std::cout) throw std::runtime_error("RGB response write failed");
        previous = header;
        ++stream_frames;
    }
    return 0;
}
} // namespace

int main(int argc, char **argv)
{
    try { return run(argc, argv); }
    catch (const std::exception &error) {
        std::cerr << "dart_rgb_stream: " << error.what() << '\n';
        return 1;
    }
}
