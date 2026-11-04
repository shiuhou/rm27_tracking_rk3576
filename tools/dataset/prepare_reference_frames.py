#!/usr/bin/env python3
"""Prepare hash-verified native frames for sparse, independent image review.

Selection depends only on source frame numbers. No detector output is used to
choose images or create labels. Encoded-video time is nominal, not exposure time.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re

import cv2


ROOT = Path(__file__).resolve().parents[2]
DIAGNOSTIC_CASES = {"clip01": 3160, "clip02": 2112,
                    "clip03": 5535, "clip04": 8681}
SEMANTIC_FIELDS = ("frame", "timestamp_us", "green", "pixel_measurement", "candidates")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def _lines_bytes(rows) -> bytes:
    return b"".join((json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n").encode()
                    for row in rows)


def _open_video(path: Path):
    if hasattr(cv2, "CAP_PROP_N_THREADS"):
        return cv2.VideoCapture(str(path), cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 1])
    # OpenCV 4.6 does not expose the decoder-thread property. Its FFmpeg
    # backend accepts codec options while opening; restore the caller's env.
    previous = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (previous + "|" if previous else "") + "threads;1"
    try:
        return cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)
    finally:
        if previous is None:
            os.environ.pop("OPENCV_FFMPEG_CAPTURE_OPTIONS", None)
        else:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = previous


class _Budget:
    """Reserve space for the single final success/failure record."""
    def __init__(self, maximum: int):
        if isinstance(maximum, bool) or maximum < 32768:
            raise ValueError("max_bytes must be at least 32768 (includes failure evidence)")
        self.maximum, self.used = maximum, 0

    def write(self, path: Path, payload: bytes, *, final: bool = False):
        limit = self.maximum if final else self.maximum - 16384
        if self.used + len(payload) > limit:
            raise OSError("recording output byte limit exceeded")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            written = stream.write(payload)
            if written != len(payload):
                raise OSError("short output write")
        self.used += len(payload)


def verify_baseline(manifest_path: Path, baseline: Path, *, repo_root: Path = ROOT) -> dict:
    """Verify existing immutable inputs; retain file digests for a second pass."""
    catalog = json.loads(manifest_path.read_text())
    identity_path, summary_path = baseline / "run_identity.json", baseline / "summary.json"
    identity = json.loads(identity_path.read_text())
    summary = json.loads(summary_path.read_text())
    if summary.get("status") != "PASS" or identity["dataset_id"] != catalog["dataset_id"]:
        raise ValueError("baseline dataset/status mismatch")
    files = {manifest_path: identity["manifest_sha256"],
             baseline / "diagnostic.conf": identity["configuration_sha256"]}
    for name, expected in identity["source_sha256"].items():
        path = (repo_root / name).resolve()
        if not path.is_relative_to(repo_root.resolve()):
            raise ValueError("baseline source path escapes repository")
        files[path] = expected
    reports = {item["clip_id"]: item for item in summary["clips"]}
    clips = catalog["clips"]
    if len({clip["id"] for clip in clips}) != len(clips):
        raise ValueError("duplicate manifest clip id")
    semantic = {}
    for clip in clips:
        clip_id = clip["id"]
        if re.fullmatch(r"[A-Za-z0-9_-]+", clip_id) is None:
            raise ValueError("unsafe manifest clip id")
        encoded = clip["encoded_video"]
        count, fps = encoded["reported_frame_count"], encoded["reported_fps"]
        if (isinstance(count, bool) or not isinstance(count, int) or count <= 0 or
                not math.isfinite(fps) or fps <= 0 or
                any(isinstance(encoded[key], bool) or not isinstance(encoded[key], int) or
                    encoded[key] <= 0 for key in ("width", "height"))):
            raise ValueError("invalid encoded video metadata")
        run_path = baseline / clip_id / "run.json"
        run = json.loads(run_path.read_text())
        if run["status"] != "PASS" or run["input_sha256"] != clip["sha256"]:
            raise ValueError(f"baseline run mismatch: {clip_id}")
        cli = run["cli_summary"]
        if (cli["frames"] != count or cli["source_frames_read"] != count or
                cli["source_width"] != encoded["width"] or
                cli["source_height"] != encoded["height"] or
                cli["frame_step"] != 1 or not cli["native_resolution"]):
            raise ValueError(f"baseline native frame contract mismatch: {clip_id}")
        source = manifest_path.parent / clip["path"]
        if source.stat().st_size != clip["size_bytes"]:
            raise ValueError(f"source size mismatch: {clip_id}")
        files[source] = clip["sha256"]
        detections = baseline / clip_id / "detections.jsonl"
        digest, rows = hashlib.sha256(), 0
        with detections.open() as stream:
            for rows, line in enumerate(stream, 1):
                record = json.loads(line)
                if record["frame"] != rows - 1:
                    raise ValueError(f"missing, duplicate, or out-of-order detector row: {clip_id}")
                if abs(record["timestamp_us"] - round((rows-1)*1e6/fps)) > 1:
                    raise ValueError(f"nominal timestamp mismatch: {clip_id}")
                selected = {key: record[key] for key in SEMANTIC_FIELDS}
                digest.update(json.dumps(selected, sort_keys=True,
                                         separators=(",", ":")).encode() + b"\n")
        if rows != count or digest.hexdigest() != reports[clip_id]["ordered_semantic_output_sha256"]:
            raise ValueError(f"baseline detection semantic hash/count mismatch: {clip_id}")
        semantic[clip_id] = digest.hexdigest()
        files[detections] = sha256(detections)
        files[run_path] = sha256(run_path)
    for path, expected in files.items():
        if sha256(path) != expected:
            raise ValueError(f"source/baseline SHA-256 mismatch: {path}")
    files[identity_path] = sha256(identity_path)
    files[summary_path] = sha256(summary_path)
    return dict(catalog=catalog, files=files, semantic=semantic)


def review_html(samples: list[dict], references: list[dict]) -> bytes:
    """One native source image at a time; labels are hidden until requested."""
    data = json.dumps(dict(samples=samples, references=references), ensure_ascii=False,
                      allow_nan=False).replace("<", "\\u003c")
    return ("""<!doctype html><html lang="zh-Hant"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>原生影格覆核</title><style>
