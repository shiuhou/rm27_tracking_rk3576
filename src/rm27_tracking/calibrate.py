"""Bounded checkerboard calibration at native resolution; no automatic undistortion.

The evaluation views estimate only their board poses using frozen intrinsics.
Image correspondences and a low reprojection error cannot validate board metrology,
focus stability, rolling shutter, or a deployed camera installation.
"""
import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path

import cv2
import numpy as np

from .pipeline import positive
from .replay import sha256_file

ESTIMATE_LABEL = 'Calibration estimate requiring physical validation'
DISTORTION_ORDER = ['k1', 'k2', 'p1', 'p2', 'k3']


def _image_size(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(type(item) is not int or item <= 0 for item in value)):
        raise ValueError('calibration image_size must contain two positive integer dimensions')
    return tuple(value)


def _finite_array(value, shape, name):
    try:
        array = np.asarray(value, dtype=object)
        if array.shape != shape or any(type(item) not in (int, float) for item in array.flat):
            raise ValueError(f'invalid calibration {name} shape or numeric type')
        array = array.astype(np.float64)
    except (TypeError, OverflowError) as exc:
        raise ValueError(f'invalid calibration {name}') from exc
    if not np.isfinite(array).all():
        raise ValueError(f'nonfinite calibration {name}')
    return array


def _validate_export(export, image_size):
    """Validate our estimates at a downstream boundary, without certifying optics."""
    size = _image_size(image_size)
    if not isinstance(export, dict) or type(export.get('schema_version')) is not int or export['schema_version'] != 1:
        raise ValueError('unsupported calibration schema')
    if _image_size(export.get('image_size')) != size:
        raise ValueError('calibration resolution mismatch; scaling or cropping requires a separate calibration decision')
    if export.get('distortion_model') != 'opencv_pinhole_5' or export.get('distortion_order') != DISTORTION_ORDER:
        raise ValueError('unsupported calibration distortion model or coefficient order')
    if export.get('label') != ESTIMATE_LABEL or export.get('physical_validation') != 'BLOCKED':
        raise ValueError('calibration estimate must retain its physical-validation limitation')
    matrix = _finite_array(export.get('camera_matrix'), (3, 3), 'camera_matrix')
    _finite_array(export.get('distortion_coefficients'), (5,), 'distortion_coefficients')
    if (matrix[0, 0] <= 0 or matrix[1, 1] <= 0
            or not np.allclose(matrix[[0, 1, 2, 2, 2], [1, 0, 0, 1, 2]], [0, 0, 0, 0, 1], rtol=0, atol=1e-12)
            or not 0 <= matrix[0, 2] < size[0] or not 0 <= matrix[1, 2] < size[1]):
        raise ValueError('invalid canonical pinhole camera_matrix or principal point outside native image')
    board = export.get('board')
    if not isinstance(board, dict) or board.get('type') != 'checkerboard':
        raise ValueError('unsupported calibration board specification')
    for name in ('inner_columns', 'inner_rows'):
        if type(board.get(name)) is not int or not 3 <= board[name] <= 100:
            raise ValueError(f'invalid calibration board {name}')
    positive(board.get('square_size'), 'calibration square_size')
    if not isinstance(board.get('square_unit'), str) or not board['square_unit'].strip():
        raise ValueError('calibration square_unit is required')
    hashes = export.get('source_hashes')
    views = export.get('views')
    if not isinstance(hashes, list) or not hashes or not isinstance(views, list) or len(views) < 10:
        raise ValueError('calibration source hashes and accepted views are required')
    quality = export.get('quality')
    if not isinstance(quality, dict) or quality.get('passed') is not True:
        raise ValueError('calibration quality has not passed')
    per_view = quality.get('per_view')
    if not isinstance(per_view, list) or len(per_view) != len(views):
        raise ValueError('calibration quality and accepted-view counts disagree')
    errors = {'fit': [], 'evaluation': []}
    for index, (view, measurement) in enumerate(zip(views, per_view)):
        partition = 'evaluation' if (index+1) % 5 == 0 else 'fit'
        for item in (view, measurement):
            if (not isinstance(item, dict) or type(item.get('accepted_index')) is not int
                    or item['accepted_index'] != index or item.get('partition') != partition):
                raise ValueError('invalid calibration fitting/evaluation partition')
        error = measurement.get('reprojection_rms_px')
        if type(error) not in (int, float) or not math.isfinite(error) or error < 0:
            raise ValueError('invalid calibration per-view reprojection error')
        errors[partition].append(error)
    for partition, count_key, rms_key in (('fit', 'fitting_view_count', 'fitting_rms_px'),
                                            ('evaluation', 'evaluation_view_count', 'evaluation_rms_px')):
        if type(quality.get(count_key)) is not int or quality[count_key] != len(errors[partition]):
            raise ValueError('calibration quality view counts disagree')
        rms = quality.get(rms_key)
        if type(rms) not in (int, float) or not math.isfinite(rms) or rms < 0:
            raise ValueError('invalid calibration aggregate reprojection error')
    limit = quality.get('maximum_allowed_evaluation_error_px')
    positive(limit, 'maximum_allowed_evaluation_error_px')
    largest = quality.get('evaluation_max_view_rms_px')
    if (type(largest) not in (int, float) or not math.isfinite(largest)
            or not math.isclose(largest, max(errors['evaluation']), rel_tol=1e-9, abs_tol=1e-12)
            or largest > limit):
        raise ValueError('calibration held-out error gate failed or is inconsistent')
    return export


