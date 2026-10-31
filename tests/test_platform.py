"""First-boot evidence collection never configures or streams from a device."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from time import monotonic
import unittest
from unittest.mock import patch


path = Path(__file__).resolve().parents[1]/'scripts/check_platform.py'
spec = importlib.util.spec_from_file_location('rm27_platform_probe', path)
platform_probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(platform_probe)


class PlatformTests(unittest.TestCase):
    def test_query_drains_large_output_and_preserves_nonzero_status(self):
        command = [sys.executable, '-c', 'import sys; sys.stdout.write("x" * 300000); sys.stdout.flush(); sys.stderr.write("failure detail"); sys.exit(7)']
        result = platform_probe.query(command)
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['returncode'], 7)
        self.assertFalse(result['timed_out'])
        self.assertTrue(result['output_truncated'])
        self.assertEqual(result['output'], 'x'*platform_probe.MAX_QUERY_BYTES)
        result = platform_probe.query([sys.executable, '-c', 'print("query passed")'])
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['output'], 'query passed\n')
        self.assertFalse(result['output_truncated'])

    def test_query_timeout_kills_and_reaps_process(self):
        command = [sys.executable, '-c', 'import os,time; print(os.getpid(), flush=True); time.sleep(30)']
        before = monotonic()
        with patch.object(platform_probe, 'QUERY_TIMEOUT_S', .2):
            result = platform_probe.query(command)
        self.assertLess(monotonic()-before, 2)
        self.assertEqual(result['status'], 'FAIL')
        self.assertIsNone(result['returncode'])
        self.assertTrue(result['timed_out'])
        pid = int(result['output'].strip())
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_default_probe_never_queries_device_and_explicit_queries_are_read_only(self):
        with patch.object(platform_probe, 'query') as query:
            result = platform_probe.probe()
            query.assert_not_called()
            self.assertFalse(result['camera_accessed'])
            query.return_value = {'status': 'PASS'}
            result = platform_probe.probe(['/dev/video0', '/dev/video2'], ['/dev/media0'])
            self.assertEqual(query.call_count, 3)
            self.assertFalse(result['camera_streaming_started'])
            for call in query.call_args_list:
                command = call.args[0]
                self.assertTrue(command[0] in ('v4l2-ctl', 'media-ctl'))
                self.assertFalse(any('set-' in part or 'stream-' in part for part in command))

    def test_query_failure_and_exclusive_evidence_directory(self):
        with patch.object(platform_probe.shutil, 'which', return_value=None):
            self.assertEqual(platform_probe.query(['v4l2-ctl'])['status'], 'BLOCKED')
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/'probe'
            with patch.object(platform_probe, 'query', return_value={'status': 'FAIL', 'reason': 'device missing'}):
                self.assertEqual(platform_probe.main(['--device', '/dev/video0', '--out', str(out)]), 1)
            result = json.loads((out/'platform.json').read_text())
            self.assertEqual(result['camera_queries'][0]['status'], 'FAIL')
            with self.assertRaises(FileExistsError):
                platform_probe.main(['--out', str(out)])
        for value in ('video0', '/dev/video', '/dev/video0/other', '/tmp/video0', '/dev/video-1'):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                platform_probe.device_path(value, 'video')
