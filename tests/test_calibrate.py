"""Generated targets exercise software only; they are not physical calibration data."""
from dataclasses import replace
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np

from rm27_tracking.calibrate import (
    CalibrationConfig, board_points, detect_checkerboard, fit_calibration, load_calibration, run,
)


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.config = CalibrationConfig(7, 5, .025, 'm', .5, max_candidates=30, max_views=15)
        self.matrix = np.array([[700., 0., 320.], [0., 720., 240.], [0., 0., 1.]])

    def pose(self, index):
        rotation = np.array([.32*np.sin(index*.61), .38*np.cos(index*.47), .12*np.sin(index*.37)])
        translation = np.array([-.075+.025*np.sin(index*.73), -.05+.02*np.cos(index*.53),
                                .55+.09*(1+np.sin(index*.41))])
        return rotation, translation

    def observations(self, count=15):
        result = []
        for index in range(count):
            corners, _ = cv2.projectPoints(board_points(self.config), *self.pose(index),
                                           self.matrix, np.zeros(5))
            result.append(corners)
        return result

    def render_board(self, index=0, image_size=(640, 480)):
        # Planar projection of an ideal printed board with a white outer margin.
        columns, rows, square = self.config.inner_columns, self.config.inner_rows, self.config.square_size
        tile = 70
        raster = np.empty(((rows+1)*tile, (columns+1)*tile), np.uint8)
        for y in range(rows+1):
            for x in range(columns+1):
                raster[y*tile:(y+1)*tile, x*tile:(x+1)*tile] = 0 if (x+y) % 2 == 0 else 255
        edge = np.array([[-1, -1, 0], [columns, -1, 0], [columns, rows, 0], [-1, rows, 0]], np.float32)*square
        projected, _ = cv2.projectPoints(edge, *self.pose(index), self.matrix, np.zeros(5))
        source = np.array([[0, 0], [raster.shape[1], 0], [raster.shape[1], raster.shape[0]], [0, raster.shape[0]]], np.float32)
        homography = cv2.getPerspectiveTransform(source, projected.reshape(-1, 2))
        image = cv2.warpPerspective(raster, homography, image_size, flags=cv2.INTER_LINEAR,
                                    borderMode=cv2.BORDER_CONSTANT, borderValue=210)
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)

    def test_real_detector_and_native_resolution_export(self):
        image = self.render_board()
        found = detect_checkerboard(image, self.config)
        self.assertIsNotNone(found)
        self.assertEqual(found.shape, (35, 1, 2))
        self.assertIsNone(detect_checkerboard(np.zeros_like(image), self.config))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = root/'images'
            images.mkdir()
            for index in range(15):
                self.assertTrue(cv2.imwrite(str(images/f'{index:03}.png'), self.render_board(index)))
            report = run(images, root/'result', replace(self.config, max_evaluation_error_px=1.0))
            self.assertTrue(report['passed'])
            self.assertEqual(report['accepted_count'], 15)
            self.assertEqual(report['image_size'], [640, 480])
            self.assertEqual(report['quality']['evaluation_view_count'], 3)
            self.assertEqual(report['physical_validation'], 'BLOCKED')
            exported = json.loads((root/'result/calibration.json').read_text())
            self.assertEqual(len(exported['distortion_coefficients']), 5)
            self.assertEqual(exported['board']['square_size'], .025)
            self.assertTrue(all(len(view['source_sha256']) == 64 for view in exported['views']))
            self.assertEqual([view['accepted_index'] for view in report['quality']['per_view'] if view['partition'] == 'evaluation'], [4, 9, 14])
            loaded = load_calibration(root/'result/calibration.json', (640, 480))
            self.assertEqual(loaded, exported)
            for size in ((320, 240), (480, 640), (True, 480), (640.0, 480), (640,)):
                with self.subTest(size=size), self.assertRaisesRegex(ValueError, 'resolution|image_size'):
                    load_calibration(root/'result/calibration.json', size)
            malformed = [
                ('distortion_model', 'opencv_fisheye'),
                ('distortion_order', ['k1', 'k2', 'k3', 'p1', 'p2']),
                ('distortion_coefficients', [0., 0., 0., 0.]),
                ('distortion_coefficients', [0., 0., True, 0., 0.]),
                ('distortion_coefficients', [0., 0., float('nan'), 0., 0.]),
                ('camera_matrix', [[700., 1., 320.], [0., 720., 240.], [0., 0., 1.]]),
                ('camera_matrix', [[-700., 0., 320.], [0., 720., 240.], [0., 0., 1.]]),
                ('camera_matrix', [[700., 0., 900.], [0., 720., 240.], [0., 0., 1.]]),
                ('image_size', [640.0, 480]),
                ('schema_version', True),
                ('physical_validation', 'VERIFIED'),
            ]
            for key, value in malformed:
                with self.subTest(key=key, value=value):
                    changed = deepcopy(exported)
                    changed[key] = value
                    (root/'malformed.json').write_text(json.dumps(changed))
                    with self.assertRaises(ValueError):
                        load_calibration(root/'malformed.json', (640, 480))
            for key, value in (('passed', False), ('evaluation_max_view_rms_px', 10.),
                               ('maximum_allowed_evaluation_error_px', False),
                               ('fitting_view_count', 1), ('evaluation_rms_px', float('inf'))):
                with self.subTest(quality=key):
                    changed = deepcopy(exported)
                    changed['quality'][key] = value
                    (root/'malformed.json').write_text(json.dumps(changed))
                    with self.assertRaises(ValueError):
                        load_calibration(root/'malformed.json', (640, 480))

    def test_fit_and_evaluation_are_separate_and_no_bad_view_discard(self):
        corners = self.observations()
        matrix, coefficients, quality = fit_calibration(corners, (640, 480), self.config)
        np.testing.assert_allclose(matrix, self.matrix, atol=.03)
        self.assertEqual(coefficients.shape, (5,))
        self.assertTrue(quality['passed'])
        # Only evaluation observations change. The fitted intrinsics must be
        # unchanged, while the evaluation catches the deliberately damaged view.
        damaged = [value.copy() for value in corners]
        damaged[4][::2, :, 0] += 9
        other_matrix, _, other_quality = fit_calibration(damaged, (640, 480), self.config)
        np.testing.assert_allclose(matrix, other_matrix, rtol=0, atol=1e-8)
        self.assertFalse(other_quality['passed'])
        self.assertEqual(len(other_quality['per_view']), 15)
        self.assertGreater(other_quality['evaluation_max_view_rms_px'], self.config.max_evaluation_error_px)

    def test_degenerate_nonfinite_and_insufficient_solutions_rejected(self):
        with self.assertRaisesRegex(ValueError, 'insufficient'):
            fit_calibration(self.observations(14), (640, 480), self.config)
        degenerate = []
        for index in range(15):
            corners, _ = cv2.projectPoints(board_points(self.config), np.zeros(3),
                                           np.array([-.075+index*.002, -.05, .6+index*.005]),
                                           self.matrix, np.zeros(5))
            degenerate.append(corners)
        with self.assertRaisesRegex(ValueError, 'degenerate'):
            fit_calibration(degenerate, (640, 480), self.config)
        invalid = self.observations()
        invalid[0][0] = np.nan
        with self.assertRaisesRegex(ValueError, 'nonfinite'):
            fit_calibration(invalid, (640, 480), self.config)

    def test_failed_threshold_never_exports_and_sampling_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = root/'images'
            images.mkdir()
            for index in range(20):
                cv2.imwrite(str(images/f'{index:03}.png'), self.render_board(index))
            with self.assertRaisesRegex(ValueError, 'held-out reprojection'):
                run(images, root/'bad-quality', replace(self.config, max_evaluation_error_px=1e-7))
            self.assertFalse((root/'bad-quality/calibration.json').exists())
            quality = json.loads((root/'bad-quality/quality.json').read_text())
            self.assertFalse(quality['passed'])
            self.assertEqual(len(quality['quality']['per_view']), 15)
            # Only ten candidates are inspected; input is not decoded to EOF.
            config = replace(self.config, sample_step=2, min_views=10, max_views=10, max_candidates=10)
            result = run(images, root/'bounded', replace(config, max_evaluation_error_px=1.0))
            self.assertEqual(result['scanned_frames'], 19)
            self.assertEqual(result['stop_reason'], 'max_views')
            self.assertEqual([view['sequence'] for view in result['accepted']], list(range(0, 20, 2)))

    def test_recorded_sequence_retains_payload_hashes_in_calibration_provenance(self):
        from rm27_tracking.recording import RecordingWriter
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            writer = RecordingWriter(root/'recording', 15*640*480*3)
            hashes = []
            try:
                for index in range(15):
                    image = self.render_board(index)
                    row = writer.append(memoryview(image).cast('B'), {
                        'sequence': index, 'source_sequence': index, 'stream_id': 0,
                        'timestamp_ns': index*1000, 'clock_domain': 'v4l2_unknown',
                        'timestamp_semantic': 'driver_end_of_frame', 'received_monotonic_ns': 100000+index*1000,
                        'driver_flags': 0, 'format': {
                            'width': 640, 'height': 480, 'stride': 1920, 'fourcc': 'BGR3',
                            'sizeimage': image.nbytes},
                    })
                    hashes.append(row['sha256'])
            finally:
                writer.finish(complete=len(hashes) == 15, reason='software_fixture')
            report = run(root/'recording', root/'result', replace(self.config, max_evaluation_error_px=1.0))
            self.assertTrue(report['passed'])
            self.assertEqual([view['payload_sha256'] for view in report['accepted']], hashes)
            self.assertEqual(len([value for value in report['source_hashes'] if 'payload_sha256' in value]), 15)
            with self.assertRaisesRegex(ValueError, 'outside the input'):
                run(root/'recording/manifest.json', root/'recording/invalid-output', self.config)
            self.assertFalse((root/'recording/invalid-output').exists())

    def test_duplicates_mixed_sizes_and_configuration_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = root/'images'
            images.mkdir()
            image = self.render_board()
            for index in range(15):
                cv2.imwrite(str(images/f'{index:03}.png'), image)
            with self.assertRaisesRegex(ValueError, 'insufficient'):
                run(images, root/'duplicate', self.config)
            quality = json.loads((root/'duplicate/quality.json').read_text())
            self.assertEqual(quality['accepted_count'], 1)
            self.assertEqual(quality['rejected_count'], 14)
            self.assertFalse((root/'duplicate/calibration.json').exists())
            cv2.imwrite(str(images/'001.png'), self.render_board(1, (800, 600)))
            with self.assertRaisesRegex(ValueError, 'mixed image sizes'):
                run(images, root/'mixed', self.config)
            with self.assertRaises(FileExistsError):
                run(images, root/'mixed', self.config)
        for values in ({'inner_columns': 0}, {'inner_rows': True}, {'square_size': 0},
                       {'square_size': float('nan')}, {'square_unit': ''}, {'max_evaluation_error_px': 0},
                       {'sample_step': 0}, {'max_views': 14}, {'min_views': 1}, {'max_candidates': 10001}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                replace(self.config, **values)


if __name__ == '__main__':
    unittest.main()
