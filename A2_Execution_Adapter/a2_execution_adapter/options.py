"""Deployment settings, never arguments authored by the planning model."""
from dataclasses import dataclass, fields
import math
from pathlib import Path
import re

PACKAGE_ROOT=Path(__file__).resolve().parents[1]

@dataclass(frozen=True)
class Options:
    source_root: str = '/home/gritandgrind/dev/kts/a2-llm-isaac'
    image: str = 'coegent-a2-adapter:sim'
    runtime_dir: str = str(PACKAGE_ROOT/'runtime')
    scene_dir: str | None = None
    pose_source: str = 'cuvslam'
    ros_domain_id: int = 72
    allow_motion: bool = False
    auto_initialize: bool = False
    stream_events: bool = False
    call_timeout_s: float = 210.
    mission_timeout_s: float = 300.
    cancel_timeout_s: float = 15.
    startup_timeout_s: float = 90.
    arrival_tolerance_m: float = .75
    progress_timeout_s: float = 30.
    private_path_bridge: bool = False
    planning_radius_m: float | None = None
    path_segment_m: float | None = None
    shortcut_margin_m: float | None = None
    max_steps: int = 128
    vision_url: str | None = None
    vision_model: str | None = None
    inspector_config: dict | None = None

    def __post_init__(self):
        if self.inspector_config is not None:
            from coegent.inspector import InspectionConfig
            from copy import deepcopy
            InspectionConfig.from_dict(self.inspector_config)
            object.__setattr__(self, 'inspector_config', deepcopy(self.inspector_config))
        if self.planning_radius_m is not None and (type(self.planning_radius_m) not in (float,int) or
                not math.isfinite(self.planning_radius_m) or self.planning_radius_m<=0):
            raise ValueError('planning_radius_m must be positive and finite')
        if self.path_segment_m is not None and (type(self.path_segment_m) not in (float,int) or
                not math.isfinite(self.path_segment_m) or self.path_segment_m<=0 or self.pose_source!='isaac_ground_truth'):
            raise ValueError('path_segment_m requires positive finite length and explicit Isaac pose')
        if self.shortcut_margin_m is not None and (type(self.shortcut_margin_m) not in (float,int) or
                not math.isfinite(self.shortcut_margin_m) or self.shortcut_margin_m<0 or
                self.pose_source!='isaac_ground_truth' or self.path_segment_m is None):
            raise ValueError('shortcut_margin_m requires nonnegative finite margin and segmented Isaac navigation')
        if self.pose_source not in ('cuvslam','isaac_ground_truth'):
            raise ValueError('unknown pose_source')
        if self.pose_source=='isaac_ground_truth' and not self.scene_dir:
            raise ValueError('Isaac pose requires an explicitly selected registered scene')
        if self.private_path_bridge and self.pose_source!='isaac_ground_truth':
            raise ValueError('private bridge is supported only with explicit Isaac pose')
        if type(self.ros_domain_id) is not int or self.ros_domain_id!=72:
            raise ValueError('this adapter targets the Isaac ROS domain 72 only')
        for k in ('allow_motion','auto_initialize','stream_events','private_path_bridge'):
            if type(getattr(self,k)) is not bool:raise ValueError(k+' must be boolean')
        for k in ('call_timeout_s','mission_timeout_s','cancel_timeout_s','startup_timeout_s','arrival_tolerance_m','progress_timeout_s'):
            v=getattr(self,k)
            if type(v) not in (float,int) or not math.isfinite(v) or v<=0:raise ValueError(k+' must be positive and finite')
        if type(self.max_steps) is not int or not 1<=self.max_steps<=1024:raise ValueError('invalid max_steps')
        for k in ('source_root','runtime_dir'):
            v=getattr(self,k)
            if not isinstance(v,str) or not v or '\n' in v or ',' in v:raise ValueError('invalid '+k)
        if self.scene_dir is not None and (not isinstance(self.scene_dir,str) or not self.scene_dir or any(c in self.scene_dir for c in ('\n',','))):
            raise ValueError('invalid scene_dir')
        if not isinstance(self.image,str) or not re.fullmatch(r'[\w./:@-]+',self.image):raise ValueError('invalid image')
        for k in ('vision_url','vision_model'):
            v=getattr(self,k)
            if v is not None and (not isinstance(v,str) or not v.strip()):raise ValueError('invalid '+k)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value,dict) or set(value)-{f.name for f in fields(cls)}:raise ValueError('unknown adapter options')
        return cls(**value)
