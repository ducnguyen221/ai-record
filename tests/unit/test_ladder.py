import dataclasses

from ai_record.config import LadderStep, Settings
from ai_record.pipeline import LadderController
from ai_record.transcriber import MockTranscriber


class _Rec(MockTranscriber):
    def __init__(self):
        super().__init__()
        self.steps = []

    def apply_ladder_step(self, step):
        self.steps.append(step)


def test_trigger_at_backlog_threshold():
    s = Settings()
    ctl = LadderController(s, _Rec(), lambda: None)
    assert ctl.should_downgrade(3, 0.0) is True   # > 2 utterances
    assert ctl.should_downgrade(0, 4.0) is True   # > 3 s lag
    assert ctl.should_downgrade(2, 1.0) is False


def test_steps_down_in_order():
    s = Settings()
    tr = _Rec()
    ctl = LadderController(s, tr, lambda: None)
    ctl.evaluate(5, 0.0)
    assert ctl.step == LadderStep.BEAM_1
    ctl.evaluate(5, 0.0)
    assert ctl.step == LadderStep.TRANSLATION_CPU
    assert tr.steps[-1] == LadderStep.TRANSLATION_CPU


def test_step_up_requires_stable_window():
    s = dataclasses.replace(Settings(), recovery_stable_seconds=0)
    tr = _Rec()
    ctl = LadderController(s, tr, lambda: None)
    ctl.evaluate(5, 0.0)             # down to BEAM_1
    assert ctl.step == LadderStep.BEAM_1
    ctl.evaluate(0, 0.0)             # first clear sample sets timer
    ctl.evaluate(0, 0.0)             # stable window (0 s) elapsed → step up
    assert ctl.step == LadderStep.NONE


def test_disabled_ladder_no_change():
    s = dataclasses.replace(Settings(), auto_downgrade_on_backpressure=False)
    ctl = LadderController(s, _Rec(), lambda: None)
    ctl.evaluate(50, 100.0)
    assert ctl.step == LadderStep.NONE
