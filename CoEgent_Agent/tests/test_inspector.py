"""Inspector must verify claims against independent, fresh observations."""
import asyncio
import time

import pytest

from coegent.inspector import (Inspector, InspectionConfig, InspectionSnapshot,
                               ReportMessage, ReportInbox, ModelReviewer)


def report(**kw):
    return ReportMessage.from_dict(dict(mission_id='m1', revision=1, report_id='r1',
        kind='completed', scope='mission', message='Arrived', **kw))


class Sensor:
    def __init__(self, values=None, *, age=0, mission='m1', revision=1):
        self.values = values or {'robot_state': {'pose': [1., 2.], 'stopped': True}}
        self.age, self.mission, self.revision = age, mission, revision

    async def snapshot(self, message):
        return InspectionSnapshot(self.mission, self.revision, self.values,
            {key: time.time() - self.age for key in self.values})


def config(**kw):
    return InspectionConfig.from_dict(dict(checks=[dict(id='destination', source='robot_state',
        path='pose', op='near', expected=[1, 2], tolerance=0.2)], **kw))


def inspect(inspector, msg=None):
    return asyncio.run(inspector.inspect(msg or report()))


def test_false_completion_is_replan_and_near_destination_is_completed():
    no = inspect(Inspector(config(), Sensor({'robot_state': {'pose': [5, 6]}})))
    assert no.decision == 'replan'
    assert no.checks[0]['status'] == 'unmet'
    yes = inspect(Inspector(config(), Sensor()))
    assert yes.decision == 'completed'
    assert yes.verified is True


@pytest.mark.parametrize('age,mission,revision', [(60, 'm1', 1), (-30, 'm1', 1),
                                                (0, 'other', 1), (0, 'm1', 2)])
def test_stale_future_and_foreign_evidence_never_proves_completion(age, mission, revision):
    result = inspect(Inspector(config(max_age_s=5), Sensor(age=age, mission=mission, revision=revision)))
    assert result.decision == 'request_evidence'
    assert not result.verified


@pytest.mark.parametrize('pose', [[float('nan'), 2], [True, 2], [1], None])
def test_invalid_measurements_are_unknown(pose):
    result = inspect(Inspector(config(), Sensor({'robot_state': {'pose': pose}})))
    assert result.decision == 'request_evidence'


def test_no_approved_checks_cannot_verify_and_step_report_cannot_complete_mission():
    assert inspect(Inspector(InspectionConfig(), Sensor())).decision == 'request_evidence'
    msg = ReportMessage.from_dict({**report().to_dict(), 'scope': 'step'})
    assert inspect(Inspector(config(), Sensor()), msg).decision == 'continue'


def test_claimed_evidence_never_overwrites_provider_and_safety_cannot_be_downgraded():
    with pytest.raises(ValueError):
        ReportMessage.from_dict({**report().to_dict(), 'robot_state': {'pose': [1, 2]}})
    error = ReportMessage.from_dict({**report().to_dict(), 'kind': 'error', 'severity': 'fatal'})
    assert inspect(Inspector(config(), Sensor()), error).decision == 'halt'
    error = ReportMessage.from_dict({**error.to_dict(), 'severity': 'recoverable'})
    assert inspect(Inspector(config(), Sensor()), error).decision == 'replan'


def test_mailbox_rejects_duplicate_late_foreign_and_overflow_reports():
    inbox = ReportInbox('m1', 1, capacity=1)
    assert inbox.submit(report())['accepted'] is True
    assert inbox.submit(report())['reason'] == 'duplicate_report'
    assert not inbox.submit(ReportMessage.from_dict({**report().to_dict(), 'report_id':'r2'}))['accepted']
    assert inbox.drain() == [report()]
    assert inbox.submit(report())['reason'] == 'duplicate_report'
    assert not inbox.submit(ReportMessage.from_dict({**report().to_dict(), 'mission_id':'x'}))['accepted']
    inbox.close()
    assert inbox.submit(ReportMessage.from_dict({**report().to_dict(), 'report_id':'r3'}))['reason'] == 'mission_not_active'


class Model:
    def __init__(self, output, delay=0):
        self.output, self.delay, self.messages = output, delay, None

    async def complete(self, messages, **kwargs):
        self.messages = messages
        await asyncio.sleep(self.delay)
        return self.output


def test_vlm_receives_only_fresh_images_and_must_cover_approved_assertions():
    import json
    cfg = InspectionConfig.from_dict({'checks': [], 'visual_assertions': [
        {'id':'door', 'description':'The door is open'}]})
    sensor = Sensor({'vision': {'images': ['data:image/jpeg;base64,YQ==']}})
    model = Model(json.dumps({'analysis':'Door open', 'assertions':[
        {'id':'door','status':'achieved','evidence_sources':['vision']}]}))
    result = inspect(Inspector(cfg, sensor, ModelReviewer(model)))
    assert result.decision == 'completed'
    assert model.messages[1]['content'][1]['type'] == 'image_url'
    model.output = '{"analysis":"Fine", "assertions":[]}'
    assert inspect(Inspector(cfg, sensor, ModelReviewer(model))).decision == 'request_evidence'
    model.messages = None
    assert inspect(Inspector(cfg, Sensor(sensor.values, age=99), ModelReviewer(model))).decision == 'request_evidence'
    assert model.messages is None


