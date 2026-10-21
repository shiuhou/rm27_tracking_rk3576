# Confirmed image measurement and tracking posterior

`TargetEstimate.green.center_x/y` remains the original TemporalTracker Kalman
posterior. `predicted=false` means a current candidate updated that track; it
does not make its center an unfiltered centroid or remove temporal correlation.
All existing top-level and `green` JSON fields keep their previous meaning.

The additive `TargetEstimate.pixel_measurement` / JSON `pixel_measurement`
product provides a separate selected current-image point before the tracker
position correction. `GreenLightDetector::process()` and `process_region()`
actually populate it; it is not a separate demo detector. The point and bbox
come from the same selected green candidate in source-image pixel coordinates,
including restoration of any ROI offset. Appearance processing and selection
remain causal and may depend on the tracking ROI; “pre-tracker” does not mean
that target selection is independent of history.

It is valid only when classical detection ran on this input, the original
tracker confirmed a valid non-predicted green detection, and exactly one
candidate was selected. Tentative candidates, pure predictions, skipped
detection and missing candidates produce no valid pixel measurement. The
detector does not change its scores, confirmation rules, association gates,
miss/age budgets, scheduling, capture limit, NPU or pose policy for this API.

## Provenance and delivery

`timestamp_us` is the source timestamp supplied to the detector.
`stamp_pixel_measurement_source()` attaches the sequence, PTS payload and their
source labels only when its input time identifies that same frame. It does not
relabel reception as exposure:

- The normal `main.cpp` camera entry calls this helper with an application frame
  index and `host_after_camera_read`; it does not claim an SDK frame sequence or
  sensor PTS. Its existing configurable JSON logging frequency is unchanged.
- The VIN entry attaches the actual SDK sequence and PTS payload, with
  `VIN_receive_host_monotonic` as the measurement time source. A valid PTS
  payload does not establish its scale or physical exposure phase.
- Cached-video input is labeled `cached_video_sequence` and
  `cached_video_submit_host_monotonic`; it is not a live VIN measurement.

`exposure_timestamp_valid` remains false in these entry points. A physical
exposure timestamp requires separate device-specific evidence.

`predict_full180_output()` clears the image-measurement product rather than
copying it into each new 180 Hz prediction. The actual full180 `control_loop`
uses `PixelMeasurementDelivery` only after taking a new detector snapshot. It
forwards each confirmed source identity once, retains its source timestamp and
sequence, and reports the elapsed `measurement_age_us`. Duplicate/reversed
sequences, repeated source timestamps, future sources and unchanged snapshots
do not create new measurements. Prediction fields keep their old semantics;
an output can therefore contain a predicted `green` track and a separately
identified newly delivered measurement from its actual earlier input frame.
Delivery does not impose a maximum age. Its `valid` flag establishes the
measurement's confirmed source identity, not freshness for a particular
consumer. Every consumer must independently gate `measurement_age_us` against
its own latency budget before updating an estimator.

The latest-snapshot policy remains unchanged. Replaced snapshots may lose a
measurement; the API does not replay discarded frames or claim lossless
delivery. Consumers should keep source identities and timestamps, rather than
treating the new output timestamp as a new image observation. Camera stream
restart/re-identification requires rebuilding the pipeline and delivery state;
the old stream's sequence and clock high-water marks must not be reused.

An explicit profile with `highfps.green_hz` equal to `camera.fps` measures each
strictly new frame taken by `vision_loop` once, rather than applying a second same-frequency
deadline. Thus a real 180 Hz stream quantized/jittered to 5.5/6.0 ms does not lose
measurements solely because a frame arrived just before that second deadline.
The actual VIN `vision_loop` uses this gate. The existing latest-slot policy can
still replace captured frames before the loop takes them; this change does not
claim lossless capture-to-detection delivery. The default 90 Hz measurement path
still uses the original 90 Hz rational deadlines. Source submission rejects
duplicate/reversed SDK sequences before publication, even if their reception
time is newer; the original sequence anomaly statistics remain and
`rejected_source_frames` separately counts these rejected submissions. This is
not a source-rate increase, a catch-up burst or acceptance of predicted frames.
The configurable selection can be expressed as `camera.fps=180` with
`highfps.green_hz=180`; no hardware profile default is changed here.

Accepted new source frames require strictly increasing receive timestamps as
well as sequence numbers. An equal receive timestamp is rejected without
consuming the new sequence. Missing-frame statistics use the highest observed
sequence, so `10,9,11` reports a reverse arrival but no missing frame; raw
arrival-order duplicate, reverse and PTS anomaly counters retain their original
meaning. `received` counts all observed submissions, including ones subsequently
rejected; `last_sequence`/`last_pts` describe that raw arrival order. The separate
highest-sequence gap baseline never moves backwards. Submission acceptance and
one-time measurement delivery each retain their own accepted source identity;
neither borrows the last rejected packet's timestamp or sequence as its baseline.
`HighFpsPipeline::submit()` requires one producer, as in the actual VIN
capture entry, or external serialization covering the entire call. Its stats
mutex does not establish publication ordering across multiple producers. SDK
frame destruction remains outside that mutex.

