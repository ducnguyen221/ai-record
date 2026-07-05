from ai_record.config import Settings
from ai_record.transcriber import is_hallucination

S = Settings()


def _guard(text, no_speech=0.1, logprob=-0.2, rms=0.1):
    return is_hallucination(text, no_speech_prob=no_speech, avg_logprob=logprob, rms=rms, settings=S)


def test_real_content_kept():
    assert _guard("let us start the meeting") is False


def test_empty_and_punct_dropped():
    assert _guard("") is True
    assert _guard("   ") is True
    assert _guard("...!!") is True


def test_denylist_dropped():
    assert _guard("thank you") is True
    assert _guard("Thanks for watching") is True
    assert _guard("ご視聴ありがとうございました") is True


def test_low_rms_dropped():
    assert _guard("hello", rms=0.001) is True


def test_no_speech_and_low_logprob_dropped():
    assert _guard("maybe words", no_speech=0.9, logprob=-2.0) is True


def test_high_no_speech_but_good_logprob_kept():
    assert _guard("clearly spoken", no_speech=0.9, logprob=-0.1) is False
