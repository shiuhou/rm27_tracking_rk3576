#!/usr/bin/env python3
"""Compare pixel detections with sparse, independently reviewed reference frames.

Uncertain labels are excluded; these counts are not full-video accuracy or
independent-scene generalization. Coordinates must use the source resolution.
"""
import argparse,hashlib,json,math,statistics
from collections import Counter
from pathlib import Path

def _evaluate_legacy(rows,reference):
 missing=[x["frame"] for x in reference if x["frame"] not in rows]
 if missing:raise ValueError(f"Replay missing {len(missing)} reference frames: {missing[:10]}")
 result={}
 for split in sorted({x['split'] for x in reference}):
  items=[x for x in reference if x['split']==split]
  stats={'sampled_frames':len(items),'missing_replay_rows':0}
  for kind in ('green','armor'):
   positives=hits=negatives=false_positives=wrong=uncertain=0;errors=[]
   for ref in items:
    row=rows.get(ref['frame'])
    if row is None:continue
    status=ref[kind+'_status'];d=row[kind]
    observed=d['valid'] and (not d.get('predicted',False) if kind=='green' else row.get('armor_detection_ran',False))
    if status=='uncertain':uncertain+=1;continue
    if status=='absent':
     negatives+=1;false_positives+=int(observed);continue
    positives+=1
    if not observed:continue
    center=[d['center_x'],d['center_y']] if kind=='green' else [d['center']['x'],d['center']['y']]
    error=math.dist(center,ref[kind+'_center'])
    # Coarse reference placement: lamp radius or >=4 px. Armor requires
    # both center and bar-center proximity, so a random nearby pair is not a hit.
    if kind=='green':matched=error<=max(4,ref['green_radius'])
    else:
     x0,y0,x1,y1=ref['armor_bbox'];pad=3
     bar_centers=[d['left_bar']['center'],d['right_bar']['center']]
     separation=math.hypot(bar_centers[0]['x']-bar_centers[1]['x'],bar_centers[0]['y']-bar_centers[1]['y'])
     matched=(error<=max(4,(x1-x0)*.2) and separation>=.5*max(x1-x0,y1-y0) and all(x0-pad<=c['x']<=x1+pad and y0-pad<=c['y']<=y1+pad for c in bar_centers))
    if matched:hits+=1;errors.append(error)
    else:wrong+=1
   stats[kind]={'visible_reference_frames':positives,'matched_direct_detections':hits,'missed_or_wrong':positives-hits,'wrong_location_detections':wrong,'absent_reference_frames':negatives,'false_detections_on_absent':false_positives,'uncertain_excluded':uncertain,'matched_center_error_median_px':statistics.median(errors) if errors else None,'matched_center_error_max_px':max(errors) if errors else None}
  stats['missing_replay_rows']=sum(x['frame'] not in rows for x in items)
  result[split]=stats
 return result


REVIEW_STATES = frozenset(("ai_provisional", "human_reviewed", "pending"))
SAMPLE_GROUPS = frozenset(("uniform", "diagnostic_case"))
STAGES = ("no_candidates", "candidates_without_confirmed_observation",
          "confirmed_wrong_location", "confirmed_matching_location")


def _integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _point(point, width, height, name):
    if not isinstance(point, (list, tuple)) or len(point) != 2:
        raise ValueError(f"{name} must contain two coordinates")
    x, y = (_finite(point[0], name), _finite(point[1], name))
    if not (0 <= x < width and 0 <= y < height):
        raise ValueError(f"{name} is outside source dimensions {width}x{height}")
    return [x, y]


def _dimensions(width, height):
    return (_integer(width, "width", 1), _integer(height, "height", 1))


