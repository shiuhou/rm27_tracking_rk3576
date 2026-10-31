#!/usr/bin/env python3
"""Build/install the wheel and run ARM64 software under isolated QEMU/Docker.

Host prerequisites: Python with pip/setuptools/wheel, Docker, dpkg-deb, and a C
compiler with static host libc. Optional clang verifies target UAPI compilation. Downloads
are pinned and hash checked. No host packages, binfmt handlers, or devices are
installed. Emulation verifies ARM64 userspace, not RK3576 hardware or performance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request

IMAGE = 'python@sha256:c27c26153fdf6863da2a1d85b474d1be004c42cc8ea2fd647004a8d4007a34d5'
QEMU_URL = 'https://archive.ubuntu.com/ubuntu/pool/universe/q/qemu/qemu-user-static_8.2.2+ds-0ubuntu1.18_amd64.deb'
QEMU_SHA256 = '5bb397f66063efa349f6fd5cb3b68cd96f29edd0994e4ba5115cf0859a716bf0'
CROSS_HEADERS = {
    'libc6-dev-arm64-cross_2.39-0ubuntu8cross1_all.deb':
        '04c6956de42669a53e620ad04a32475ea0bd4325808c227bf5ba52498f0d3f90',
    'linux-libc-dev-arm64-cross_6.8.0-25.25cross1_all.deb':
        '6a5a00b8ba8de66862e05493def87a5cbbb23b949601e45a5e3cbde56505cb6b',
}
WHEELS = {
    'numpy-2.2.6-cp312-cp312-manylinux_2_17_aarch64.manylinux2014_aarch64.whl':
        'f2618db89be1b4e05f7a1a847a9c1c0abd63e63a1607d892dd54668dd92faf87',
    'opencv_python_headless-4.11.0.86-cp37-abi3-manylinux_2_17_aarch64.manylinux2014_aarch64.whl':
        '6efabcaa9df731f29e5ea9051776715b1bdd1845d7c9530065c7951d2a2899eb',
}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def checked(path, expected):
    if sha256(path) != expected:
        raise ValueError(f'download hash mismatch: {path}')


def execute(command, **kwargs):
    subprocess.run([str(part) for part in command], check=True, **kwargs)


def guest():
    """Run installed package without mounting source code or touching a camera."""
    import ctypes
    import unittest
    import cv2
    import numpy as np
    from rm27_tracking import __version__, camera
    from rm27_tracking.benchmark import make_fixture

    output = Path('/evidence/runtime')
    output.mkdir(exist_ok=False)
    if platform.machine() != 'aarch64':
        raise RuntimeError('verification requires actual aarch64 Python userspace')
    if Path('/source').exists():
        raise RuntimeError('source checkout must not be mounted in installed-wheel validation')
    elf_paths = [Path('/usr/local/bin/python3.12'), Path(cv2.__file__).parent/'cv2.abi3.so',
                 Path(np._core._multiarray_umath.__file__)]
    elf_machines = {}
    for path in elf_paths:
        with path.open('rb') as stream:
            header = stream.read(20)
        if header[:4] != b'\x7fELF' or int.from_bytes(header[18:20], 'little') != 183:
            raise RuntimeError(f'expected AArch64 ELF: {path}')
        elf_machines[str(path)] = 183
    base = ['/usr/bin/qemu-aarch64-static', '/usr/local/bin/python3']
    for entry in ('rm27-vision', 'rm27-replay', 'rm27-calibrate', 'rm27-benchmark'):
        execute(base + [f'/site/bin/{entry}', '--help'], stdout=subprocess.DEVNULL)
    fixture = output/'input.avi'
    make_fixture(fixture, width=32, height=24, fps=25, frames=7)
    summaries = []
    for name in ('first', 'second'):
        completed = subprocess.run(base + ['/site/bin/rm27-benchmark', '--input', str(fixture),
                                          '--complete', '--fps', '25', '--out', str(output/name)],
                                   text=True, capture_output=True, check=True)
        summary = json.loads(completed.stdout)
        if summary['frame_count'] != 7 or summary['capture']['queue_drops']:
            raise RuntimeError('installed ARM64 benchmark lost frames')
        summaries.append(summary)
    if summaries[0]['processed_frames_sha256'] != summaries[1]['processed_frames_sha256']:
        raise RuntimeError('installed ARM64 replay was not deterministic')
    missing = output/'missing-camera.json'
    missing.write_text(json.dumps({'camera': {'device': '/dev/video999999'},
                                   'processing': {'max_frames': 1}}))
    failed = subprocess.run(base + ['/site/bin/rm27-vision', '--config', str(missing),
                                   '--out', str(output/'missing-camera')], text=True, capture_output=True)
    if failed.returncode != 1 or json.loads((output/'missing-camera/summary.json').read_text())['passed']:
        raise RuntimeError('missing-camera acquisition did not fail cleanly')
    replay = subprocess.run(base + ['/site/bin/rm27-replay', str(fixture), '--out', str(output/'replay')],
                            text=True, capture_output=True, check=True)
    if json.loads(replay.stdout)['frame_count'] != 7:
        raise RuntimeError('installed ARM64 replay CLI did not decode all input')
    # Forward guest Python subprocesses through explicit QEMU without binfmt.
    # The tiny static host bridge changes only this validation environment; the
    # production code and original tests run unchanged with ARM64 Python children.
    original_executable = sys.executable
    sys.executable = '/usr/local/bin/arm64-python-bridge'
    suite = unittest.defaultTestLoader.discover('/tests')
    selected = unittest.TestSuite()
    excluded = []
    def collect(test):
        if isinstance(test, unittest.TestSuite):
            for item in test:
                collect(item)
        elif test.id().endswith('.test_query_timeout_kills_and_reaps_process'):
            excluded.append({'test': test.id(), 'reason': 'QEMU Python startup exceeds the test 0.2-second host budget; equivalent ARM64 timeout/cleanup checked separately with 2-second allowance'})
        else:
            selected.addTest(test)
    collect(suite)
    result = unittest.TextTestRunner(verbosity=1).run(selected)
    # Exercise the same production query timeout/kill/reap implementation with
    # enough time for emulated Python to start. Application defaults and original
    # test thresholds are untouched; this is reported separately from that test.
    import importlib.util
    from time import monotonic
    spec = importlib.util.spec_from_file_location('arm64_platform_probe', '/scripts/check_platform.py')
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    probe.QUERY_TIMEOUT_S = 2
    started = monotonic()
    timeout_result = probe.query([sys.executable, '-c',
        'import os,time; print(os.getpid(), flush=True); time.sleep(30)'])
    timeout_elapsed = monotonic()-started
    if not timeout_result['timed_out'] or timeout_result['returncode'] is not None or timeout_elapsed > 5:
        raise RuntimeError('ARM64 query timeout did not stop within the declared emulation allowance')
    pid = int(timeout_result['output'].strip())
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    else:
        raise RuntimeError('ARM64 query child was not reaped')
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise RuntimeError('ARM64 timed-out query child remains alive')
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise RuntimeError('ARM64 timed-out query process group remains alive')
    sys.executable = original_executable
    # Compile this guest ctypes contract against independent target UAPI headers
    # on the host after emulation. No camera ioctl is executed here.
    c_lines = ['#include <stddef.h>', '#include <linux/videodev2.h>']
    structures = [('Capability', 'v4l2_capability'), ('PixelFormat', 'v4l2_pix_format'),
                  ('Format', 'v4l2_format'), ('RequestBuffers', 'v4l2_requestbuffers'),
                  ('Buffer', 'v4l2_buffer'), ('StreamParameters', 'v4l2_streamparm'),
                  ('QueryControl', 'v4l2_queryctrl'), ('Control', 'v4l2_control'),
                  ('PlanePixelFormat', 'v4l2_plane_pix_format'), ('PixelFormatMplane', 'v4l2_pix_format_mplane'),
                  ('Plane', 'v4l2_plane')]
    abi = {}
    for python_name, c_name in structures:
        if not hasattr(camera, python_name):
            continue
        cls = getattr(camera, python_name)
        abi[python_name] = {'size': ctypes.sizeof(cls), 'alignment': ctypes.alignment(cls),
                            'offsets': {field[0]: getattr(cls, field[0]).offset for field in cls._fields_}}
        c_lines.append(f'_Static_assert(sizeof(struct {c_name}) == {ctypes.sizeof(cls)}, "{python_name} size");')
        c_lines.append(f'_Static_assert(_Alignof(struct {c_name}) == {ctypes.alignment(cls)}, "{python_name} alignment");')
        for name, offset in abi[python_name]['offsets'].items():
            c_lines.append(f'_Static_assert(offsetof(struct {c_name}, {name}) == {offset}, "{python_name}.{name}");')
    for name, code in [('VIDIOC_QUERYCAP', camera.ioctl_code(0, camera.Capability, 2)),
                       ('VIDIOC_DQBUF', camera.ioctl_code(17, camera.Buffer)),
                       ('VIDIOC_QUERYCTRL', camera.ioctl_code(36, camera.QueryControl)),
                       ('VIDIOC_G_CTRL', camera.ioctl_code(27, camera.Control)),
                       ('VIDIOC_STREAMON', camera.ioctl_code(18, ctypes.c_int, 1))]:
        c_lines.append(f'_Static_assert({name} == {code}UL, "{name}");')
    (output/'uapi_abi.c').write_text('\n'.join(c_lines)+'\n')
    loaded_libraries = sorted({line.split()[-1] for line in Path('/proc/self/maps').read_text().splitlines()
                               if '.so' in line and line.split()[-1].startswith('/')})
    report = {'status': 'PASS' if result.wasSuccessful() else 'FAIL',
              'scope': 'ARM64 userspace executed by QEMU; no physical RK3576 or camera validation',
              'machine': platform.machine(), 'python': platform.python_version(),
              'rm27_tracking': __version__, 'numpy': np.__version__, 'opencv': cv2.__version__,
              'elf_machine': elf_machines, 'loaded_shared_libraries': loaded_libraries,
              'ctypes_abi': abi,
              'subprocess_method': 'static host bridge forwarding Python subprocess argv to explicit QEMU and ARM64 Python; no host binfmt',
              'timeout_equivalent': {'status': 'PASS', 'declared_timeout_s': 2, 'elapsed_s': timeout_elapsed,
                                     'child_reaped': True, 'process_group_gone': True},
              'tests_run': result.testsRun, 'skipped': [{'test': test.id(), 'reason': reason} for test, reason in result.skipped],
              'excluded': excluded, 'failures': len(result.failures), 'errors': len(result.errors),
              'installed_cli': ['rm27-vision --help', 'rm27-calibrate --help', 'rm27-replay seven-frame video',
                                'rm27-benchmark two complete seven-frame passes', 'rm27-vision missing-device failure'],
              'deterministic_pixel_sha256': summaries[0]['processed_frames_sha256'],
              'physical_acquisition': 'BLOCKED', 'rk3576_runtime': 'BLOCKED'}
    (output/'verification.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({key: report[key] for key in ('status', 'machine', 'python', 'rm27_tracking', 'numpy', 'opencv', 'tests_run', 'skipped', 'excluded')}))
    if not result.wasSuccessful():
        raise RuntimeError('ARM64 installed-wheel regression failed')


def host(output):
    if platform.machine() not in ('x86_64', 'amd64') or platform.system() != 'Linux':
        raise RuntimeError('this pinned static QEMU host binary requires Linux x86_64; use native install on ARM64')
    for tool in ('docker', 'dpkg-deb', 'cc'):
        if shutil.which(tool) is None:
            raise RuntimeError(f'required host tool is unavailable: {tool}')
    repository = Path(__file__).resolve().parent.parent
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output/'runtime').exists() or (output/'site').exists():
        raise FileExistsError('choose a fresh output directory; previous installed/runtime evidence is preserved')
    downloads = output/'downloads'
    downloads.mkdir(exist_ok=True)
    qemu_deb = downloads/'qemu-user-static.deb'
    if not qemu_deb.exists():
        urllib.request.urlretrieve(QEMU_URL, qemu_deb)
    checked(qemu_deb, QEMU_SHA256)
    qemu = output/'qemu-aarch64-static'
    with tempfile.TemporaryDirectory(dir=output) as extraction:
        execute(['dpkg-deb', '-x', qemu_deb, extraction])
        shutil.copy2(Path(extraction)/'usr/bin/qemu-aarch64-static', qemu)
    if any(not (downloads/name).exists() for name in WHEELS):
        execute([sys.executable, '-m', 'pip', 'download', '--only-binary=:all:', '--platform', 'manylinux2014_aarch64',
                 '--python-version', '312', '--implementation', 'cp', '--abi', 'cp312', '--dest', downloads,
                 'numpy==2.2.6', 'opencv-python-headless==4.11.0.86'])
    for name, expected in WHEELS.items():
        checked(downloads/name, expected)
    package_dir = output/'package'
    package_dir.mkdir(exist_ok=False)
    execute([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation', '--wheel-dir', package_dir, repository])
    packages = list(package_dir.glob('rm27_tracking-*-py3-none-any.whl'))
    if len(packages) != 1:
        raise RuntimeError('expected exactly one portable rm27 wheel')
    bridge = output/'arm64-python-bridge'
    bridge_source = output/'arm64-python-bridge.c'
    bridge_source.write_text('#include <unistd.h>\n#include <stdlib.h>\n#include <stdio.h>\n'
        'int main(int argc, char **argv) {\n'
        ' char **args = calloc((size_t)argc + 2, sizeof(char *));\n'
        ' if (!args) return 125;\n'
        ' args[0] = "/usr/bin/qemu-aarch64-static"; args[1] = "/usr/local/bin/python3";\n'
        ' for (int i=1; i<argc; i++) args[i+1] = argv[i];\n'
        ' execv(args[0], args); perror("ARM64 Python bridge"); return 126;\n}\n')
    execute(['cc', '-static', '-O2', bridge_source, '-o', bridge])
    (output/'site').mkdir()
    command = ['docker', 'run', '--rm', '--platform', 'linux/arm64', '--network', 'none', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges', '--user', f'{os.getuid()}:{os.getgid()}',
               '--env', 'PYTHONPATH=/site', '--env', 'PIP_DISABLE_PIP_VERSION_CHECK=1', '--env', 'PIP_NO_CACHE_DIR=1',
               '--mount', f'type=bind,source={qemu},target=/usr/bin/qemu-aarch64-static,readonly',
               '--mount', f'type=bind,source={bridge},target=/usr/local/bin/arm64-python-bridge,readonly',
               '--mount', f'type=bind,source={downloads},target=/wheels,readonly',
               '--mount', f'type=bind,source={package_dir},target=/package,readonly',
               '--mount', f'type=bind,source={output},target=/evidence',
               '--mount', f'type=bind,source={output / "site"},target=/site',
               '--mount', f'type=bind,source={repository / "tests"},target=/tests,readonly',
               '--mount', f'type=bind,source={repository / "scripts/check_platform.py"},target=/scripts/check_platform.py,readonly',
               '--mount', f'type=bind,source={Path(__file__).resolve()},target=/verify_arm64.py,readonly',
               '--entrypoint', '/usr/bin/qemu-aarch64-static', IMAGE, '/usr/local/bin/python3']
    execute(command + ['-m', 'pip', 'install', '--no-index', '--no-deps', '--target', '/site',
                        '--find-links', '/wheels', 'numpy==2.2.6', 'opencv-python-headless==4.11.0.86',
                        f'/package/{packages[0].name}'])
    execution = {'image': IMAGE, 'host_machine': platform.machine(), 'qemu_deb_sha256': QEMU_SHA256,
                 'qemu': subprocess.check_output([str(qemu), '--version'], text=True).splitlines()[0],
                 'download_hashes': WHEELS, 'subprocess_bridge_sha256': sha256(bridge), 'package': packages[0].name, 'package_sha256': sha256(packages[0]),
                 'source_files_sha256': {str(path.relative_to(repository)): sha256(path) for path in
                                        sorted((repository/'src/rm27_tracking').glob('*.py'))},
                 'validation_files_sha256': {str(path.relative_to(repository)): sha256(path) for path in
                                            sorted((repository/'tests').glob('test_*.py')) +
                                            [repository/'scripts/check_platform.py', Path(__file__).resolve()]}}
    (output/'environment.json').write_text(json.dumps(execution, indent=2)+'\n')
    execute(command + ['/verify_arm64.py', '--guest'])
    cross_abi(output, downloads)


def cross_abi(output, downloads):
    """Compile guest ctypes layout assertions against actual AArch64 UAPI."""
    clang = shutil.which('clang')
    if clang is None:
        (output/'cross-abi.json').write_text(json.dumps({'status': 'NOT_RUN', 'reason': 'clang unavailable'})+'\n')
        return
    include_root = output/'target-headers'
    include_root.mkdir(exist_ok=False)
    for name, expected in CROSS_HEADERS.items():
        path = downloads/name
        if not path.exists():
            urllib.request.urlretrieve('https://archive.ubuntu.com/ubuntu/pool/main/c/cross-toolchain-base/'+name, path)
        checked(path, expected)
        execute(['dpkg-deb', '-x', path, include_root])
    artifact = output/'runtime/uapi_abi.o'
    execute([clang, '--target=aarch64-linux-gnu', '-std=c11', '-include', 'time.h', '-isystem',
             include_root/'usr/aarch64-linux-gnu/include', '-c', output/'runtime/uapi_abi.c', '-o', artifact])
    header = artifact.read_bytes()[:20]
    if header[:4] != b'\x7fELF' or int.from_bytes(header[18:20], 'little') != 183:
        raise RuntimeError('ABI compile did not produce an AArch64 object')
    (output/'cross-abi.json').write_text(json.dumps({'status': 'PASS', 'target': 'aarch64-linux-gnu',
        'scope': 'guest ctypes sizes/alignment/offsets and ioctl constants matched target Linux6.8 UAPI; compile only',
        'compiler': subprocess.check_output([clang, '--version'], text=True).splitlines()[0],
        'target_headers_sha256': CROSS_HEADERS, 'source_sha256': sha256(output/'runtime/uapi_abi.c'),
        'object_sha256': sha256(artifact), 'elf_machine': 183,
        'verification_script_sha256': sha256(Path(__file__))}, indent=2)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, help='new evidence/install directory under artifacts/')
    parser.add_argument('--guest', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.guest:
        guest()
    elif args.out is None:
        parser.error('--out is required')
    else:
        host(args.out)


if __name__ == '__main__':
    main()