def load_calibration(path, image_size):
    """Load a validated estimate for exactly (native width, native height).

    No rescaling, crop adjustment or undistortion occurs. The returned artifact
    remains an estimate requiring physical validation for its actual camera mode.
    """
    with Path(path).open() as stream:
        export = json.load(stream)
    return _validate_export(export, image_size)


@dataclass(frozen=True)
class CalibrationConfig:
    inner_columns: int
    inner_rows: int
    square_size: float
    square_unit: str
    max_evaluation_error_px: float
    sample_step: int = 1
    max_candidates: int = 300
    max_views: int = 60
    min_views: int = 15
    min_view_distance_px: float = 1.0

    def __post_init__(self):
        for name in ('inner_columns', 'inner_rows'):
            if type(getattr(self, name)) is not int or not 3 <= getattr(self, name) <= 100:
                raise ValueError(f'{name} must be an integer in [3,100]')
        for name in ('square_size', 'max_evaluation_error_px', 'min_view_distance_px'):
            positive(getattr(self, name), name)
        if not isinstance(self.square_unit, str) or not self.square_unit.strip():
            raise ValueError('square_unit must declare the measured physical unit')
        for name in ('sample_step', 'max_candidates', 'max_views', 'min_views'):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f'{name} must be a positive integer')
        if self.min_views < 10:
            raise ValueError('min_views must be at least 10, including two held-out views')
        if not self.min_views <= self.max_views <= self.max_candidates <= 10000:
            raise ValueError('require min_views <= max_views <= max_candidates <= 10000')


def board_points(config):
    points = np.zeros((config.inner_columns * config.inner_rows, 3), np.float32)
    points[:, :2] = np.mgrid[:config.inner_columns, :config.inner_rows].T.reshape(-1, 2)
    points *= config.square_size
    return points


def detect_checkerboard(image, config):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    found, corners = cv2.findChessboardCornersSB(
        gray, (config.inner_columns, config.inner_rows), cv2.CALIB_CB_NORMALIZE_IMAGE)
    if not found:
        return None
    corners = np.asarray(corners, np.float32).reshape(-1, 1, 2)
    if not np.isfinite(corners).all():
        raise ValueError('checkerboard detector returned nonfinite points')
    return corners


def _constraint_ratio(points, corners, image_size):
    """Check the rank-five constraint needed by planar pinhole calibration.

    Normalize both coordinate systems before homographies/SVD. Frontoparallel
    translated/scaled boards do not independently constrain all intrinsics.
    """
    obj = points[:, :2].astype(np.float64)
    obj -= obj.mean(axis=0)
    obj /= np.max(np.ptp(obj, axis=0))
    rows = []
    def v(a, b):
        return [a[0]*b[0], a[0]*b[1]+a[1]*b[0], a[1]*b[1],
                a[2]*b[0]+a[0]*b[2], a[2]*b[1]+a[1]*b[2], a[2]*b[2]]
    for image_points in corners:
        img = image_points.reshape(-1, 2).astype(np.float64)
        img = (img - np.asarray(image_size)/2) / max(image_size)
        homography, _ = cv2.findHomography(obj, img, method=0)
        if homography is None or not np.isfinite(homography).all():
            raise ValueError('degenerate checkerboard homography')
        a, b = homography[:, 0], homography[:, 1]
        rows.extend((v(a, b), np.asarray(v(a, a))-np.asarray(v(b, b))))
    singular = np.linalg.svd(np.asarray(rows), compute_uv=False)
    ratio = float(singular[4]/singular[0]) if singular[0] else 0.0
    return ratio