def validate_references(reference, *, width, height):
    """Validate sparse green references without changing their review provenance."""
    _dimensions(width, height)
    seen = set()
    for ref in reference:
        frame = _integer(ref.get("frame"), "reference frame")
        if frame in seen:
            raise ValueError(f"Duplicate reference frame {frame}")
        seen.add(frame)
        if not isinstance(ref.get("split"), str) or not ref["split"]:
            raise ValueError(f"Reference frame {frame} needs a split")
        status = ref.get("green_status")
        if status not in ("visible", "absent", "uncertain", "pending"):
            raise ValueError(f"Invalid green_status at frame {frame}")
        if ref.get("review_state", "ai_provisional") not in REVIEW_STATES:
            raise ValueError(f"Invalid review_state at frame {frame}")
        if ref.get("sample_group", "uniform") not in SAMPLE_GROUPS:
            raise ValueError(f"Invalid sample_group at frame {frame}")
        if status == "visible":
            _point(ref.get("green_center"), width, height, "green_center")
            if _finite(ref.get("green_radius"), "green_radius") <= 0:
                raise ValueError("green_radius must be positive")
    return seen


def _fresh_pixel(row, width, height):
    """A confirmed pre-tracker pixel is distinct from a predicted posterior."""
    measurement = row.get("pixel_measurement")
    if not isinstance(measurement, dict):
        raise ValueError("Missing pixel_measurement object")
    for key in ("valid", "new_measurement", "tracker_confirmed"):
        if not isinstance(measurement.get(key), bool):
            raise ValueError(f"pixel_measurement.{key} must be boolean")
    count = _integer(measurement.get("selected_count"), "selected_count")
    center = measurement.get("center")
    if not isinstance(center, dict) or not isinstance(center.get("valid"), bool):
        raise ValueError("pixel_measurement.center.valid must be boolean")
    if measurement["valid"] or center["valid"]:
        actual_size = (_integer(measurement.get("source_width"), "source_width", 1),
                       _integer(measurement.get("source_height"), "source_height", 1))
        if actual_size != (width, height):
            raise ValueError("pixel_measurement source dimensions do not match")
        _point([center.get("x"), center.get("y")], width, height, "pixel_measurement center")
    observed = (measurement["valid"] and measurement["new_measurement"]
                and measurement["tracker_confirmed"] and count == 1 and center["valid"]
                and not row.get("predicted", False)
                and not row.get("green", {}).get("predicted", False))
    if observed:
        if "timestamp_us" in measurement:
            timestamp = _integer(measurement["timestamp_us"], "pixel_measurement.timestamp_us")
            if timestamp != row.get("timestamp_us"):
                raise ValueError("Fresh pixel timestamp does not match replay record")
        if "measurement_age_us" in measurement:
            age = _integer(measurement["measurement_age_us"], "pixel_measurement.measurement_age_us")
            if age != 0:
                raise ValueError("Fresh pixel measurement_age_us must be zero")
    return [center["x"], center["y"]] if observed else None


def _green_center(row, width, height, *, include_prediction=False):
    green = row.get("green")
    if not isinstance(green, dict) or not isinstance(green.get("valid"), bool):
        raise ValueError("green.valid must be boolean")
    if not isinstance(green.get("predicted", False), bool):
        raise ValueError("green.predicted must be boolean")
    if not green["valid"]:
        return None
    point = _point([green.get("center_x"), green.get("center_y")], width, height, "green center")
    return point if include_prediction or not green.get("predicted", False) else None


def _row_observations(row, width, height):
    if not isinstance(row.get("predicted", False), bool):
        raise ValueError("predicted must be boolean")
    count = _integer(row.get("candidate_count"), "candidate_count")
    candidates = row.get("candidates")
    if candidates is not None and (not isinstance(candidates, list) or len(candidates) != count):
        raise ValueError("candidate_count does not match candidates")
    posterior = _green_center(row, width, height, include_prediction=True)
    fresh = _fresh_pixel(row, width, height)
    if fresh is not None and count == 0:
        raise ValueError("Confirmed pixel without any candidates")
    return count, fresh, posterior


