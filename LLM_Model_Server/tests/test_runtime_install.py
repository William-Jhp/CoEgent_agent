import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from local_llm_server.runtime_install import install_llamacpp
from local_llm_server.lifecycle import process_identity


@unittest.skipUnless(shutil.which('cc') and shutil.which('ldd'),'ELF fixture needs cc and ldd')
class RuntimeInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.build=self.root/'upstream/build'
        self.bin=self.build/'bin';self.bin.mkdir(parents=True)
        (self.root/'upstream/LICENSE').write_text('Fixture license')
        library=self.root/'library.c';library.write_text('int fixture_value(void){return 42;}')
        program=self.root/'main.c';program.write_text('#include <stdio.h>\nextern int fixture_value(void);\nint main(void){if(fixture_value()!=42)return 1;puts("version: fixture");return 0;}')
        subprocess.run(['cc','-shared','-fPIC',str(library),'-Wl,-soname,libggml-fixture.so',
                        '-o',str(self.bin/'libggml-fixture.so.1')],check=True,capture_output=True)
        (self.bin/'libggml-fixture.so').symlink_to('libggml-fixture.so.1')
        subprocess.run(['cc',str(program),'-L'+str(self.bin),'-lggml-fixture',
                        '-Wl,-rpath,'+str(self.bin),'-o',str(self.bin/'llama-server')],
                       check=True,capture_output=True)
        self.destination=self.root/'installed';self.state=self.root/'run/server.json'

    def test_bundle_runs_after_original_build_is_removed(self):
        manifest=install_llamacpp(self.build,self.destination,state_path=self.state)
        self.assertIn('version: fixture',manifest['version'])
        shutil.rmtree(self.root/'upstream')
        environment={**os.environ,'LD_LIBRARY_PATH':str(self.destination/'bin')}
        result=subprocess.run([str(self.destination/'bin/llama-server'),'--version'],
                              env=environment,capture_output=True,text=True,check=True)
        self.assertIn('version: fixture',result.stdout)
        self.assertTrue((self.destination/'LICENSE').is_file())
        self.assertEqual((self.destination/'bin/libggml-fixture.so').resolve().parent,
                         self.destination/'bin')
        self.assertTrue((self.destination/'manifest.json').is_file())

    def test_active_managed_runtime_cannot_be_overwritten(self):
        self.state.parent.mkdir()
        record={**process_identity(os.getpid()),'runtime':'llamacpp'}
        self.state.write_text(json.dumps(record))
        with self.assertRaisesRegex(RuntimeError,'running'):
            install_llamacpp(self.build,self.destination,state_path=self.state)
        self.assertFalse(self.destination.exists())

    def test_destination_cannot_overlap_source_build(self):
        for destination in [self.build/'private-runtime',self.build,self.build.parent]:
            with self.subTest(destination=destination), self.assertRaisesRegex(ValueError,'source build'):
                install_llamacpp(self.build,destination,state_path=self.state)


if __name__=='__main__': unittest.main()