def _reprojection_error(points, corners, rvec, tvec, matrix, distortion):
    projected, _ = cv2.projectPoints(points, rvec, tvec, matrix, distortion)
    error = projected.reshape(-1, 2) - corners.reshape(-1, 2)
    return float(np.sqrt(np.mean(np.sum(error.astype(np.float64)**2, axis=1))))


def _projection_distance(corners, previous, config):
    """Board numbering may reverse even when its image projection is unchanged."""
    alternatives = [previous, previous[::-1]]
    if config.inner_columns == config.inner_rows:
        grid = previous.reshape(config.inner_rows, config.inner_columns, 1, 2)
        alternatives.extend(np.rot90(grid, turns).reshape(-1, 1, 2) for turns in (1, 3))
    return min(float(np.sqrt(np.mean(np.sum((corners-other)**2, axis=2)))) for other in alternatives)


def fit_calibration(corners, image_size, config):
    """Fit all training views; every fifth accepted view is evaluation only."""
    if len(corners) < config.min_views:
        raise ValueError(f'insufficient distinct detected views: {len(corners)} < {config.min_views}')
    expected = (config.inner_columns * config.inner_rows, 1, 2)
    if any(c.shape != expected or not np.isfinite(c).all() for c in corners):
        raise ValueError('invalid or nonfinite corner observations')
    training = [index for index in range(len(corners)) if (index+1) % 5]
    evaluation = [index for index in range(len(corners)) if not (index+1) % 5]
    points = board_points(config)
    ratio = _constraint_ratio(points, [corners[i] for i in training], image_size)
    # A numerical conditioning check, not a claim about physical image coverage.
    minimum_ratio = 1e-5
    if not math.isfinite(ratio) or ratio < minimum_ratio:
        raise ValueError(f'degenerate calibration views: constraint ratio {ratio:.3g} < {minimum_ratio}')
    rms, matrix, distortion, rvecs, tvecs = cv2.calibrateCamera(
        [points] * len(training), [corners[i] for i in training], image_size, None, None,
        flags=cv2.CALIB_FIX_K4 | cv2.CALIB_FIX_K5 | cv2.CALIB_FIX_K6)
    distortion = np.asarray(distortion).reshape(-1)
    arrays = (matrix, distortion, np.asarray(rvecs), np.asarray(tvecs))
    if (not math.isfinite(rms) or any(not np.isfinite(a).all() for a in arrays)
            or distortion.size != 5 or matrix[0, 0] <= 0 or matrix[1, 1] <= 0):
        raise ValueError('nonfinite or invalid pinhole calibration solution')
    per_view = []
    fitting_errors = []
    evaluation_errors = []
    training_pose = dict(zip(training, zip(rvecs, tvecs)))
    for index, observed in enumerate(corners):
        held_out = index in evaluation
        if held_out:
            ok, rotation, translation = cv2.solvePnP(points, observed, matrix, distortion,
                                                    flags=cv2.SOLVEPNP_ITERATIVE)
            if not ok or not np.isfinite(rotation).all() or not np.isfinite(translation).all():
                raise ValueError('held-out pose estimation failed')
        else:
            rotation, translation = training_pose[index]
        error = _reprojection_error(points, observed, rotation, translation, matrix, distortion)
        if not math.isfinite(error):
            raise ValueError('nonfinite reprojection error')
        (evaluation_errors if held_out else fitting_errors).append(error)
        per_view.append({'accepted_index': index, 'partition': 'evaluation' if held_out else 'fit',
                         'reprojection_rms_px': error})
    quality = {'opencv_fitting_rms_px': float(rms), 'per_view': per_view,
               'fitting_view_count': len(training), 'evaluation_view_count': len(evaluation),
               'fitting_rms_px': float(np.sqrt(np.mean(np.square(fitting_errors)))),
               'evaluation_rms_px': float(np.sqrt(np.mean(np.square(evaluation_errors)))),
               'evaluation_max_view_rms_px': max(evaluation_errors),
               'maximum_allowed_evaluation_error_px': config.max_evaluation_error_px,
               'constraint_ratio': ratio, 'minimum_constraint_ratio': minimum_ratio,
               'evaluation_policy': 'Every fifth accepted view; intrinsics frozen before solvePnP pose evaluation'}
    quality['passed'] = max(evaluation_errors) <= config.max_evaluation_error_px
    return matrix, distortion, quality