def _empty_green_stats():
    return {"sampled_frames": 0, "visible_reference_frames": 0,
            "matched_direct_detections": 0, "missed_or_wrong": 0,
            "wrong_location_detections": 0, "absent_reference_frames": 0,
            "false_detections_on_absent": 0, "uncertain_excluded": 0,
            "pending_excluded": 0, "stage_counts": dict.fromkeys(STAGES, 0),
            "sample_results": []}


def _evaluate_green_group(rows, reference, observation_source, width, height):
    stats = _empty_green_stats()
    errors, matched_errors = [], []
    for ref in reference:
        frame = ref["frame"]
        row = rows[frame]
        count, fresh, posterior = _row_observations(row, width, height)
        observed = fresh if observation_source == "pixel_measurement" else _green_center(row, width, height)
        stats["sampled_frames"] += 1
        item = {"frame": frame, "reference_status": ref["green_status"],
                "candidate_count": count, "fresh_pixel_observed": fresh is not None,
                "posterior_valid": posterior is not None,
                "posterior_predicted": bool(row["green"].get("predicted", False)),
                "observation_center": observed, "center_error_px": None,
                "posterior_center_error_px": None, "stage": None}
        status = ref["green_status"]
        if ref.get("review_state") == "pending" or status == "pending":
            stats["pending_excluded"] += 1
            item["evaluation_status"] = "pending_excluded"
        elif status == "uncertain":
            stats["uncertain_excluded"] += 1
            item["evaluation_status"] = "uncertain_excluded"
        elif status == "absent":
            stats["absent_reference_frames"] += 1
            stats["false_detections_on_absent"] += int(observed is not None)
            item["evaluation_status"] = "false_output" if observed is not None else "correct_absence"
        else:
            stats["visible_reference_frames"] += 1
            if posterior is not None:
                item["posterior_center_error_px"] = math.dist(posterior, ref["green_center"])
            if observed is not None:
                error = math.dist(observed, ref["green_center"])
                item["center_error_px"] = error
                errors.append(error)
                matched = error <= max(4, ref["green_radius"])
                if matched:
                    matched_errors.append(error)
                    stats["matched_direct_detections"] += 1
                else:
                    stats["wrong_location_detections"] += 1
            else:
                matched = False
            # Stage attribution always uses the confirmed pre-tracker pixel.
            # The posterior comparison above remains a separate diagnostic.
            if fresh is None:
                stage = STAGES[0] if count == 0 else STAGES[1]
            else:
                fresh_error = math.dist(fresh, ref["green_center"])
                stage = STAGES[3] if fresh_error <= max(4, ref["green_radius"]) else STAGES[2]
            stats["stage_counts"][stage] += 1
            item["stage"] = stage
            item["fresh_pixel_center_error_px"] = math.dist(fresh, ref["green_center"]) if fresh is not None else None
            item["evaluation_status"] = "matched" if matched else "missed_or_wrong"
        stats["sample_results"].append(item)
    stats["missed_or_wrong"] = stats["visible_reference_frames"] - stats["matched_direct_detections"]
    for prefix, values in (("matched", matched_errors), ("all_observed", errors)):
        stats[prefix + "_center_error_median_px"] = statistics.median(values) if values else None
        stats[prefix + "_center_error_max_px"] = max(values) if values else None
    return stats


def evaluate(rows, reference, *, kind=None, observation_source="green", width=None, height=None):
    """Legacy evaluation is unchanged; explicit green mode adds provenance strata.

    New mode requires native dimensions. Its group summaries are deliberately
    separate: tentative AI and human-reviewed labels are never pooled together.
    """
    if kind is None and observation_source == "green":
        return _evaluate_legacy(rows, reference)
    if kind != "green" or observation_source not in ("green", "pixel_measurement"):
        raise ValueError("Explicit evaluation supports kind=green and green/pixel_measurement sources")
    frames = validate_references(reference, width=width, height=height)
    missing = sorted(frames - rows.keys())
    if missing:
        raise ValueError(f"Replay missing {len(missing)} reference frames: {missing[:10]}")
    result = {}
    for split in sorted({ref["split"] for ref in reference}):
        refs = [ref for ref in reference if ref["split"] == split]
        groups = []
        keys = sorted({(ref.get("review_state", "ai_provisional"), ref.get("sample_group", "uniform")) for ref in refs})
        for review_state, sample_group in keys:
            items = [ref for ref in refs if (ref.get("review_state", "ai_provisional"), ref.get("sample_group", "uniform")) == (review_state, sample_group)]
            groups.append({"review_state": review_state, "sample_group": sample_group,
                           "green": _evaluate_green_group(rows, items, observation_source, width, height)})
        result[split] = {"sampled_frames": len(refs), "missing_replay_rows": 0, "groups": groups}
    return result


