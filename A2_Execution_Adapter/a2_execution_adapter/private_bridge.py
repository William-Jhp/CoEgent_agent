"""Build an owned copy of the installed bridge with UUID-only cancellation.

The colleague's source and installed binary remain unchanged. On cancel before
acceptance, the source's existing cancel_requested_ acceptance callback cancels
the owned handle; a ROS action-server-wide cancel-all is never sent.
"""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys


def patched_source(source):
    call='controller_client_->async_cancel_all_goals();'
    if source.count(call)!=1 or 'if (cancel_requested_)' not in source or 'async_cancel_goal(goal_handle)' not in source:
        raise ValueError('unsupported_private_bridge_source')
    return source.replace(call,'// Defer to cancel_requested_ at acceptance; never cancel foreign goals.')


def build(source_root,output_dir):
    source_root=Path(source_root);output=Path(output_dir);output.mkdir(parents=True,exist_ok=True)
    source=source_root/'teamgrit-navigation/nav/src/mode/nav2_native/src/nav2_path_bridge_node.cpp'
    cmake=source_root/'teamgrit-navigation/nav/build/grit_nav_nav2_native/CMakeFiles/nav2_path_bridge_node.dir'
    text=patched_source(source.read_text());patched=output/'nav2_path_bridge_node.cpp';patched.write_text(text)
    flags={}
    for line in (cmake/'flags.make').read_text().splitlines():
        if ' = ' in line:
            k,v=line.split(' = ',1);flags[k]=v
    args=[]
    for key in ('CXX_DEFINES','CXX_INCLUDES','CXX_FLAGS'):
        args.extend(shlex.split(flags[key].replace('/repo/',str(source_root)+'/')))
    obj=output/'nav2_path_bridge_node.o';binary=output/'nav2_path_bridge_node'
    subprocess.run(['/usr/bin/c++',*args,'-c',str(patched),'-o',str(obj)],check=True)
    link=shlex.split((cmake/'link.txt').read_text());link[1]=str(obj);link[link.index('-o')+1]=str(binary)
    subprocess.run(link,check=True)
    record={'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'patched_sha256':hashlib.sha256(patched.read_bytes()).hexdigest(),
            'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'policy':'owned UUID cancellation only; defer pre-accept cancel to goal_response_callback'}
    (output/'build.json').write_text(json.dumps(record,indent=2)+'\n');print(json.dumps(record))


def verify(source_root,output_dir):
    output=Path(output_dir);record=json.loads((output/'build.json').read_text())
    source=Path(source_root)/'teamgrit-navigation/nav/src/mode/nav2_native/src/nav2_path_bridge_node.cpp'
    expected={'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
              'patched_sha256':hashlib.sha256(patched_source(source.read_text()).encode()).hexdigest(),
              'binary_sha256':hashlib.sha256((output/'nav2_path_bridge_node').read_bytes()).hexdigest()}
    if any(record.get(k)!=v for k,v in expected.items()):raise ValueError('private_bridge_build_changed_or_stale')
    return record

if __name__=='__main__':build(*sys.argv[1:])
