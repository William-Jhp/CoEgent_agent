import math

import pytest

from a2_execution_adapter.office_keyboard import KeyboardState, encode_axes


@pytest.mark.parametrize('key,want',[
    ('w',(.3,0.,0.)),('s',(-.3,0.,0.)),('a',(0.,0.,.5)),('d',(0.,0.,-.5)),
    ('q',(0.,.2,0.)),('e',(0.,-.2,0.)),('W',(.3,0.,0.)),
])
def test_keyboard_direction_uses_physical_speed_units(key,want):
    s=KeyboardState();s.feed(key,10.)
    assert s.velocity(10.1)==want


def test_missing_key_repeats_stop_on_wall_deadline():
    s=KeyboardState();s.feed('w',1.)
    assert s.velocity(1.29)==(.3,0.,0.)
    assert s.velocity(1.31)==(0.,0.,0.)
    s.feed('w',1.5)
    assert s.velocity(1.51)==(.3,0.,0.)


def test_unrecognized_keys_do_not_extend_motion_and_space_stops_immediately():
    s=KeyboardState();s.feed('w',1.);s.feed('z',1.25)
    assert s.velocity(1.31)==(0.,0.,0.)
    s.feed('w',2.);s.feed(' ',2.01)
    assert s.velocity(2.01)==(0.,0.,0.)


def params():
    return dict(vx_max=1.,vy_max=.6,wz_max=1.2,axis_vx=1,axis_vy=0,axis_yaw=2,
                sign_vx=-1.,sign_vy=-1.,sign_yaw=-1.,deadzone=.08)


def test_ros_axis_encoding_compensates_live_driver_deadzone_and_signs():
    axes=encode_axes((.3,-.2,.5),params())
    # Independently decode as the existing joystick driver does.
    decoded=[]
    for index,sign,cap in [(1,-1,1.),(0,-1,.6),(2,-1,1.2)]:
        a=axes[index];scaled=math.copysign((abs(a)-.08)/.92,a) if abs(a)>.08 else 0.
        decoded.append(sign*scaled*cap)
    assert decoded==pytest.approx([.3,-.2,.5])
    assert encode_axes((0,0,0),params())==[0.,0.,0.,0.]


@pytest.mark.parametrize('changes',[{'vx_max':0},{'deadzone':1.},{'axis_vx':-1},{'axis_yaw':1},{'sign_vx':0},{'wz_max':float('nan')}])
def test_invalid_live_joystick_mapping_cannot_emit_motion(changes):
    with pytest.raises(ValueError):encode_axes((.3,0,0),{**params(),**changes})


def test_requested_speed_over_live_cap_is_rejected():
    with pytest.raises(ValueError,match='cap'):encode_axes((.4,0,0),{**params(),'vx_max':.3})


@pytest.mark.parametrize('kwargs',[{'speed':float('nan')},{'speed':-1},{'release_timeout':0},{'release_timeout':2.}])
def test_invalid_keyboard_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):KeyboardState(**kwargs)