def _json_lines(path):
    with Path(path).open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                raise ValueError(f"Blank JSONL record at {path}:{number}")
            try:
                record = json.loads(line)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Invalid JSON at {path}:{number}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"Expected JSON object at {path}:{number}")
            yield record


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_sha256(path, expected):
    actual = file_sha256(path)
    if expected is not None and actual != expected:
        raise ValueError(f"SHA-256 mismatch for {path}")
    return actual


def read_replay(path, reference_frames, *, stream_audit=False, expected_frames=None,
                fps=None, width=None, height=None, expected_sha256=None):
    """Read one stream, retaining only requested frames; fail on invalid ordering.

    Audit durations are frame-count / declared nominal FPS, never exposure time.
    An expected frame count requires an exact complete [0, N) stream.
    """
    if expected_frames is not None:
        _integer(expected_frames, "expected_frames", 1)
    if stream_audit:
        _dimensions(width, height)
        if _finite(fps, "fps") <= 0:
            raise ValueError("fps must be positive")
    before = verify_sha256(path, expected_sha256)
    wanted, selected = set(reference_frames), {}
    first, previous, previous_timestamp, count = None, None, None, 0
    fresh_count = predicted_count = direct_count = missing_count = gap_events = 0
    no_fresh_run = longest_run = no_fresh_runs = 0
    states = Counter()
    for row in _json_lines(path):
        frame = _integer(row.get("frame"), "replay frame")
        if first is None:
            first = frame
        if previous is not None and frame <= previous:
            raise ValueError(f"Duplicate or out-of-order replay frame {frame}")
        if previous is not None and frame > previous + 1:
            gap_events += 1
            missing_count += frame - previous - 1
        if expected_frames is not None and frame != count:
            raise ValueError(f"Replay frame {frame} breaks expected complete sequence at {count}")
        if stream_audit:
            timestamp = _integer(row.get("timestamp_us"), "timestamp_us")
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                raise ValueError(f"Nonmonotonic nominal timestamp at frame {frame}")
            if abs(timestamp - frame * 1e6 / fps) > .51:
                raise ValueError(f"Unexpected nominal timestamp at frame {frame}")
            if row.get("replay_timestamps") != "nominal_source_fps":
                raise ValueError("Replay timestamp provenance must be nominal_source_fps")
            _, fresh, _ = _row_observations(row, width, height)
            fresh_count += int(fresh is not None)
            predicted_count += int(row["green"]["valid"] and row["green"].get("predicted", False))
            direct_count += int(_green_center(row, width, height) is not None)
            state = row["green"].get("state", "unreported")
            if state not in ("LOST", "CANDIDATE", "TRACKING", "unreported"):
                raise ValueError("Invalid green state")
            states[state] += 1
            if fresh is None:
                if no_fresh_run == 0:
                    no_fresh_runs += 1
                no_fresh_run += 1
                longest_run = max(longest_run, no_fresh_run)
            else:
                no_fresh_run = 0
            previous_timestamp = timestamp
        if frame in wanted:
            selected[frame] = row
        previous, count = frame, count + 1
    if expected_frames is not None and count != expected_frames:
        raise ValueError(f"Replay has {count} rows; expected {expected_frames}")
    missing = sorted(wanted - selected.keys())
    if missing:
        raise ValueError(f"Replay missing {len(missing)} reference frames: {missing[:10]}")
    if verify_sha256(path, before) != before:
        raise ValueError("Replay changed during evaluation")
    audit = {"status": "PASS", "row_count": count, "first_frame": first,
             "last_frame": previous, "retained_reference_rows": len(selected),
             "gap_events": gap_events, "missing_sequence_frames": missing_count,
             "replay_sha256": before}
    if stream_audit:
        audit.update({"fresh_confirmed_pixel_frames": fresh_count,
                      "green_direct_frames": direct_count, "green_predicted_valid_frames": predicted_count,
                      "green_state_counts": dict(sorted(states.items())),
                      "intervals_without_fresh_observation": no_fresh_runs,
                      "longest_without_fresh_observation_frames": longest_run,
                      "longest_without_fresh_observation_nominal_seconds": longest_run / fps,
                      "timing_provenance": "frame index / declared nominal FPS; not exposure timing",
                      "scope": "Output availability and integrity only; gaps are not labelled missed detections"})
    return selected, audit

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for key in ("reference", "replay", "output"):
        ap.add_argument("--" + key, type=Path, required=True)
    ap.add_argument("--kind", choices=("green",))
    ap.add_argument("--observation-source", choices=("green", "pixel_measurement"), default="green")
    ap.add_argument("--width", type=int)
    ap.add_argument("--height", type=int)
    ap.add_argument("--stream-audit", action="store_true")
    ap.add_argument("--expected-frames", type=int)
    ap.add_argument("--fps", type=float)
    ap.add_argument("--replay-sha256")
    ap.add_argument("--reference-sha256")
    args = ap.parse_args()
    strict = args.kind is not None or args.observation_source != "green" or args.stream_audit
    try:
        if strict and args.kind != "green":
            raise ValueError("Explicit observation source/stream audit requires --kind green")
        ref_hash = verify_sha256(args.reference, args.reference_sha256)
        refs = list(_json_lines(args.reference))
        if strict:
            frames = validate_references(refs, width=args.width, height=args.height)
        else:
            frames = {_integer(ref.get("frame"), "reference frame") for ref in refs}
            if len(frames) != len(refs):
                raise ValueError("Duplicate reference frame")
        rows, audit = read_replay(args.replay, frames, stream_audit=args.stream_audit,
                                  expected_frames=args.expected_frames, fps=args.fps,
                                  width=args.width, height=args.height,
                                  expected_sha256=args.replay_sha256)
        results = evaluate(rows, refs, kind=args.kind, observation_source=args.observation_source,
                           width=args.width, height=args.height)
        verify_sha256(args.reference, ref_hash)
        out = {"replay": str(args.replay), "reference": str(args.reference),
               "scope": "Sparse reviewed references; uncertain/pending excluded; same-video regression, not independent-scene accuracy.",
               "results": results}
        if strict:
            reviewed = (bool(refs) and all(ref.get("review_state") == "human_reviewed"
                                          and ref["green_status"] != "pending" for ref in refs)
                        and any(ref["green_status"] in ("visible", "absent") for ref in refs))
            out.update({"kind": args.kind, "observation_source": args.observation_source,
                        "reference_sha256": ref_hash, "stream_audit": audit,
                        "formal_quality_status": "PASS" if reviewed else "BLOCKED",
                        "quality_acceptance_status": "NOT_RUN",
                        "formal_quality_limitation": "Status describes evaluation readiness, not algorithm acceptance. Human review is required; AI provisional groups remain tentative."})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x" if strict else "w", encoding="utf-8") as stream:
            json.dump(out, stream, indent=2, allow_nan=False)
            stream.write("\n")
    except (ValueError, KeyError, TypeError, OSError) as exc:
        ap.exit(2, f"Error: {exc}\n")
    print(json.dumps(results, indent=2, allow_nan=False))
if __name__=='__main__':main()
