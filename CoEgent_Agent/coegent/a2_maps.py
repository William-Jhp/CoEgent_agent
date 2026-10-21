"""Explicit scene paths shared by planning and the selected A2 executor."""
import hashlib
import json
from pathlib import Path
import re


def scene_path(root,scene_dir=None):
    return Path(scene_dir).expanduser().resolve() if scene_dir else Path(root)/'planner/data'


def native_directory(root,meta):
    raw=str(meta['map_dir'])
    if raw.startswith(('/rig/','/repo/')):raw=raw.split('/',2)[2]
    path=(Path(root)/raw).resolve()
    if not path.is_relative_to(Path(root).resolve()):raise ValueError('scene map must be within configured source checkout')
    return path


def scene_digest(root,scene_dir=None):
    directory=scene_path(root,scene_dir)
    names={name:directory/name for name in ('scene_meta.json','map_scene.npz')}
    result={}
    if scene_dir:
        meta=json.loads((directory/'scene_meta.json').read_text())
        selection=meta.get('map_selection',{})
        uuid=selection.get('map_uuid','')
        if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',uuid):raise ValueError('selected scene needs a native map_uuid')
        if selection.get('backend')!='cuvslam' or selection.get('profile') not in ('a2_sim_cuvslam.yaml','a2_sim_cuvslam360.yaml'):
            raise ValueError('selected scene needs a cuVSLAM camera profile')
        native=native_directory(root,meta)
        for name,expected in selection.get('native_files',{}).items():
            path=(native/name).resolve()
            if not path.is_relative_to(native):raise ValueError('native map path escapes map directory')
            with path.open('rb') as f:actual=hashlib.file_digest(f,'sha256').hexdigest()
            if actual!=expected:raise ValueError('native map hash mismatch: '+name)
            result['native/'+name]=actual
    for name,path in names.items():
        with path.open('rb') as f:result[name]=hashlib.file_digest(f,'sha256').hexdigest()
    return result
