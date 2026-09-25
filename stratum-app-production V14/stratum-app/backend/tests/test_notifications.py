import risk

ALERTS = [{"rule_type": "pace", "severity": "high", "feature_id": "PAD-A", "message": "PAD-A is 12 points behind"}]


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


def test_resend_is_preferred_and_called_correctly(monkeypatch):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append((url, headers, json))
        return FakeResp(200, {"id": "email_123"})

    monkeypatch.setattr(risk.requests, "post", fake_post)
    env = {"RESEND_API_KEY": "re_test", "ALERT_FROM_EMAIL": "alerts@stratum.example", "SMTP_HOST": "smtp.example"}
    result = risk.send_alert_email(ALERTS, "Hollow Creek", ["pm@example.com"], env=env)
    assert result == {"sent": True, "provider": "resend", "id": "email_123"}
    url, headers, body = calls[0]
    assert url == "https://api.resend.com/emails"
    assert headers["Authorization"] == "Bearer re_test"
    assert body["to"] == ["pm@example.com"] and body["from"] == "alerts@stratum.example"
    assert "PAD-A is 12 points behind" in body["text"] and "Hollow Creek" in body["subject"]


def test_resend_error_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setattr(risk.requests, "post", lambda *a, **k: FakeResp(422, {"message": "domain not verified"}))
    env = {"RESEND_API_KEY": "re_test", "ALERT_FROM_EMAIL": "a@b.example"}
    result = risk.send_alert_email(ALERTS, "Site", ["x@example.com"], env=env)
    assert result["sent"] is False and "422" in result["reason"]


def test_no_provider_or_no_recipients():
    assert risk.send_alert_email(ALERTS, "Site", ["x@example.com"], env={})["sent"] is False
    env = {"RESEND_API_KEY": "re_test", "ALERT_FROM_EMAIL": "a@b.example"}
    assert risk.send_alert_email(ALERTS, "Site", [], env=env)["reason"] == "no recipients configured"
