"""Verify source and scene contents across host/container mount path changes."""
from copy import deepcopy
import hashlib
from pathlib import Path
from coegent_contracts.registry import fingerprint

def portable_contract(profile):
    tools=deepcopy(profile.tools)
    for t in tools:t['source']['path']=str(Path(t['source']['path']).relative_to(profile.root))
    return fingerprint({'profile':'a2-isaac.v1','tools':tools})

def scene_binding(root,scene_dir=None):
    from coegent.a2_maps import scene_digest
    return scene_digest(root,scene_dir)
