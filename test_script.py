import importlib
import csv
import io
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("SCREENSHOT_DIR", str(tmp_path / "screenshots"))

    app_module = importlib.import_module("app")
    models = importlib.import_module("models")
    alerts = importlib.import_module("alerts")
    scheduler = importlib.import_module("scheduler")
    scraper = importlib.import_module("scraper")

    app = app_module.create_app({
        "TESTING": True,
        "WTF_CSRF_ENABLED": False,
        "SECRET_KEY": "test-secret",
    })

    with app.app_context():
        models.db.drop_all()
        models.db.create_all()
        user = models.User(email="test@example.invalid", password_hash="!disabled!")
        models.db.session.add(user)
        models.db.session.commit()
        user_id = user.id

    monkeypatch.setattr(
        app_module,
        "current_user",
        lambda: SimpleNamespace(id=user_id),
    )

    return app, app.test_client(), models, alerts, scheduler, scraper


def test_dashboard_loads(project):
    app, client, *_ = project
    response = client.get("/")
    assert response.status_code == 200
    assert b"Website monitoring" in response.data


def test_monitor_partial_loads(project):
    _, client, *_ = project
    response = client.get("/partials/monitors")
    assert response.status_code == 200


def test_monitor_search_and_filters(project):
    app, client, models, *_ = project
    with app.app_context():
        user = models.User.query.first()
        models.db.session.add_all(
            [
                models.Monitor(
                    user_id=user.id,
                    name="Pricing page",
                    url="https://example.com/pricing",
                    canonical_url="https://example.com/pricing",
                    interval_minutes=60,
                    status="OK",
                ),
                models.Monitor(
                    user_id=user.id,
                    name="Blog page",
                    url="https://example.com/blog",
                    canonical_url="https://example.com/blog",
                    interval_minutes=60,
                    status="ERROR",
                    enabled=False,
                ),
            ]
        )
        models.db.session.commit()

    response = client.get("/partials/monitors?q=pricing")
    assert b"Pricing page" in response.data
    assert b"Blog page" not in response.data
    response = client.get("/partials/monitors?status=ERROR&enabled=paused")
    assert b"Blog page" in response.data
    assert b"Pricing page" not in response.data


