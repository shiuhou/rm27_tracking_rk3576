#!/usr/bin/env python3
"""Compare baseline, candidate default and candidate --diagnostics offline.

Each binary mode receives the entire integrity-checked original-payload recording
once, in order, with original application sequence and host receipt timestamps.
This checks output parity, not performance or hardware acceptance. Put --out in
an ignored artifact directory; inputs and existing outputs are never overwritten.
Optional human CSV labels are used only after all three detector passes finish.
"""

import argparse
from collections import Counter
import copy
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time


TIMING_PATHS = (
    "classical_detection_ms", "model_inference_ms",
    "rgb_stream.host_header_received_us", "rgb_stream.host_payload_received_us",
    "rgb_stream.host_processing_start_us", "rgb_stream.host_processing_end_us",
    "rgb_stream.processing_ms",
)
COUNTERS = (
    "tile_seeds", "coarse_proposals", "coarse_retained", "refined_proposals",
    "exact_evaluations", "exact_passed", "exact_rejected_support",
    "exact_rejected_response", "exact_rejected_contrast", "exact_rejected_color",
    "peaks_considered", "nms_rejected", "appearance_evaluated", "appearance_passed",
)
MAX_REPLY_BYTES = 65536
MAX_SAMPLES = 128
HUMAN_STATUSES = {"VISIBLE", "OCCLUDED", "OUT_OF_FOV", "NOT_PRESENT", "UNCERTAIN"}


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def reject_nonfinite(value):
    raise ValueError(f"nonfinite JSON value: {value}")


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def comparable(row):
    result = copy.deepcopy(row)
    result.pop("stage_diagnostics", None)
    for path in TIMING_PATHS:
        current = result
        parts = path.split(".")
        for part in parts[:-1]:
            current = current.get(part, {})
        current.pop(parts[-1], None)
    return result


