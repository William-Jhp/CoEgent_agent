import pytest


def test_goal_types_unknown_and_confirmed_empty_gripper():
    from coegent.goals import Goal, verify_goals
    from coegent.scenarios import initial_state
    state = initial_state().snapshot()
    assert verify_goals((), state, ())["goal_status"] == "not_evaluated"
    assert verify_goals((Goal("holding_object", "cup"),), state, ())["goal_status"] == "unmet"
    assert verify_goals((Goal("at_location", "HOME"),), state, (), state_known=False)["goal_status"] == "unknown"
    assert verify_goals((Goal("valve_state", "red_valve", "open"),), state, ())["goal_status"] == "achieved"
    assert verify_goals((Goal("inspected_target", "red_valve"),), state, ())["goal_status"] == "unknown"


@pytest.mark.parametrize("args", [("free_expression", "x"), ("valve_state", "x", "other"),
                                  ("at_location", "", None)])
def test_unsupported_goals_rejected(args):
    from coegent.goals import Goal
    with pytest.raises(ValueError):
        Goal(*args)


@pytest.mark.parametrize("state_known", [True, False])
@pytest.mark.parametrize("history_complete,expected", [(True, "unmet"), (False, "unknown")])
def test_missing_inspection_depends_on_execution_history_not_sensor_state(state_known, history_complete, expected):
    from coegent.goals import Goal, mission_success, verify_goals
    from coegent.scenarios import initial_state
    result = verify_goals((Goal("inspected_target", "red_valve"),), initial_state().snapshot(), (),
                          state_known=state_known, inspection_history_complete=history_complete)
    assert result["goal_status"] == expected
    assert mission_success("SUCCESS", expected) is (False if history_complete else None)
