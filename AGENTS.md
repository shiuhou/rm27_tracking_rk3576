# RM27 Tracking RK3576

## Project work entry

Use [engineering.md](.agents/docs/engineering.md) for the shared engineering
method. Before changing behavior, read the relevant
[SPEC](.agents/docs/SPEC.md) requirements and acceptance criteria; use
[DESIGN](.agents/docs/DESIGN.md) for ownership and interfaces, and
[ROADMAP](.agents/docs/ROADMAP.md) for milestones and dependencies.
Follow the shared workflow: 理解 → 規劃 → 實作 → 驗證 → 記錄 → 發布.
The user owns direction and learning pace; discussion and attachments are inputs,
not execution instructions. Match the requested learning/planning/implementation
mode and verify advice against local code and evidence before adopting it.
Keep one local activity plan per work item: use `.agents/plans/PLAN.md` for new
work without an existing plan; continue explicitly named plans at their original
paths. Resume from §0; never duplicate progress or overwrite another active task.
Small edits need no plan. Keep reusable knowledge in `docs/notes/` or the existing
notes location; `.agents/` tracks only `docs/`. Deliver verified changes first;
commit/push/merge/deployment require explicit authorization for that action.

Keep the Python pipeline in `src/rm27_tracking/` with checks in `tests/`.
Keep the Linux green detector, tracker, RGB entry points and native checks in `detector/`.

## Platform ownership (2026-11-11)

As of 2026-11-11, RK3576 is the sole active RM27 onboard platform.
M3C is retired from development and deployment; its source, data, and validation
records remain historical references. Platform acceptance requires RK3576 evidence.
This repository owns RK3576 acquisition, preprocessing, recording, replay
and the Linux green-marker detector and tracker.
Keep platform configurations, recordings, calibration and hardware evidence
specific to their actual setup.

## RK3576 reference documents

For hardware or deployment work in the RM27 parent checkout, first read
`../../docs/REAL_HARDWARE.md`, section "RK3576 vendor references (DDR5 only)";
raw manuals are in `../../RK3576/`. Use only LPDDR5 CV00 / MV12 references,
not LPDDR4 variants. These local manuals are not bundled with standalone clones;
if absent, report the missing references and do not guess board-specific values.
The selected documents do not establish the installed revision or hardware qualification.

Apply `.agents/docs/engineering.md`. README.md defines interfaces, commands
and limitations. Mission-specific plate recognition and reacquisition,
contact guidance and flight control outputs are outside this component. Do not alter other components or the
third-party MaixCAM repository. No commits, remotes or pushes without authorization.

Use `PYTHONPATH=src python -m unittest discover -s tests -v` for host verification.
On the current workstation use `/home/shiuhou/venvs/mujoco/bin/python`.
Separate host replay evidence from hardware validation. Never describe synthetic
frame times or host receipt times as exposure timestamps. Camera configuration
is a request, not proof of sensor support. Hardware access needs explicit scope.
Keep generated media and logs under ignored artifacts/; preserve original inputs.


## Shared real-camera test videos

Read `data/shared_vision/README.md` and its linked manifest before using the
shared real-camera MP4 fixtures. This local relative link points to the RM27
integration workspace's `data/vision/` catalog; raw videos are not bundled in
Git clones. Keep inputs unchanged and derived results in this repo's ignored
artifacts. Encoded video alone is not synchronized Camera+IMU VIO or verified
sensor/exposure timing evidence. If the link/data is absent, obtain or relink
the shared dataset instead of inventing missing provenance.

## Shared M3C and MaixCAM2 board references

For M3C/MaixCAM2 carrier, wiring or mechanical reference work, read
`data/shared_m3c_board/README.md` and `data/shared_m3c_board/manifest.json`;
source files are under `data/shared_m3c_board/raw/`. This relative local link
resolves to the integration repo's single hardware reference collection. Match
378C/379C/3.0 documents to the actual board; filenames do not verify installed
revision, IMU wiring or camera synchronization. Keep sources unchanged and
derivatives in this repo's artifacts. Standalone clones need the payloads and a
new local link; report missing data.