@pytest.mark.parametrize('output,delay', [('nonsense', 0), ('{}', 0.1)])
def test_model_failure_is_unknown_not_success(output, delay):
    cfg = InspectionConfig.from_dict({'visual_assertions':[{'id':'v','description':'target seen'}],
                                      'model_timeout_s':0.01})
    result = inspect(Inspector(cfg, Sensor({'vision': {'target':'seen'}}), ModelReviewer(Model(output, delay))))
    assert result.decision == 'request_evidence'


def test_model_cannot_override_numeric_failure():
    cfg = InspectionConfig.from_dict({'checks':[{'id':'battery','source':'robot_state',
        'path':'battery','op':'eq','expected':100}], 'visual_assertions':[{'id':'v','description':'fine'}]})
    model = Model('{"analysis":"complete", "assertions":[{"id":"v","status":"achieved","evidence_sources":["vision"]}]}')
    result = inspect(Inspector(cfg, Sensor({'robot_state':{'battery':20},'vision':{'ok':True}}), ModelReviewer(model)))
    assert result.decision == 'replan'


@pytest.mark.parametrize('bad', [{'max_age_s':float('nan')}, {'checks':[{'id':'x','source':'claim','path':'x','op':'eq','expected':1}]},
    {'checks':[{'id':'x','source':'robot_state','path':'pose','op':'near','expected':[1,2],'tolerance':-1}]}])
def test_unsafe_config_is_rejected(bad):
    with pytest.raises(ValueError):
        InspectionConfig.from_dict(bad)


def test_slow_model_cannot_bless_observations_that_expired_during_review():
    cfg=InspectionConfig.from_dict({'max_age_s':.01,'model_timeout_s':.1,
        'visual_assertions':[{'id':'v','description':'target seen'}]})
    model=Model('{"analysis":"seen","assertions":[{"id":"v","status":"achieved","evidence_sources":["vision"]}]}',.03)
    result=inspect(Inspector(cfg,Sensor({'vision':{'target':'seen'}}),ModelReviewer(model)))
    assert result.decision=='request_evidence'


def test_source_timestamp_cannot_hide_stale_image_timestamp():
    class Mixed(Sensor):
        async def snapshot(self, message):
            return InspectionSnapshot('m1',1,{'vision':{'images':['data:image/jpeg;base64,YQ==']}},
                {'vision':time.time(),'vision.images':time.time()-99})
    model=Model('{"analysis":"seen","assertions":[{"id":"v","status":"achieved","evidence_sources":["vision"]}]}')
    cfg=InspectionConfig.from_dict({'visual_assertions':[{'id':'v','description':'target seen'}]})
    assert inspect(Inspector(cfg,Mixed(),ModelReviewer(model))).decision=='request_evidence'
    assert model.messages is None


def test_cancellation_suppressing_reviewer_remains_owned_and_blocks_new_inspection():
    async def exercise():
        release=asyncio.Event()
        class Slow:
            async def review(self,*args):
                try: await release.wait()
                except asyncio.CancelledError: await release.wait()
                return {'analysis':'late','assertions':[]}
        instance=Inspector(InspectionConfig(model_timeout_s=.005),Sensor(),Slow())
        msg=ReportMessage.from_dict({**report().to_dict(),'kind':'error'})
        try:
            result=await asyncio.wait_for(instance.inspect(msg),.2)
            assert result.decision=='replan'
            assert instance.pending
            again=await instance.inspect(report())
            assert again.decision=='request_evidence'
        finally:
            release.set();await asyncio.sleep(.01)
        assert not instance.pending
    asyncio.run(exercise())


@pytest.mark.parametrize('actual,expected',[([True,False],[1,0]),({'stopped':1},{'stopped':True})])
def test_nested_json_boolean_is_not_a_numeric_measurement(actual,expected):
    cfg=InspectionConfig.from_dict({'checks':[{'id':'v','source':'robot_state','path':'value','op':'eq','expected':expected}]})
    assert inspect(Inspector(cfg,Sensor({'robot_state':{'value':actual}}))).decision=='replan'


@pytest.mark.parametrize('revision',[True,1.0])
def test_evidence_revision_requires_integer_identity_not_python_numeric_equivalence(revision):
    result=inspect(Inspector(config(),Sensor(revision=revision)))
    assert result.decision=='request_evidence'