def run(input_path, output_path, config, allow_incomplete=False):
    # Lazy import keeps the detector/fitter independently testable and shares the
    # same native-resolution decoding path as rm27-replay.
    from .pipeline import open_replay
    path, output = Path(input_path).resolve(), Path(output_path).resolve()
    source = open_replay(path, allow_incomplete=allow_incomplete)
    source_path = Path(source.path).resolve()
    try:
        if source_path == output or (source_path.is_dir() and source_path in output.parents):
            raise ValueError('output must be outside the input directory')
        output.mkdir(parents=True, exist_ok=False)
    except BaseException:
        source.close()
        raise
    report = {'schema_version': 1, 'passed': False, 'physical_validation': 'BLOCKED',
              'label': ESTIMATE_LABEL,
              'config': asdict(config), 'input': str(path), 'opencv_version': cv2.__version__,
              'accepted': [], 'rejected': [], 'scanned_frames': 0, 'candidate_count': 0,
              'source_hashes': [],
              'source_scope': 'Bounded decoded prefix; exact candidate-image and available original-payload hashes retained',
              'source_recording': getattr(source, 'reported', None)}
    iterator = None
    failure = None
    try:
        if path.is_file():
            report['source_hashes'].append({'path': str(path), 'sha256': sha256_file(path)})
        elif (path / 'manifest.json').is_file():
            report['source_hashes'].append({'path': str(path/'manifest.json'),
                                            'sha256': sha256_file(path/'manifest.json')})
        observations, digests = [], set()
        image_size = None
        iterator = iter(source)
        for scanned in range(config.max_candidates * config.sample_step):
            try:
                frame = next(iterator)
            except StopIteration:
                report['stop_reason'] = 'source_end'
                break
            report['scanned_frames'] += 1
            size = (frame.image.shape[1], frame.image.shape[0])
            if image_size is not None and size != image_size:
                raise ValueError('mixed image sizes are not permitted for calibration')
            image_size = size
            if scanned % config.sample_step:
                continue
            report['candidate_count'] += 1
            digest = hashlib.sha256(frame.image).hexdigest()
            view = {'sequence': frame.sequence, 'stream_id': frame.stream_id,
                    'image_sha256': digest}
            metadata = getattr(frame, 'acquisition_metadata', None) or {}
            original_hash = metadata.get('payload_sha256', metadata.get('sha256'))
            if original_hash:
                view['payload_sha256'] = original_hash
                report['source_hashes'].append({'sequence': frame.sequence, 'stream_id': frame.stream_id,
                                                'payload_sha256': original_hash})
            files = getattr(source, 'files', None)
            if files is not None and path.is_dir() and scanned < len(files) and files[scanned].suffix.lower() in {'.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff'}:
                view['source_path'] = str(files[scanned])
                view['source_sha256'] = sha256_file(files[scanned])
                report['source_hashes'].append({'path': view['source_path'], 'sha256': view['source_sha256']})
            found = detect_checkerboard(frame.image, config)
            reason = None
            if found is None:
                reason = 'checkerboard_not_detected'
            elif digest in digests or any(_projection_distance(found, old, config) < config.min_view_distance_px for old in observations):
                reason = 'duplicate_or_indistinct_projection'
            if reason:
                report['rejected'].append({**view, 'reason': reason})
                continue
            digests.add(digest)
            observations.append(found)
            hull_area = cv2.contourArea(cv2.convexHull(found))
            view.update(accepted_index=len(observations)-1,
                        partition='evaluation' if len(observations) % 5 == 0 else 'fit',
                        coverage_fraction=hull_area / (size[0]*size[1]))
            report['accepted'].append(view)
            if len(observations) >= config.max_views:
                report['stop_reason'] = 'max_views'
                break
        else:
            report['stop_reason'] = 'max_candidates'
        report.update(image_size=list(image_size) if image_size else None,
                      accepted_count=len(observations), rejected_count=len(report['rejected']))
        if observations:
            all_corners = np.concatenate(observations).reshape(-1, 2)
            hull_area = cv2.contourArea(cv2.convexHull(all_corners))
            report['image_coverage'] = {'accepted_corner_hull_fraction': hull_area / (image_size[0]*image_size[1]),
                                        'definition': 'Convex hull of detected inner corners; not a board area or calibration accuracy guarantee'}
        matrix, distortion, quality = fit_calibration(observations, image_size, config)
        report['quality'] = quality
        if not quality['passed']:
            raise ValueError('held-out reprojection error exceeds the user-specified maximum; export rejected')
        export = {'schema_version': 1, 'label': report['label'], 'physical_validation': 'BLOCKED',
                  'distortion_model': 'opencv_pinhole_5', 'distortion_order': DISTORTION_ORDER,
                  'camera_matrix': matrix.tolist(), 'distortion_coefficients': distortion.tolist(),
                  'image_size': list(image_size), 'board': {
                      'type': 'checkerboard', 'inner_columns': config.inner_columns,
                      'inner_rows': config.inner_rows, 'square_size': config.square_size,
                      'square_unit': config.square_unit, 'measurement_provenance': 'User declaration; not independently verified'},
                  'source_hashes': report['source_hashes'], 'views': report['accepted'],
                  'source_scope': report['source_scope'], 'source_recording': report['source_recording'],
                  'image_coverage': report['image_coverage'],
                  'quality': quality, 'opencv_version': cv2.__version__}
        _validate_export(export, image_size)
        with (output / 'calibration.json').open('x') as stream:
            json.dump(export, stream, indent=2, allow_nan=False)
            stream.write('\n')
        report['passed'] = True
    except BaseException as exc:
        failure = exc
        report['error'] = str(exc)
    finally:
        try:
            if iterator is not None and hasattr(iterator, 'close'):
                iterator.close()
            if source is not None:
                source.close()
        except BaseException as exc:
            if failure is None:
                failure = exc
                report.update(passed=False, error=str(exc))
        report['accepted_count'] = len(report['accepted'])
        report['rejected_count'] = len(report['rejected'])
        if failure is not None and (output / 'calibration.json').exists():
            # The directory is exclusively owned by this run. A close failure
            # must not leave an apparently valid export after a failed run.
            (output / 'calibration.json').unlink()
        with (output / 'quality.json').open('x') as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write('\n')
    if failure is not None:
        raise failure
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='native-resolution images, video, or recording manifest')
    parser.add_argument('--out', type=Path, required=True, help='new output directory')
    parser.add_argument('--inner-columns', type=int, required=True)
    parser.add_argument('--inner-rows', type=int, required=True)
    parser.add_argument('--square-size', type=float, required=True, help='measured physical square side')
    parser.add_argument('--square-unit', required=True, help='physical unit of the measured square side')
    parser.add_argument('--max-evaluation-error-px', type=float, required=True,
                        help='reject export if any held-out view exceeds this RMS pixel error')
    parser.add_argument('--sample-step', type=int, default=1)
    parser.add_argument('--max-candidates', type=int, default=300)
    parser.add_argument('--max-views', type=int, default=60)
    parser.add_argument('--min-views', type=int, default=15)
    parser.add_argument('--min-view-distance-px', type=float, default=1.0)
    parser.add_argument('--allow-incomplete', action='store_true')
    args = parser.parse_args(argv)
    try:
        config = CalibrationConfig(**{key: getattr(args, key) for key in CalibrationConfig.__dataclass_fields__})
        report = run(args.input, args.out, config, args.allow_incomplete)
    except (Exception, KeyboardInterrupt) as exc:
        parser.exit(1, f'calibration failed: {exc}\n')
    print(json.dumps(report, allow_nan=False))


if __name__ == '__main__':
    main()
