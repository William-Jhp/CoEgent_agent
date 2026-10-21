from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'A2_Execution_Adapter'),str(ROOT/'CoEgent_Agent'),str(ROOT/'Executor_Adapters')]

def test_contract_verification_survives_docker_path_translation_but_not_source_changes():
    from a2_execution_adapter.binding import portable_contract
    from coegent.tool_profiles import A2ToolProfile
    p=A2ToolProfile();tools=deepcopy(p.tools)
    for t in tools:t['source']['path']=t['source']['path'].replace(str(p.root),'/rig',1)
    mounted=SimpleNamespace(root=Path('/rig'),tools=tools)
    assert portable_contract(p)==portable_contract(mounted)
    tools[0]['source']['file_sha256']='0'*64
    assert portable_contract(p)!=portable_contract(mounted)

def test_scene_binding_detects_changed_map_data(tmp_path):
    from a2_execution_adapter.binding import scene_binding
    data=tmp_path/'planner/data';data.mkdir(parents=True)
    (data/'scene_meta.json').write_text('{}');(data/'map_scene.npz').write_bytes(b'grid1')
    original=scene_binding(tmp_path)
    (data/'map_scene.npz').write_bytes(b'grid2')
    assert scene_binding(tmp_path)!=original