def test_export_monitor_history_returns_owned_csv(project):
    app, client, models, *_ = project
    with app.app_context():
        user = models.User.query.first()
        monitor = models.Monitor(
            user_id=user.id,
            name="Export test",
            url="https://example.com/",
            canonical_url="https://example.com/",
            interval_minutes=60,
        )
        models.db.session.add(monitor)
        models.db.session.flush()
        models.db.session.add(
            models.Snapshot(
                monitor_id=monitor.id,
                content_hash="a" * 64,
                content_text="Price: $10",
                title="Example",
                http_status=200,
                summary="Price changed.",
            )
        )
        models.db.session.commit()
        monitor_id = monitor.id

    response = client.get(f"/monitors/{monitor_id}/export.csv")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "attachment" in response.headers["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
    assert rows[0]["monitor_name"] == "Export test"
    assert rows[0]["content_text"] == "Price: $10"
    assert rows[0]["summary"] == "Price changed."


def test_monitor_and_snapshot_can_be_created(project):
    app, _, models, *_ = project

    with app.app_context():
        user = models.User.query.first()
        monitor = models.Monitor(
            user_id=user.id,
            name="Test monitor",
            url="https://example.com/",
            canonical_url="https://example.com/",
            interval_minutes=60,
        )
        models.db.session.add(monitor)
        models.db.session.flush()

        snapshot = models.Snapshot(
            monitor_id=monitor.id,
            content_hash="a" * 64,
            content_text="Initial content",
            title="Example",
            http_status=200,
        )
        models.db.session.add(snapshot)
        models.db.session.commit()

        assert models.db.session.get(models.Snapshot, snapshot.id).content_text == "Initial content"


def test_all_monitor_routes_are_registered(project):
    app, *_ = project
    rules = {
        (rule.rule, method)
        for rule in app.url_map.iter_rules()
        for method in rule.methods
    }
    assert ("/monitors", "POST") in rules
    assert ("/monitors/<int:monitor_id>/check", "POST") in rules
    assert ("/monitors/<int:monitor_id>/toggle", "POST") in rules
    assert ("/monitors/<int:monitor_id>/delete", "POST") in rules
    assert ("/monitors/<int:monitor_id>/picker", "POST") in rules


def test_invalid_monitor_input_does_not_return_500(project):
    _, client, *_ = project
    response = client.post(
        "/monitors",
        data={
            "name": "",
            "url": "not-a-url",
            "interval_minutes": "999999",
            "sensitivity": "invalid",
        },
        follow_redirects=False,
    )
    assert response.status_code < 500


def test_css_selector_parser_handles_commas_in_attributes(project):
    _, _, _, _, scheduler, _ = project
    selectors = scheduler.split_selectors('[data-label="one,two"], .price, main article')
    assert selectors == ['[data-label="one,two"]', ".price", "main article"]


def test_css_selector_parser_rejects_incomplete_selector(project):
    _, _, _, _, scheduler, _ = project
    with pytest.raises(ValueError):
        scheduler.split_selectors('[data-label="unfinished')


def test_cleaning_ignores_configured_elements(project):
    app, _, models, _, scheduler, _ = project

    with app.app_context():
        user = models.User.query.first()
        monitor = models.Monitor(
            user_id=user.id,
            name="Cleaning test",
            url="https://example.com/",
            canonical_url="https://example.com/",
            interval_minutes=60,
            ignore_selectors=".ad-slot",
        )
        content = "<main><p>Price is $12</p><div class='ad-slot'>Rotating ad</div></main>"
        cleaned = scheduler.clean_monitor_content(content, monitor)
        assert "Price is $12" in cleaned
        assert "Rotating ad" not in cleaned


def test_low_sensitivity_preserves_values_for_metric_tracking(project):
    app, _, models, _, scheduler, _ = project

    with app.app_context():
        user = models.User.query.first()
        monitor = models.Monitor(
            user_id=user.id,
            name="Metrics test",
            url="https://example.com/",
            canonical_url="https://example.com/",
            interval_minutes=60,
            sensitivity="low",
        )
        models.db.session.add(monitor)
        models.db.session.flush()
        normalized = scheduler.clean_monitor_content(
            "<main><p>Price: $123.45</p></main>", monitor
        )
        metric_text = scheduler.clean_monitor_content(
            "<main><p>Price: $123.45</p></main>",
            monitor,
            preserve_numbers=True,
        )

        assert "<number>" in normalized
        assert "$123.45" in metric_text


def test_queue_processing_claims_due_row_once(project, monkeypatch):
    app, _, models, _, scheduler, _ = project

    with app.app_context():
        row = models.CheckQueue(canonical_url="https://example.com/", due_at=models.utcnow())
        models.db.session.add(row)
        models.db.session.commit()

    monkeypatch.setattr(scheduler, "_check_url", lambda app, url: "OK")
    assert scheduler.process_check_queue(app, limit=1) == 1

    with app.app_context():
        row = models.db.session.scalar(select(models.CheckQueue))
        assert row.status == "DONE"


def test_hash_is_stable(project):
    _, _, _, _, _, scraper = project
    assert scraper.hash_content("same") == scraper.hash_content("same")
    assert scraper.hash_content("same") != scraper.hash_content("different")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/",
        "http://localhost/",
        "http://169.254.169.254/",
        "http://user:password@example.com/",
    ],
)
def test_scraper_rejects_unsafe_urls(project, url):
    _, _, _, _, _, scraper = project
    with pytest.raises(ValueError):
        scraper.canonicalize_url(url)


def test_alert_diff_contains_both_changed_lines(project):
    _, _, _, alerts, *_ = project
    changed = alerts.changed_text("Price\n$10\nAvailable", "Price\n$12\nUnavailable")
    assert "$10" in changed
    assert "$12" in changed
    assert "Unavailable" in changed


def test_keyword_filter_skips_unrelated_change(project, monkeypatch):
    app, _, models, alerts, *_ = project
    monkeypatch.setenv("SMTP_HOST", "smtp.example.invalid")
    monkeypatch.setenv("SMTP_USER", "sender@example.invalid")
    monkeypatch.setenv("SMTP_FROM", "sender@example.invalid")

    with app.app_context():
        user = models.User.query.first()
        monitor = models.Monitor(
            user_id=user.id,
            name="Keyword test",
            url="https://example.com/",
            canonical_url="https://example.com/",
            interval_minutes=60,
            keywords="price",
            alert_email="recipient@example.invalid",
        )
        models.db.session.add(monitor)
        models.db.session.flush()

        old = models.Snapshot(
            monitor_id=monitor.id,
            content_hash="1" * 64,
            content_text="Title: Product",
            title="Example",
            http_status=200,
        )
        new = models.Snapshot(
            monitor_id=monitor.id,
            content_hash="2" * 64,
            content_text="Page now says hello",
            title="Example",
            http_status=200,
        )
        models.db.session.add_all([old, new])
        models.db.session.commit()

        with patch("smtplib.SMTP") as smtp_mock, patch("requests.post") as requests_mock:
            assert alerts.notify_change(app, monitor, old, new) == 0
            smtp_mock.assert_not_called()
            requests_mock.assert_not_called()


