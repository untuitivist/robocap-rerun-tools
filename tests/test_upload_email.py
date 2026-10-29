import pytest

from robocap_rerun_tools import upload_email


def configure(tmp_path, monkeypatch):
    for key in ("UPLOAD_EMAIL_SENDER", "UPLOAD_EMAIL_AUTH_CODE", "UPLOAD_EMAIL_RECEIVER"):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    path.write_text("UPLOAD_EMAIL_SENDER=sender@qq.com\nUPLOAD_EMAIL_AUTH_CODE=test-secret\n"
                    "UPLOAD_EMAIL_RECEIVER=receiver@qq.com\n", encoding="utf-8")
    return path


def test_summary_mail_counts_hours_errors_and_attachment(tmp_path, monkeypatch):
    path = configure(tmp_path, monkeypatch)
    sent = []

    class SMTP:
        def __init__(self, host, port, timeout):
            assert (host, port, timeout) == ("smtp.qq.com", 465, 30)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def login(self, user, password):
            assert (user, password) == ("sender@qq.com", "test-secret")
        def send_message(self, message):
            sent.append(message)

    monkeypatch.setattr(upload_email.smtplib, "SMTP_SSL", SMTP)
    summary = upload_email.BatchSummary("F:/data", "owner/data", new=2, replaced=1,
        skipped=4, failed=1, hours=1.5, errors=["session1: transfer failed"])
    result = upload_email.send_batch_summary(summary, path)
    assert "receiver@qq.com" in result
    message = sent[0]
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert "1.5000 hour" in body
    assert "session1: transfer failed" in body
    assert "成功 Session 数：3" in body
    assert "test-secret" not in message.as_string()
    assert len(list(message.iter_attachments())) == 1


def test_email_failure_does_not_raise_or_expose_password(tmp_path, monkeypatch):
    path = configure(tmp_path, monkeypatch)
    def fail(*a, **k):
        raise OSError("test-secret")
    monkeypatch.setattr(upload_email.smtplib, "SMTP_SSL", fail)
    result = upload_email.send_batch_summary(upload_email.BatchSummary("root", "repo"), path)
    assert "失败" in result
    assert "test-secret" not in result


@pytest.mark.parametrize("fails", [False, True])
def test_batch_wrapper_sends_once_after_finish_or_error(monkeypatch, fails):
    from robocap_rerun_tools import web_app
    sent = []
    def run(*args, _summary, **kwargs):
        yield "checking"
        _summary.new = 2
        _summary.hours = 0.5
        if fails:
            raise ValueError("batch failed")
        yield "finished"
    monkeypatch.setattr(web_app, "_bulk_upload_modelscope_sessions", run)
    monkeypatch.setattr(upload_email, "send_batch_summary", lambda summary, path: sent.append(summary) or "sent")
    stream = web_app.bulk_upload_clean_modelscope_sessions("root", 4, False)
    assert next(stream) == "checking"
    assert sent == []
    if fails:
        with pytest.raises(ValueError):
            list(stream)
    else:
        assert list(stream)[-1].endswith("sent")
    assert len(sent) == 1
    assert sent[0].hours == 0.5
    assert bool(sent[0].errors) == fails
