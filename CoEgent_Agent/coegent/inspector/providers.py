"""Application-owned observation providers, deliberately separate from executor claims."""
from pathlib import Path
import json
from time import monotonic, time

from .contracts import InspectionConfig, InspectionSnapshot
from .core import Inspector
from .model import ModelReviewer


class FileEvidenceProvider:
    """Read an independently produced snapshot on each inspection; timestamps are never reset."""
    def __init__(self, path):
        self.path = Path(path)

    async def snapshot(self, report):
        if self.path.stat().st_size > 5 * 1024 * 1024:
            raise ValueError('evidence_file_too_large')
        value = json.loads(self.path.read_text())
        return InspectionSnapshot(**value)


class StateEvidenceProvider:
    """Bridge a trusted CoEgent state provider, preserving original observation ages."""
    def __init__(self, provider):
        self.provider = provider

    async def snapshot(self, report):
        snapshot = await self.provider.snapshot()
        values = {'robot_state':snapshot.values.get('robot', {}),
                  'vision':snapshot.values.get('observations', {}).get('inspection', {})}
        stamps = {}
        for path, stamp in snapshot.observed_at.items():
            target = ('robot_state.' + path[6:] if path.startswith('robot.') else
                      'vision.' + path[len('observations.inspection.'):] if path.startswith('observations.inspection.') else None)
            if target:
                stamps[target] = time() - (monotonic() - stamp)
        return InspectionSnapshot(report.mission_id, report.revision, values, stamps)


def build_inspector(config, provider):
    config = config if isinstance(config, InspectionConfig) else InspectionConfig.from_dict(config)
    reviewer = None
    if config.model:
        from ..planner import ModelServerPlanner
        reviewer = ModelReviewer(ModelServerPlanner(endpoint=config.model['endpoint'],
            model=config.model['name'], timeout_s=config.model_timeout_s))
    return Inspector(config, provider, reviewer)
