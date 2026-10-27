from threading import Event

import pytest

from robocap_rerun_tools.upload_progress import terminal_progress, upload_phase


def test_progress_is_sent_to_terminal_and_web(capsys):
    web = []
    terminal_progress(web.append)("verifying files")
    assert web == ["verifying files"]
    assert capsys.readouterr().out.count("verifying files") == 1
    terminal_progress(print)("CLI progress")
    assert capsys.readouterr().out.count("CLI progress") == 1


@pytest.mark.parametrize("fails", [False, True])
def test_phase_heartbeat_and_completion(fails):
    beat = Event()
    messages = []

    def progress(message):
        messages.append(message)
        if "RUNNING" in message:
            beat.set()

    def run():
        with upload_phase(progress, "verification", interval=0.01):
            assert beat.wait(2)
            if fails:
                raise ValueError("failure")

    if fails:
        with pytest.raises(ValueError):
            run()
    else:
        run()
    assert "START verification" in messages[0]
    assert ("FAILED" if fails else "DONE") in messages[-1]
