"""Build the optional NumPy support validators. No runtime Cython dependency."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-tools',type=Path,help='Optional directory containing Cython and setuptools')
    parser.add_argument('--within-scope',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if not args.within_scope:
        if not sys.platform.startswith('linux'):
            raise SystemExit('This optional builder currently requires Linux/systemd; Python fallback remains available')
        command=['systemd-run','--user','--scope','--quiet','-p','MemoryMax=2G',
                 '-p','MemoryHigh=1536M','-p','MemorySwapMax=0','-p','TasksMax=64',
                 'timeout','300',sys.executable,str(Path(__file__).resolve()),'--within-scope']
        if args.build_tools:command+=['--build-tools',str(args.build_tools.resolve())]
        raise SystemExit(subprocess.run(command).returncode)
    group=Path('/sys/fs/cgroup')/Path('/proc/self/cgroup').read_text().split('::',1)[1].strip().lstrip('/')
    expected={'memory.max':'2147483648','memory.high':'1610612736','memory.swap.max':'0','pids.max':'64'}
    actual={key:(group/key).read_text().strip() for key in expected}
    if actual!=expected:raise SystemExit('Required build cgroup limits unavailable')
    if args.build_tools:sys.path.insert(0,str(args.build_tools.resolve()))
    try:
        from Cython.Build import cythonize
        from setuptools import Extension,Distribution
    except ImportError as error:
        raise SystemExit('Optional build tools required: python tools/install_dependencies.py --profile support') from error
    import numpy
    root=Path(__file__).resolve().parents[1]
    source=root/'solbreaker/native/support_checks/_support_checks.pyx'
    flags=['-O2','-g0','-fno-fast-math','-ffp-contract=off']
    with tempfile.TemporaryDirectory(prefix='solbreaker-support-checks-') as directory:
        stage=Path(directory)
        extension=Extension('solbreaker._support_checks',[str(source)],
            include_dirs=[numpy.get_include()],extra_compile_args=flags)
        compiled=cythonize([extension],build_dir=str(stage/'generated'),nthreads=0,quiet=True,
            compiler_directives=dict(language_level=3,infer_types=False,annotation_typing=False,
                binding=True,boundscheck=True,wraparound=True,initializedcheck=True,
                nonecheck=True,overflowcheck=True,cdivision=False,profile=False,linetrace=False))
        distribution=Distribution(dict(name='solbreaker-support-checks',ext_modules=compiled))
        command=distribution.get_command_obj('build_ext')
        command.build_lib=str(stage/'lib');command.build_temp=str(stage/'objects')
        command.inplace=False;command.parallel=1;command.force=True
        command.ensure_finalized();command.run()
        artifacts=list((stage/'lib/solbreaker').glob('_support_checks*.so'))
        if len(artifacts)!=1:raise RuntimeError('Unexpected support-check artifact count')
        data=artifacts[0].read_bytes();destination=root/'solbreaker'/artifacts[0].name
        fd,temporary=tempfile.mkstemp(prefix='.support-checks-',dir=destination.parent)
        try:
            with os.fdopen(fd,'wb') as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
            os.chmod(temporary,0o755)
            if hashlib.sha256(Path(temporary).read_bytes()).digest()!=hashlib.sha256(data).digest():
                raise RuntimeError('Staged artifact mismatch')
            os.replace(temporary,destination)
        finally:
            if os.path.exists(temporary):os.unlink(temporary)
    print(json.dumps(dict(artifact=str(destination),sha256=hashlib.sha256(data).hexdigest(),
                          bytes=len(data),flags=flags,limits=actual)))

if __name__=='__main__':main()