## Noise and downstream estimation

The detector supplies no empirical candidate position covariance. The default
is `position_covariance_valid=false` and JSON `position_covariance_px2=null`.
Candidate quality, track posterior covariance, zeroed angular covariance and
circle residuals are not substituted for image-measurement variance. This
change imports no MuJoCo-derived noise constant or camera calibration.

An application that has an independently calibrated model may call
`set_pixel_measurement_covariance()` with a finite symmetric positive
semidefinite 2x2 matrix in source pixel² and a nonempty model source identifier.
The helper requires exact symmetry of the supplied float entries, nonnegative
diagonal entries and a nonnegative determinant, evaluated using double-precision
products. It uses no absolute tolerance floor and does not silently project or
symmetrize an indefinite input; rejection leaves a previously attached model
unchanged. Near-zero and rank-deficient PSD matrices are allowed. This validates
the matrix representation, not the empirical accuracy of that model. The
existing camera entries do not call it, so their uncertainty
stays explicitly unavailable.

`kalman.measurement_position` is a **position variance in pixel²**, not a pixel
standard deviation. For example, the unchanged hardware profile value 4 means
sigma=2 pixels in that profile's source coordinates. A downstream
`pixel_sigma_px` field denotes a standard deviation instead. Resampling by
`S=diag(sx,sy)` transforms covariance as `R_new=S R_old S^T`; scalar variance
therefore scales by the square of its pixel scale. A conservative isotropic
bound can use the larger axis scale when the original noise model supports it.

The value `(0.5*max(1344/1280,760/720))² ≈ 0.278549` pixel² is only a particular
MuJoCo development calibration example: 0.5 is a sigma in that simulation's
1280x720 pixel coordinates. It is **not a measured OS04A10 noise variance and
must not become a board default**. Camera, exposure, lens, resolution, codec,
candidate type and motion can change the model. A device configuration needs
its own measured calibration and provenance; no existing hardware noise value
is changed by this interface or scheduling patch.

A downstream estimator can retain the posterior for association/display and
consume each confirmed `pixel_measurement` once for its own position update.
This separates two data products; it does not establish that one choice gives
better control. A downstream implementation must still match coordinate
transforms, timestamps, noise assumptions and correlation treatment.

The existing `safe_for_control`, `angles_valid` and `pose.valid` are never
promoted by the new product. In the uncalibrated full180 path they stay false.
Valid image pixels are not calibrated bearings or hardware-control approval.

## Verification boundary

`tests/test_pixel_measurement.cpp` covers confirmation, source-coordinate/raw
versus posterior separation on real RGB pixel operations, unknown uncertainty,
same-frame stamping, one-time delivery, skipped/predicted/duplicate/reversed
source rejection, covariance validation and JSON representation. It is wired
into the normal `tests/CMakeLists.txt` with the existing full numerical sources
and thread-capable host configuration.

For hosts whose MinGW lacks `std::thread`, run
`python tests/run_pixel_measurement_host.py --compiler-bin <GNU-bin>`.
This fallback compiles the original detector/fusion/JSON sources and extracts
unchanged numerical NV21 helper definitions, omitting unused asynchronous
service declarations/definitions. Generated files, hashes, executable and test
record are under ignored `build/pixel-measurement-host/`. No thread substitute
or board SDK executes in that test. It does not certify the complete threaded
pipeline, a MaixCDK/ARM build, VIN timing or board operation.

The normal application still compiles all main sources through its existing
MaixCDK build. This change requires a source rebuild because the C++
`TargetEstimate` value type has an added member; it does not claim binary ABI
compatibility with already-built clients. JSON is additive, with the existing
schema-v2 field meanings retained and the nested measurement schema marked 1.

## Stateless RGB candidate collection

`GreenLightDetector::detect_candidates(Image&) const` is a separate low-level
candidate API. It creates a fresh local collector for the current RGB image and
returns `GreenLightCandidateDebug` values before temporal tracking or selection.
It does not call the temporal tracker, candidate-hypothesis update, NPU, armor,
output prediction or confirmation path; it leaves the calling detector's tracker,
source/output cache and `last_candidates()` unchanged. Repeated calls on the same
pixels and configuration are independent of earlier tracking history.

These are **unconfirmed candidates**, with `selected=false`. They are not
`PixelMeasurement` or `TargetEstimate` products and carry no timestamp, source
identity, calibrated bearing, uncertainty or control-safety claim. Consumers
own association, source stamping, deduplication, freshness and confirmation.
Candidate scores are not position covariances. Existing stateful detection and
fail-closed output defaults remain unchanged. The method accepts a complete RGB
image; a cropped image requires the caller to transform coordinates and camera
calibration explicitly.

The numerical host regression verifies current-image/history independence,
unchanged temporal state and cache, and absence of selected candidates. It does
not qualify a downstream estimator or demonstrate board throughput.
