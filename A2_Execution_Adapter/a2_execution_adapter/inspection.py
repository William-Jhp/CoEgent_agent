"""Trusted A2 feedback binding; API result messages never supply observation values."""
class A2EvidenceProvider:
    def __init__(self, runtime, *, include_vision=False):
        self.runtime, self.include_vision = runtime, include_vision
        self.last_snapshot = None

    async def snapshot(self, report):
        self.last_snapshot = await self.runtime.inspection_snapshot(report, include_vision=self.include_vision)
        return self.last_snapshot
