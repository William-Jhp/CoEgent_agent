"""Compare claims with independent evidence; deterministic contradictions win."""
import asyncio
import base64
from copy import deepcopy
import math
from time import time

from .contracts import (InspectionConfig, InspectionDecision, InspectionSnapshot,
                        ReportMessage, finite_json, number, json_equal)


class Inspector:
    def __init__(self, config, provider, reviewer=None):
        self.config = config if isinstance(config, InspectionConfig) else InspectionConfig.from_dict(config)
        self.provider, self.reviewer = provider, reviewer
        self._tasks = set()

    @property
    def pending(self):
        return tuple(task for task in self._tasks if not task.done())

    async def _bounded(self, awaitable):
        task = asyncio.create_task(awaitable)
        self._tasks.add(task)
        def finished(done):
            self._tasks.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        try:
            done, _ = await asyncio.wait({task}, timeout=self.config.model_timeout_s)
            if not done:
                raise TimeoutError('inspection_timeout')
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                # Never let cancellation-suppressing plugins make the timeout
                # unbounded. Keep their ownership visible until actually drained.
                await asyncio.wait({task}, timeout=min(.05, self.config.model_timeout_s))

    async def inspect(self, report):
        report = report if isinstance(report, ReportMessage) else ReportMessage.from_dict(report)
        def result(decision, reason, checks=None, analysis=''):
            return InspectionDecision(decision, reason, checks or [], analysis,
                {'kind':'inspection', 'reason':reason, 'report':report.to_dict(),
                 'checks':checks or [], 'analysis':analysis})
        if report.severity in {'fatal','safety'}:
            return result('halt','executor_safety_report')
        if report.kind == 'progress' or (report.kind == 'completed' and report.scope == 'step'):
            return result('continue','step_or_progress_report')
        if self.pending:
            return result('request_evidence','previous_inspection_still_pending')
        try:
            snapshot = await self._bounded(self.provider.snapshot(report))
            if not isinstance(snapshot, InspectionSnapshot) or (snapshot.mission_id, snapshot.revision) != (report.mission_id, report.revision):
                raise ValueError('foreign_snapshot')
            values, stamps = deepcopy(snapshot.values), deepcopy(snapshot.observed_at)
            if not isinstance(values, dict) or not isinstance(stamps, dict):
                raise ValueError('invalid_snapshot')
        except Exception as exc:
            values, stamps = {}, {}
            snapshot_error = type(exc).__name__
        else:
            snapshot_error = ''
        def fresh(source, path=''):
            parts = (source + '.' + path).split('.') if path else [source]
            stamp = None
            for length in range(len(parts), 0, -1):
                prefix = '.'.join(parts[:length])
                if prefix in stamps:
                    stamp = stamps[prefix]; break
            return number(stamp) and 0 <= time() - stamp <= self.config.max_age_s
        def source_fresh(source):
            return fresh(source) and all(fresh(source, key[len(source)+1:])
                for key in stamps if isinstance(key,str) and key.startswith(source+'.'))
        checks = []
        for check in self.config.checks:
            actual = values.get(check['source'])
            observed = None
            status, why = 'unknown', 'missing_or_stale_evidence'
            if fresh(check['source'], check['path']):
                try:
                    for part in check['path'].split('.'):
                        actual = actual[part]
                    finite_json(actual)
                    observed = deepcopy(actual)
                    if check['op'] == 'eq':
                        matches = json_equal(actual, check['expected'])
                    else:
                        expected = check['expected']
                        if isinstance(expected, list):
                            if not isinstance(actual, list) or len(actual) != len(expected) or not all(number(x) for x in actual):
                                raise ValueError('invalid_vector')
                            distance = math.dist(actual, expected)
                        else:
                            if not number(actual):
                                raise ValueError('invalid_number')
                            distance = abs(actual - expected)
                        matches = distance <= check['tolerance']
                    status, why = ('achieved','matches') if matches else ('unmet','observation_disagrees')
                except (KeyError, TypeError, ValueError, OverflowError):
                    why = 'invalid_or_missing_value'
            checks.append({'id':check['id'], 'status':status, 'reason':why,
                'source':check['source'], 'path':check['path'], 'actual':observed,
                'expected':deepcopy(check['expected']),
                **({'tolerance':check['tolerance']} if 'tolerance' in check else {})})
        model_checks, analysis = [], ''
        # Only fresh sources reach a model; raw reports cannot introduce evidence.
        evidence = {}
        for source in ('robot_state','vision'):
            if source_fresh(source):
                try:
                    finite_json(values.get(source), self.config.max_image_bytes * 2 + 65536)
                    evidence[source] = deepcopy(values[source])
                except (ValueError, KeyError):
                    pass
        vision = evidence.get('vision')
        if isinstance(vision, dict) and 'images' in vision:
            try:
                images = vision['images']
                if not isinstance(images, list) or not 1 <= len(images) <= 4:
                    raise ValueError('invalid_images')
                total = 0
                for url in images:
                    if not isinstance(url, str) or not url.startswith(('data:image/jpeg;base64,','data:image/png;base64,')):
                        raise ValueError('invalid_image')
                    total += len(base64.b64decode(url.split(',', 1)[1], validate=True))
                if total > self.config.max_image_bytes:
                    raise ValueError('images_too_large')
            except (ValueError, TypeError):
                evidence.pop('vision', None)
        eligible = self.reviewer is not None and (report.kind == 'error' or
                    (self.config.visual_assertions and 'vision' in evidence))
        if eligible:
            try:
                reviewed = await self._bounded(self.reviewer.review(report, evidence,
                    self.config.visual_assertions))
                analysis = reviewed['analysis']
                model_checks = [{'id':a['id'],'status':a['status'],'reason':'model_vision_assessment'}
                    for a in reviewed['assertions']]
            except Exception as exc:
                analysis = 'model_review_unavailable:' + type(exc).__name__
        if not model_checks:
            model_checks = [{'id':a['id'],'status':'unknown','reason':'fresh_vision_or_reviewer_unavailable'}
                            for a in self.config.visual_assertions]
        for check, criterion in zip(checks, self.config.checks):
            if not fresh(criterion['source'], criterion['path']):
                check.update(status='unknown', reason='evidence_expired_during_review')
        if not source_fresh('vision'):
            for check in model_checks:
                check.update(status='unknown', reason='vision_missing_or_expired')
        checks.extend(model_checks)
        if report.kind == 'error':
            return result('replan','executor_error_report', checks, analysis)
        if any(c['status'] == 'unmet' for c in checks):
            return result('replan','completion_contradicted', checks, analysis)
        if not checks or any(c['status'] != 'achieved' for c in checks):
            return result('request_evidence','completion_unverified' + (':' + snapshot_error if snapshot_error else ''), checks, analysis)
        return result('completed','completion_verified', checks, analysis)