def test_email_alert_uses_monitor_recipient(project, monkeypatch):
    app, _, models, alerts, *_ = project
    monkeypatch.setenv("SMTP_HOST", "smtp.example.invalid")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "sender@example.invalid")
    monkeypatch.setenv("SMTP_PASSWORD", "test-password")
    monkeypatch.setenv("SMTP_FROM", "sender@example.invalid")
    monkeypatch.setenv("SMTP_STARTTLS", "true")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    with app.app_context():
        user = models.User.query.first()
        monitor = models.Monitor(
            user_id=user.id,
            name="Email test",
            url="https://example.com/",
            canonical_url="https://example.com/",
            interval_minutes=60,
            alert_email="recipient@example.invalid",
        )
        models.db.session.add(monitor)
        models.db.session.flush()

        old = models.Snapshot(
            monitor_id=monitor.id,
            content_hash="3" * 64,
            content_text="Old price: $10",
            title="Example",
            http_status=200,
        )
        new = models.Snapshot(
            monitor_id=monitor.id,
            content_hash="4" * 64,
            content_text="New price: $12",
            title="Example",
            http_status=200,
        )
        models.db.session.add_all([old, new])
        models.db.session.commit()

        with patch("smtplib.SMTP") as smtp_mock:
            smtp = smtp_mock.return_value.__enter__.return_value
            delivered = alerts.notify_change(app, monitor, old, new)

        assert delivered == 1
        smtp.send_message.assert_called_once()
        sent_message = smtp.send_message.call_args.args[0]
        assert sent_message["To"] == "recipient@example.invalid"

        record = models.db.session.query(models.NotificationDelivery).filter_by(
            monitor_id=monitor.id,
            snapshot_id=new.id,
            channel="email",
        ).first()
        assert record is not None


def test_keyword_filter_alerts_when_keyword_is_removed(project, monkeypatch):
    app, _, models, alerts, *_ = project
    monkeypatch.setenv("SMTP_HOST", "smtp.example.invalid")
    monkeypatch.setenv("SMTP_USER", "sender@example.invalid")
    monkeypatch.setenv("SMTP_FROM", "sender@example.invalid")

    with app.app_context():
        user = models.User.query.first()
        monitor = models.Monitor(
            user_id=user.id,
            name="Keyword removal",
            url="https://example.com/",
            canonical_url="https://example.com/",
            interval_minutes=60,
            keywords="price",
            alert_email="recipient@example.invalid",
        )
        models.db.session.add(monitor)
        models.db.session.flush()
        old = models.Snapshot(
            monitor_id=monitor.id,
            content_hash="5" * 64,
            content_text="Price: $10",
            title="Example",
            http_status=200,
        )
        new = models.Snapshot(
            monitor_id=monitor.id,
            content_hash="6" * 64,
            content_text="Availability: In stock",
            title="Example",
            http_status=200,
        )
        models.db.session.add_all([old, new])
        models.db.session.commit()

        with patch("smtplib.SMTP") as smtp_mock:
            delivered = alerts.notify_change(app, monitor, old, new)

        assert delivered == 1
        smtp_mock.return_value.__enter__.return_value.send_message.assert_called_once()


def test_delete_unknown_monitor_does_not_return_500(project):
    _, client, *_ = project
    response = client.post("/monitors/999999/delete", follow_redirects=False)
    assert response.status_code in {302, 404}


def test_dashboard_has_alert_email_field(project):
    app, *_ = project
    template_path = Path(app.root_path) / "templates" / "dashboard.html"
    source = template_path.read_text(encoding="utf-8")
    assert re.search(r'name=["\']alert_email["\']', source)


def test_scheduler_functions_exist(project):
    _, _, _, _, scheduler, _ = project
    assert callable(scheduler.sync_jobs)
    assert callable(scheduler.start_scheduler)
    assert callable(scheduler._check_url)


def test_ai_summary_is_normalized_to_one_line(project, monkeypatch):
    summaries = importlib.import_module("summaries")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    response = type(
        "Response",
        (),
        {
            "raise_for_status": lambda self: None,
            "json": lambda self: {
                "choices": [{"message": {"content": "- Price changed\n- Stock changed"}}]
            },
        },
    )()
    with patch.object(summaries.requests, "post", return_value=response):
        result = summaries.summarize_change("Price: $10", "Price: $12")
    assert "\n" not in result
    assert result.startswith("Price changed")


if __name__ == "__main__":
    raise SystemExit(pytest.main(["-v", __file__]))