def natural(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def finite(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def validate_diagnostics(row):
    """Check bounded current-frame telemetry, never use it to alter a decision."""
    diag = row["stage_diagnostics"]
    if diag.get("schema_version") != 1 or diag.get("enabled") is not True:
        raise ValueError("unsupported or disabled stage_diagnostics")
    if diag.get("sample_capacity") != MAX_SAMPLES:
        raise ValueError("stage sample capacity must be 128")
    if diag.get("classical_ran") is not row["classical_detection_ran"]:
        raise ValueError("diagnostics do not describe the current detection pass")
    if diag.get("tracker_state_after") != row["green"]["state"]:
        raise ValueError("diagnostic tracker state differs from current output")
    for name in (*COUNTERS, "samples_dropped", "peaks_unvisited", "candidate_count",
                 "inside_cone_count", "score_passed_count"):
        natural(diag[name], name)
    if diag["candidate_count"] != row.get("candidate_count", diag["candidate_count"]):
        raise ValueError("diagnostic candidate count differs from current output")
    if type(diag["selected_index"]) is not int or not -1 <= diag["selected_index"] < max(1, diag["candidate_count"]):
        raise ValueError("invalid diagnostic selected_index")
    if diag["candidate_count"] == 0 and diag["selected_index"] != -1:
        raise ValueError("selected candidate exists in an empty candidate list")
    if diag["inside_cone_count"] > diag["candidate_count"] or diag["score_passed_count"] > diag["candidate_count"]:
        raise ValueError("candidate filter count exceeds candidate count")
    if diag["search_mode"] not in {"not_run", "full_frame", "capture_cone", "tracking_roi"}:
        raise ValueError("unknown diagnostic search mode")
    rect = diag["search_rect"]
    for axis in ("x", "y", "width", "height"):
        natural(rect[axis], f"search_rect.{axis}")
    for name in ("tracker_confirmed_before", "tracker_confirmed_after", "selected_passes_association_gate"):
        if type(diag[name]) is not bool:
            raise ValueError(f"{name} must be boolean")
    for name in ("source_region", "normalized_enabled", "sparse_search", "legacy_candidates_enabled", "saturated_core_candidates_enabled"):
        if type(diag[name]) is not bool:
            raise ValueError(f"{name} must be boolean")
    if not isinstance(diag["reason_counts"], dict):
        raise ValueError("reason_counts must be an object")
    for reason, count in diag["reason_counts"].items():
        if not isinstance(reason, str) or not reason:
            raise ValueError("empty diagnostic reason")
        natural(count, f"reason_counts.{reason}")
    samples = diag["samples"]
    if not isinstance(samples, list) or len(samples) > MAX_SAMPLES:
        raise ValueError("diagnostic samples exceed fixed capacity")
    observed = Counter()
    for sample in samples:
        for name in ("x", "y", "diameter", "response"):
            finite(sample[name], f"sample.{name}")
        if sample["diameter"] <= 0 or sample["reason"] not in diag["reason_counts"]:
            raise ValueError("invalid diagnostic sample outcome")
        observed[sample["reason"]] += 1
    if any(count > diag["reason_counts"][reason] for reason, count in observed.items()):
        raise ValueError("sample outcomes exceed current-frame reason counts")
    if sum(diag["reason_counts"].values()) != len(samples) + diag["samples_dropped"]:
        raise ValueError("diagnostic sample/drop conservation failed")
    if diag["peaks_considered"] != len(samples) + diag["samples_dropped"]:
        raise ValueError("diagnostic considered-peak conservation failed")
    if diag["exact_passed"] != diag["peaks_considered"] + diag["peaks_unvisited"]:
        raise ValueError("diagnostic exact-pass conservation failed")
    exact_rejections = sum(diag[name] for name in COUNTERS if name.startswith("exact_rejected_"))
    if diag["exact_evaluations"] != diag["exact_passed"] + exact_rejections:
        raise ValueError("diagnostic exact-evaluation conservation failed")
    if not diag["classical_ran"] and (any(diag[name] for name in COUNTERS) or samples or diag["samples_dropped"]):
        raise ValueError("skipped detection retained stale stage counters")
    return diag


def load_annotations(path):
    """Read explicit human decisions; AI columns and filenames are not labels."""
    with Path(path).open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        required = {"frame_id", "human_status", *(f"human_{axis}" for axis in "xywh")}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError("annotations need frame_id, human_status and human_xywh")
        result = {}
        for row in reader:
            if None in row or any(row[key] is None for key in required):
                raise ValueError("malformed annotation row")
            frame_id = natural(int(row["frame_id"]), "annotation frame_id")
            if frame_id in result or row["human_status"] not in HUMAN_STATUSES:
                raise ValueError("duplicate frame or unreviewed human status")
            fields = [row[f"human_{axis}"].strip() for axis in "xywh"]
            box = None
            if row["human_status"] == "VISIBLE":
                box = [finite(float(value), "human box") for value in fields]
                if min(box[:2]) < 0 or min(box[2:]) <= 0:
                    raise ValueError("human box needs nonnegative xy and positive wh")
            elif any(fields):
                raise ValueError("human box supplied for non-visible reference")
            result[frame_id] = {"human_status": row["human_status"], "bbox_xywh": box}
            if len(result) > 1900:
                raise ValueError("annotation table exceeds 1900-frame bound")
    return result


def inside(x, y, box):
    bx, by, width, height = box
    return bx <= x < bx + width and by <= y < by + height


def score_annotations(annotations, rows, supplemental):
    missing = set(annotations) - set(rows)
    if missing or not supplemental.issubset(annotations):
        raise ValueError(f"annotation or supplemental frame absent: {sorted(missing | (supplemental - set(annotations)))}")
    groups = {}
    for frame_id, reference in annotations.items():
        row = rows[frame_id]
        meta = row["rgb_stream"]
        point = row["pixel_measurement"]
        fresh = all(point.get(key) is True for key in ("valid", "new_measurement", "tracker_confirmed", "source_metadata_valid"))
        fresh = fresh and point.get("selected_count") == 1 and point.get("center", {}).get("valid") is True
        fresh = fresh and point.get("source_sequence") == frame_id and point.get("timestamp_us") == meta["receipt_timestamp_us"]
        fresh = fresh and point.get("measurement_age_us") == 0
        center = [finite(point["center"][axis], f"pixel center {axis}") for axis in "xy"] if fresh else None
        group = groups.setdefault("supplemental" if frame_id in supplemental else "uniform", {
            "human_status_counts": Counter(), "visible_frames": 0, "fresh_centers_inside_box": 0,
            "visible_without_fresh_center": 0, "fresh_centers_outside_box": 0,
            "absent_frames": 0, "fresh_centers_on_absent_frames": 0, "excluded_uncertain_frames": 0,
            "sample_reasons_inside_boxes": Counter(), "frames_with_dropped_samples": 0, "frames": [],
        })
        status, box = reference["human_status"], reference["bbox_xywh"]
        item = {"frame_id": frame_id, **reference, "fresh_confirmed_center": center}
        group["human_status_counts"][status] += 1
        if status == "VISIBLE":
            if box[0] + box[2] > meta["width"] or box[1] + box[3] > meta["height"]:
                raise ValueError("reviewed box extends beyond source frame")
            matched = bool(fresh and inside(*center, box))
            item["fresh_center_inside_box"] = matched
            item["center_error_px"] = math.hypot(center[0] - box[0] - box[2] / 2, center[1] - box[1] - box[3] / 2) if fresh else None
            group["visible_frames"] += 1
            group["fresh_centers_inside_box"] += matched
            group["visible_without_fresh_center"] += not fresh
            group["fresh_centers_outside_box"] += bool(fresh and not matched)
            diag = row["stage_diagnostics"]
            reasons = Counter(sample["reason"] for sample in diag["samples"] if inside(sample["x"], sample["y"], box))
            rect = diag["search_rect"]
            overlap_w = max(0, min(box[0] + box[2], rect["x"] + rect["width"]) - max(box[0], rect["x"]))
            overlap_h = max(0, min(box[1] + box[3], rect["y"] + rect["height"]) - max(box[1], rect["y"]))
            item["diagnostics"] = {
                "search_mode": diag["search_mode"], "search_rect": rect,
                "search_rect_bbox_coverage_fraction": overlap_w * overlap_h / (box[2] * box[3]),
                "candidate_count": diag["candidate_count"], "selected_index": diag["selected_index"],
                "normalized_enabled": diag["normalized_enabled"],
                "sparse_search": diag["sparse_search"],
                "legacy_candidates_enabled": diag["legacy_candidates_enabled"],
                "saturated_core_candidates_enabled": diag["saturated_core_candidates_enabled"],
                "sample_reasons_inside_box": dict(reasons), "samples_dropped_global": diag["samples_dropped"],
                "peaks_unvisited_global": diag["peaks_unvisited"],
                "attribution": "sampled_appearance_rejections_observed" if any(reason.startswith("appearance_") for reason in reasons) else "no_local_appearance_attribution_from_samples",
            }
            group["sample_reasons_inside_boxes"].update(reasons)
            group["frames_with_dropped_samples"] += diag["samples_dropped"] > 0
        elif status in {"OUT_OF_FOV", "NOT_PRESENT"}:
            group["absent_frames"] += 1
            group["fresh_centers_on_absent_frames"] += bool(fresh)
        else:
            group["excluded_uncertain_frames"] += 1
        group["frames"].append(item)
    return {
        "matching_rule": "Current confirmed pre-tracker pixel center inside human XYWH; right/bottom exclusive",
        "grouping": "Explicit --supplemental-frame IDs are separated; all other reviewed frames are uniform",
        "limits": "CSV frame_id binds to verified recording application sequence. Only human_status/human_xywh are labels. Sparse reason samples do not establish every local rejection; rectangular search coverage does not establish cone-mask coverage. No whole-dataset or generalization claim.",
        "groups": groups,
    }


def exchange(process, selector, chunks, deadline, stderr):
    """Transfer one frame while draining both output pipes within fixed bounds."""
    chunks = iter(chunks)
    pending = next(chunks)
    reply = bytearray()
    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
    writing = True
    while writing or not reply.endswith(b"\n"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("RGB frame/pass deadline exceeded")
        events = selector.select(remaining)
        if not events:
            raise TimeoutError("RGB frame/pass deadline exceeded")
        for key, _ in events:
            if key.data == "stdin":
                try:
                    count = os.write(key.fd, pending)
                except BlockingIOError:
                    continue
                if count <= 0:
                    raise RuntimeError("RGB input made no progress")
                pending = pending[count:]
                if not pending:
                    pending = next(chunks, None)
                    if pending is None:
                        selector.unregister(process.stdin)
                        writing = False
            else:
                try:
                    chunk = os.read(key.fd, 8192)
                except BlockingIOError:
                    continue
                if key.data == "stderr":
                    if not chunk:
                        selector.unregister(key.fileobj)
                    stderr.extend(chunk)
                    if len(stderr) > MAX_REPLY_BYTES:
                        raise RuntimeError("detector stderr exceeds 65536-byte bound")
                elif not chunk:
                    raise RuntimeError("detector closed stdout before a complete reply")
                else:
                    reply.extend(chunk)
                    if len(reply) > MAX_REPLY_BYTES or b"\n" in reply[:-1]:
                        raise RuntimeError("RGB reply exceeds bound or contains extra rows")
    return bytes(reply)


def replay(binary, config, recording, out, name, diagnostics, wanted_frames, max_frames, max_pass_s):
    # Resolve this component's owned recording implementation, not historical code.
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
    import cv2
    from rm27_tracking.recording import RecordingSource

    cv2.setNumThreads(1)
    source = RecordingSource(recording)
    if source.manifest["complete"] is not True or not 1 <= source.manifest["frame_count"] <= max_frames:
        source.close()
        raise ValueError("recording must be complete, nonempty and within frame bound; no prefix is scored")
    command = [str(binary.resolve()), "--config", str(config.resolve())]
    if diagnostics:
        command.append("--diagnostics")
    began = time.monotonic()
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    selector = selectors.DefaultSelector()
    stderr = bytearray()
    for stream, kind in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, kind)
    os.set_blocking(process.stdin.fileno(), False)
    rgb_hash, semantic_hash = hashlib.sha256(), hashlib.sha256()
    hashes, retained = [], {}
    modes, states, counters, reasons = Counter(), Counter(), Counter(), Counter()
    dropped_frames = dropped_total = unvisited_total = 0
    path = out / f"{name}.jsonl"
    try:
        with path.open("xb") as output:
            for frame in source:
                if len(hashes) >= max_frames or time.monotonic() - began >= max_pass_s:
                    raise RuntimeError("complete recording exceeded frame/pass bound")
                rgb = cv2.cvtColor(frame.image, cv2.COLOR_BGR2RGB)
                height, width = rgb.shape[:2]
                header = {"protocol": "rm27-camera-rgb-v1", "stream_epoch": frame.stream_id,
                          "source_sequence": frame.sequence,
                          "receipt_timestamp_us": frame.original_received_monotonic_ns // 1000,
                          "width": width, "height": height, "stride": width * 3}
                payload = memoryview(rgb).cast("B")
                rgb_hash.update(payload)
                reply = exchange(process, selector, (memoryview(encoded(header) + b"\n"), payload),
                                 min(began + max_pass_s, time.monotonic() + 5), stderr)
                row = json.loads(reply, parse_constant=reject_nonfinite)
                if any(row["rgb_stream"].get(key) != value for key, value in header.items()):
                    raise ValueError("reply changed original frame identity/receipt timestamp")
                if diagnostics:
                    diag = validate_diagnostics(row)
                    modes[diag["search_mode"]] += 1
                    counters.update({name: diag[name] for name in COUNTERS})
                    reasons.update(diag["reason_counts"])
                    dropped_frames += diag["samples_dropped"] > 0
                    dropped_total += diag["samples_dropped"]
                    unvisited_total += diag["peaks_unvisited"]
                elif "stage_diagnostics" in row:
                    raise ValueError("default detector unexpectedly emitted stage_diagnostics")
                semantic = encoded(comparable(row))
                hashes.append(hashlib.sha256(semantic).hexdigest())
                semantic_hash.update(semantic + b"\n")
                states[row["green"]["state"]] += 1
                output.write(reply)
                if frame.sequence in wanted_frames:
                    retained[frame.sequence] = row
        if (len(hashes) != source.manifest["frame_count"] or source.stats["frames"] != len(hashes)
                or source.stats["incomplete_tail"]):
            raise ValueError("replay did not consume every declared recording frame")
        process.stdin.close()
        remaining = min(5, began + max_pass_s - time.monotonic())
        if remaining <= 0 or process.wait(timeout=remaining) != 0:
            raise RuntimeError("detector did not exit successfully within pass bound")
        # Reject unsolicited trailing output, preserving the one-input/one-row rule.
        tail = process.stdout.read(MAX_REPLY_BYTES + 1) or b""
        if tail:
            raise RuntimeError("detector emitted trailing unsolicited stdout")
        stderr.extend(process.stderr.read(MAX_REPLY_BYTES + 1) or b"")
        if len(stderr) > MAX_REPLY_BYTES:
            raise RuntimeError("detector stderr exceeds 65536-byte bound")
    finally:
        source.close()
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        if stderr:
            (out / f"{name}.stderr.txt").write_bytes(stderr)
    result = {"frames": len(hashes), "expected_frame_count": source.manifest["frame_count"],
              "recording_complete": True, "decoded_rgb_sha256": rgb_hash.hexdigest(),
              "semantic_output_sha256": semantic_hash.hexdigest(), "rows_sha256": digest(path),
              "recorded_source_sequence_gaps": source.stats["source_drops"],
              "recorded_application_sequence_gaps": source.stats["application_sequence_gaps"],
              "replay_dropped_frames": 0, "state_counts": dict(states)}
    if diagnostics:
        result["diagnostics"] = {"schema_validation": "PASS", "search_modes": dict(modes),
                                 "counter_totals": dict(counters), "reason_totals": dict(reasons),
                                 "frames_with_dropped_samples": dropped_frames,
                                 "samples_dropped": dropped_total, "peaks_unvisited": unvisited_total}
    return result, hashes, retained


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "candidate", "config", "recording", "out"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--annotations", type=Path, help="Reviewed CSV; only human status/box columns are labels")
    parser.add_argument("--supplemental-frame", type=int, action="append", default=[])
    parser.add_argument("--max-frames", type=int, default=1900)
    parser.add_argument("--max-pass-s", type=float, default=150)
    args = parser.parse_args()
    if not 1 <= args.max_frames <= 1900 or not 0 < args.max_pass_s <= 150:
        parser.error("bounds require 1..1900 frames and 0..150 seconds per complete pass")
    if args.supplemental_frame and not args.annotations:
        parser.error("--supplemental-frame requires --annotations")
    args.out.mkdir(parents=True, exist_ok=False)
    inputs = {name: {"path": str(getattr(args, name).resolve()), "sha256": digest(getattr(args, name))}
              for name in ("baseline", "candidate", "config")}
    inputs["recording"] = {name: digest(args.recording / name) for name in ("manifest.json", "frames.jsonl", "payload.bin")}
    if args.annotations:
        inputs["annotations"] = {"path": str(args.annotations.resolve()), "sha256": digest(args.annotations)}
    (args.out / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
    summary = {"status": "FAIL", "comparison": "Three sequential complete-recording passes, one per mode",
               "scope": "PC original-payload replay; no hardware, performance or detection improvement claim. Original host receipt timestamps are preserved, not sensor exposure timestamps.",
               "limits": {"max_frames": args.max_frames, "max_pass_s": args.max_pass_s,
                          "reply_timeout_s": 5, "max_reply_bytes": MAX_REPLY_BYTES, "sample_capacity": MAX_SAMPLES},
               "excluded_paths": [*TIMING_PATHS, "stage_diagnostics"], "inputs": inputs, "runs": {}}
    try:
        annotations = load_annotations(args.annotations) if args.annotations else {}
        supplemental = set(args.supplemental_frame)
        all_hashes = []
        retained = {}
        for name, binary, diagnostic in (("baseline", args.baseline, False),
                                         ("candidate_default", args.candidate, False),
                                         ("candidate_diagnostics", args.candidate, True)):
            result, hashes, rows = replay(binary, args.config, args.recording, args.out, name,
                                          diagnostic, set(annotations), args.max_frames, args.max_pass_s)
            summary["runs"][name] = result
            all_hashes.append(hashes)
            if diagnostic:
                retained = rows
        mismatches = {name: [index for index, (a, b) in enumerate(zip(all_hashes[0], candidate)) if a != b]
                      for name, candidate in zip(("candidate_default", "candidate_diagnostics"), all_hashes[1:])}
        same_frames = len({len(values) for values in all_hashes}) == 1
        same_pixels = len({run["decoded_rgb_sha256"] for run in summary["runs"].values()}) == 1
        summary["parity"] = {"same_frame_count": same_frames, "same_decoded_rgb": same_pixels,
                             "mismatched_row_indices": mismatches}
        if annotations:
            summary["human_review"] = score_annotations(annotations, retained, supplemental)
        if not same_frames or not same_pixels or any(mismatches.values()):
            raise ValueError("semantic output parity failed")
        summary["status"] = "PASS"
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
    (args.out / "comparison.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"status": summary["status"], "comparison": str((args.out / "comparison.json").resolve()),
                      **({"error": summary["error"]} if "error" in summary else {})}))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
