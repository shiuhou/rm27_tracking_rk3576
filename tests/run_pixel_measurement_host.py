"""Run native pixel-measurement contract tests without board/async services.

For a complete C++ thread-capable host, use tests/CMakeLists.txt. This runner
extracts the same numerical NV21 helper definitions to support MinGW builds
without std::thread. It neither tests nor replaces the asynchronous service.
"""
from pathlib import Path
import argparse,hashlib,json,os,shutil,subprocess

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compiler-bin',type=Path)
    args=parser.parse_args()
    compiler=args.compiler_bin/('g++.exe' if os.name=='nt' else 'g++') if args.compiler_bin else shutil.which('g++')
    if not compiler or not Path(compiler).is_file():raise RuntimeError('GNU C++17 compiler required')
    build=ROOT/'build/pixel-measurement-host';(build/'dart').mkdir(parents=True,exist_ok=True)
    header=(ROOT/'main/include/dart/nv21_pipeline.hpp').read_text(encoding='utf-8')
    view=header[header.index('struct Nv21View {'):header.index('// Lease owns')]
    declarations=header[header.index('CandidateRoi source_roi'):header.index('enum class PipelineInputSource')]
    (build/'dart/nv21_pipeline.hpp').write_text('#pragma once\n#include "dart/green_detector.hpp"\nnamespace dart {\n'+view+declarations+'}\n',encoding='utf-8')
    frames=(ROOT/'main/include/dart/async_frame.hpp').read_text(encoding='utf-8')
    metadata=frames[frames.index('namespace dart {'):frames.index('// One pending frame')]
    schedules=frames[frames.index('// Absolute timestamp deadlines'):]
    (build/'dart/async_frame.hpp').write_text('#pragma once\n#include <cstdint>\n#include <stdexcept>\n'+metadata+schedules,encoding='utf-8')
    original=(ROOT/'main/src/nv21_pipeline.cpp').read_text(encoding='utf-8')
    numeric=original[:original.index('HighFpsPipeline::HighFpsPipeline')]
    for name in ('async_log','target_json','visual_motion','roi_scheduler'):
        numeric=numeric.replace('#include "dart/'+name+'.hpp"\n','')
    derived=build/'nv21_numerical.cpp';derived.write_text(numeric+'\n} // namespace dart\n',encoding='utf-8')
    sources=[ROOT/'main/src'/name for name in ['config.cpp','green_detector.cpp','green_detector_core.cpp','target_fusion.cpp','target_json.cpp','visual_motion.cpp']]
    sources += [derived,ROOT/'tests/test_pixel_measurement.cpp']
    executable=build/('pixel_measurement_tests.exe' if os.name=='nt' else 'pixel_measurement_tests')
    command=[str(compiler),'-std=c++17','-O2','-fno-fast-math','-I'+str(build),'-I'+str(ROOT/'tests/stubs'),'-I'+str(ROOT/'main/include')]
    if os.name=='nt':command+=['-static-libgcc','-static-libstdc++']
    command += [str(p) for p in sources]+['-o',str(executable)]
    subprocess.run(command,check=True,cwd=ROOT)
    result=subprocess.run([str(executable)],capture_output=True,text=True,cwd=ROOT)
    record=dict(passed=result.returncode==0,returncode=result.returncode,stdout=result.stdout,stderr=result.stderr,
        scope='Native numerical interface tests, original RGB image stub; no board, camera, NPU or asynchronous-service execution',
        original_nv21_source_sha256=hashlib.sha256((ROOT/'main/src/nv21_pipeline.cpp').read_bytes()).hexdigest(),
        derived_numerical_sha256=hashlib.sha256(derived.read_bytes()).hexdigest(),command=command)
    if result.returncode==0:
        sample=json.loads(next(line[5:] for line in result.stdout.splitlines() if line.startswith('JSON:')))
        assert sample['pixel_measurement']['position_covariance_valid']
        assert sample['pixel_measurement']['position_covariance_px2']==[[4.,.2],[.2,9.]]
        assert sample['pixel_measurement']['covariance_source']=='external model "example"\nnot a device calibration claim'
        assert sample['safe_for_control'] is False and sample['angles_valid'] is False
        record['json_round_trip_passed']=True
    (build/'result.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in record.items() if k not in ('command','stdout')},indent=2))
    if result.returncode:print(result.stdout);raise SystemExit(result.returncode)

if __name__=='__main__':main()