body{font:16px sans-serif;background:#17202b;color:#eee;margin:20px}button,select,input{font:inherit;margin:5px;padding:5px}#stage{position:relative;display:inline-block;max-width:100%}img{display:block;max-width:100%;height:auto}svg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}textarea{width:min(90vw,900px);height:65px}a{color:#8ce}#meta{white-space:pre-wrap}label{display:inline-block}</style>
<h1>原生影格覆核</h1><p>AI 初標／人工覆核分開保留。原圖沒有辨識器疊圖；預設隱藏參考標記。名義影片時間不是曝光時間。四片均為已看過的回歸資料。</p>
<select id="clip"></select><button id="previous">上一張</button><button id="next">下一張</button><select id="frame"></select>
<label><input type="checkbox" id="overlay">顯示參考標記</label><p id="meta"></p>
<div id="stage"><img id="image"><svg id="mark"></svg></div><p>
<label>可見性<select id="status"><option value="pending">待標註</option><option value="visible">可見</option><option value="absent">不在畫面</option><option value="uncertain">不確定</option></select></label>
<label>中心 x<input id="x" type="number" step="0.1"></label><label>y<input id="y" type="number" step="0.1"></label><label>半徑 px<input id="radius" type="number" min="0" step="0.1"></label>
<p>可點擊原圖設定中心。修改只保存在本頁記憶體；下載後交回參考 JSONL，再重新評估。未修改的列不會自動變成人工覆核。</p>
<textarea id="notes" placeholder="覆核備註"></textarea><p><button id="save">確認本張人工覆核</button><button id="download">下載本片參考 JSONL</button><span id="message"></span></p>
<script>const DATA=__DATA__;const $=x=>document.getElementById(x);let current=0;
const clips=[...new Set(DATA.samples.map(s=>s.clip_id))];for(const c of clips)$('clip').add(new Option(c,c));
function ref(s){return DATA.references.find(r=>r.clip_id===s.clip_id&&r.frame===s.frame)}
function draw(){const s=DATA.samples[current],r=ref(s);$('image').src=s.image_path;$('meta').textContent=`${s.clip_id} / frame ${s.frame} / ${s.time_s.toFixed(3)} s nominal / ${s.sample_group}\n${s.width}×${s.height} | ${r.annotation_source} / ${r.review_state}`;
$('status').value=r.green_status;const p=r.green_center||[];$('x').value=p[0]??'';$('y').value=p[1]??'';$('radius').value=r.green_radius??'';$('notes').value=r.notes||'';drawMark();}
function drawMark(){const s=DATA.samples[current],r=ref(s);$('mark').setAttribute('viewBox',`0 0 ${s.width} ${s.height}`);$('mark').innerHTML=$('overlay').checked&&r.green_status==='visible'&&r.green_center?`<circle cx="${r.green_center[0]}" cy="${r.green_center[1]}" r="${r.green_radius||4}" fill="none" stroke="#ffda54" stroke-width="2"/>`:'';}
function chooseClip(){const options=DATA.samples.map((s,i)=>[s,i]).filter(([s])=>s.clip_id===$('clip').value);$('frame').innerHTML='';for(const [s,i] of options)$('frame').add(new Option(`${s.frame} (${s.sample_group})`,i));current=options[0][1];draw();}
$('clip').onchange=chooseClip;$('frame').onchange=()=>{current=Number($('frame').value);draw()};$('overlay').onchange=drawMark;
function step(delta){const v=$('frame');v.selectedIndex=Math.max(0,Math.min(v.length-1,v.selectedIndex+delta));v.onchange()};$('previous').onclick=()=>step(-1);$('next').onclick=()=>step(1);
$('image').onclick=e=>{const s=DATA.samples[current],b=e.target.getBoundingClientRect();$('x').value=((e.clientX-b.left)*s.width/b.width).toFixed(1);$('y').value=((e.clientY-b.top)*s.height/b.height).toFixed(1)};
$('save').onclick=()=>{const s=DATA.samples[current],r=ref(s),status=$('status').value,x=Number($('x').value),y=Number($('y').value),radius=Number($('radius').value);if(status==='pending'||status==='visible'&&(!$('x').value||!$('y').value||!$('radius').value||![x,y,radius].every(Number.isFinite)||x<0||x>=s.width||y<0||y>=s.height||radius<=0)){ $('message').textContent='請提供有效的可見性／中心／半徑';return;}Object.assign(r,{green_status:status,green_center:status==='visible'?[x,y]:null,green_radius:status==='visible'?radius:null,annotation_source:'human_review',review_state:'human_reviewed',notes:$('notes').value});$('message').textContent='本張已確認；請下載保存';draw()};
$('download').onclick=()=>{const rows=DATA.references.filter(r=>r.clip_id===$('clip').value),blob=new Blob([rows.map(r=>JSON.stringify(r)).join('\\n')+'\\n'],{type:'application/x-ndjson'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=$('clip').value+'-reference-reviewed.jsonl';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)};chooseClip();</script></html>
""".replace("__DATA__", data)).encode()


def render_review(output: Path, samples: list[dict] | None = None,
                  references: list[dict] | None = None) -> Path:
    """Regenerate only the review page after independent annotation updates."""
    output = Path(output)
    if samples is None:
        samples = [json.loads(line) for line in (output / "samples.jsonl").read_text().splitlines()]
    if references is None:
        references = []
        for clip_id in dict.fromkeys(item["clip_id"] for item in samples):
            references.extend(json.loads(line) for line in
                              (output / clip_id / "reference.jsonl").read_text().splitlines())
    target = output / "index.html"
    payload = review_html(samples, references)
    summary = json.loads((output / "summary.json").read_text())
    total = sum(p.stat().st_size for p in output.rglob("*") if p.is_file())
    if total - (target.stat().st_size if target.exists() else 0) + len(payload) > summary["max_bytes"]:
        raise OSError("review page would exceed output byte limit")
    target.write_bytes(payload)
    return target


def prepare(manifest: Path, baseline: Path, output: Path, *, stride: int = 360,
            max_bytes: int = 512 * 1024 * 1024, repo_root: Path = ROOT) -> dict:
    if isinstance(stride, bool) or not isinstance(stride, int) or stride <= 0:
        raise ValueError("stride must be a positive integer")
    budget = _Budget(max_bytes)
    output.mkdir(parents=True, exist_ok=False)
    samples, references, reports = [], [], []
    summary = dict(status="FAIL", schema_version=1, max_bytes=max_bytes, stride=stride,
                   manifest=str(manifest.resolve()), baseline=str(baseline.resolve()),
                   scope="Sparse unlabelled native frames; regression_seen, no independent test claim",
                   opencv_version=cv2.__version__, source_timestamps="nominal_source_fps",
                   sample_count=0, clips=reports)
    try:
        verified = verify_baseline(manifest, baseline, repo_root=repo_root)
        summary["dataset_id"] = verified["catalog"]["dataset_id"]
        summary["input_sha256"] = {str(path.resolve()): value for path, value in verified["files"].items()}
        summary["detector_semantic_sha256"] = verified["semantic"]
        cv2.setNumThreads(1)
        compatible = cv2.__version__ == verified["catalog"].get("validation_environment", {}).get("opencv")
        for clip in verified["catalog"]["clips"]:
            clip_id, encoded = clip["id"], clip["encoded_video"]
            width, height, count, fps = (encoded[key] for key in
                                        ("width", "height", "reported_frame_count", "reported_fps"))
            selected = dict.fromkeys(range(0, count, stride), "uniform")
            diagnostic = DIAGNOSTIC_CASES.get(clip_id)
            if diagnostic is not None and diagnostic < count:
                selected.setdefault(diagnostic, "diagnostic_case")
            if len(samples) + len(selected) > 132:
                raise ValueError("selection exceeds the 132 native-image limit")
            pixel_checks = {item["frame_index"]: item["pixel_sha256"] for item in
                            clip.get("validation", {}).get("sample_pixel_hashes", [])}
            capture = _open_video(manifest.parent / clip["path"])
            if not capture.isOpened():
                capture.release()
                raise RuntimeError(f"cannot open source: {clip_id}")
            decoded, checked = 0, 0
            try:
                while True:
                    ok, frame = capture.read()
                    if not ok:
                        break
                    if frame.shape != (height, width, 3) or frame.dtype.name != "uint8":
                        raise ValueError(f"native source dimension/type mismatch: {clip_id}/{decoded}")
                    if decoded >= count:
                        raise ValueError(f"source has extra frames: {clip_id}")
                    pixel_hash = None
                    if decoded in selected or compatible and decoded in pixel_checks:
                        pixel_hash = hashlib.sha256(memoryview(frame)).hexdigest()
                    if compatible and decoded in pixel_checks:
                        if pixel_hash != pixel_checks[decoded]:
                            raise ValueError(f"catalog native BGR pixel hash mismatch: {clip_id}/{decoded}")
                        checked += 1
                    if decoded in selected:
                        ok, png = cv2.imencode(".png", frame)
                        if not ok:
                            raise OSError("PNG encoding failed")
                        image_path = f"{clip_id}/frames/{decoded:06d}.png"
                        payload = png.tobytes()
                        budget.write(output / image_path, payload)
                        sample = dict(clip_id=clip_id, frame=decoded, time_s=decoded/fps,
                                      sample_group=selected[decoded], width=width, height=height,
                                      source_sha256=clip["sha256"], image_path=image_path,
                                      image_sha256=hashlib.sha256(payload).hexdigest(),
                                      native_bgr_pixel_sha256=pixel_hash, kind="native_BGR8_to_lossless_PNG")
                        samples.append(sample)
                        references.append(dict(clip_id=clip_id, frame=decoded, time_s=decoded/fps,
                                               split="regression_seen", sample_group=selected[decoded],
                                               green_status="pending", green_center=None, green_radius=None,
                                               armor_status="uncertain", armor_center=None, armor_bbox=None,
                                               annotation_source="unreviewed", review_state="pending", notes=""))
                    decoded += 1
            finally:
                capture.release()
            if decoded != count:
                raise ValueError(f"incomplete source decode: {clip_id}: {decoded} != {count}")
            reports.append(dict(clip_id=clip_id, decoded_frames=decoded,
                                uniform_samples=sum(value == "uniform" for value in selected.values()),
                                diagnostic_samples=sum(value == "diagnostic_case" for value in selected.values()),
                                catalog_pixel_checks=checked,
                                catalog_pixel_check_status="PASS" if compatible else "NOT_RUN decoder_version_differs"))
        for path, expected in verified["files"].items():
            if sha256(path) != expected:
                raise ValueError(f"input changed during extraction: {path}")
        budget.write(output / "samples.jsonl", _lines_bytes(samples))
        for clip_id in dict.fromkeys(item["clip_id"] for item in samples):
            budget.write(output / clip_id / "reference.jsonl",
                         _lines_bytes(item for item in references if item["clip_id"] == clip_id))
        budget.write(output / "index.html", review_html(samples, references))
        summary.update(status="PASS", sample_count=len(samples),
                       uniform_samples=sum(item["sample_group"] == "uniform" for item in samples),
                       diagnostic_samples=sum(item["sample_group"] == "diagnostic_case" for item in samples),
                       inputs_verified_before_and_after=True,
                       formal_quality_status="BLOCKED pending independent annotation and human review",
                       script_sha256=sha256(Path(__file__)))
    except Exception as error:
        summary.update(status="FAIL", sample_count=len(samples), error=f"{type(error).__name__}: {error}")
        raise
    finally:
        # A failed run retains its complete images and a single explicit FAIL.
        # Count actual bytes, including a short/partial write if storage failed.
        budget.used = sum(path.stat().st_size for path in output.rglob("*") if path.is_file())
        summary["bytes_before_summary"] = budget.used
        budget.write(output / "summary.json", _json_bytes(summary), final=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--baseline", type=Path, default=ROOT / "artifacts/shared-real-green-replay")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--stride", type=int, default=360)
    parser.add_argument("--max-bytes", type=int, default=512*1024*1024)
    args = parser.parse_args()
    try:
        summary = prepare(args.manifest, args.baseline, args.output,
                          stride=args.stride, max_bytes=args.max_bytes)
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        parser.exit(1, f"prepare_reference_frames: {error}\n")
    print(json.dumps({key: summary[key] for key in
                      ("status", "sample_count", "uniform_samples", "diagnostic_samples")}))


if __name__ == "__main__":
    main()
