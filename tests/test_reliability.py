import argparse
import asyncio
import sqlite3
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from mailagent import cli
from mailagent.config import Config, MailboxConfig, TelegramConfig
from mailagent.db import Database
from mailagent.mail import watcher as watcher_mod
from mailagent.mail.watcher import MailWatcher
from mailagent.models import OutMessage


def test_idle_without_new_mail_advances_heartbeat(monkeypatch):
    checks = []
    watcher = MailWatcher(MailboxConfig('t', 'me@example.invalid', 'test'), lambda: (1, 5),
                          lambda *a: None, lambda *a: None, lambda *a: checks.append(a))

    class Box:
        def __init__(self, cfg):
            pass

        def connect(self):
            pass

        def select_inbox(self):
            return 1, 6

        def fetch_new(self, uid, limit=50):
            return []

        def idle_wait(self, timeout):
            return False

    original = watcher._catch_up

    def catch_up(*args):
        original(*args)
        if checks:
            watcher._stop.set()

    monkeypatch.setattr(watcher_mod, 'ImapMailbox', Box)
    monkeypatch.setattr(watcher, '_catch_up', catch_up)
    watcher._session()
    assert checks == [('ok', None), ('ok', None)]


def test_doctor_fails_when_daemon_stopped(monkeypatch, capsys):
    cfg = Config(mailboxes=[MailboxConfig('t', 'me@example.invalid', 'test')],
                 telegram=TelegramConfig(bot_token='test', owner_chat_id='42'))

    async def models(*args):
        return []

    async def channel(*args):
        return 'mock channel ok'

    monkeypatch.setattr(cli, 'load_config', lambda: cfg)
    monkeypatch.setattr(cli, '_jev_models', models)
    monkeypatch.setattr(cli, '_imap_check', lambda mb: None)
    monkeypatch.setattr(cli, '_with_channel', channel)
    monkeypatch.setattr(cli, 'service_status', lambda: 'installed, not running')
    assert cli.cmd_doctor(argparse.Namespace()) == 1
    assert 'All good' not in capsys.readouterr().out
    monkeypatch.setattr(cli, 'service_status', lambda: 'running')
    assert cli.cmd_doctor(argparse.Namespace()) == 0


def test_sync_and_arrival_timestamps_are_distinct(monkeypatch):
    from mailagent import db as db_mod

    db = Database(':memory:')
    first = datetime(2026, 11, 6, 8, tzinfo=timezone.utc)
    second = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(db_mod, 'utcnow', lambda: first)
    db.set_mailbox_status('t', 'ok')
    monkeypatch.setattr(db_mod, 'utcnow', lambda: second)
    db.set_mailbox_status('t', 'ok')
    state = db.stats()['mailboxes'][0]
    assert state['last_sync_at'] == second.isoformat()
    assert state['last_message_at'] is None
    db.close()


def _notification(db, uid, *, thread='thread', mailbox='t', urgent=False):
    from test_ai_offline import _mail

    mail = replace(_mail(), uid=uid, mailbox_id=mailbox, gm_msgid=str(uid), gm_thrid=thread,
                   message_id=f'<{uid}@example.invalid>')
    [msg_id] = db.ingest_messages(mailbox, 1, [mail])
    db.enqueue_mail_notification(msg_id, mailbox, thread, 'telegram', '42',
                                 OutMessage(title=f'Alert {uid}', text=f'Content {uid}'),
                                 datetime.now(timezone.utc), urgent)
    return msg_id


def test_followups_group_across_restart_and_all_get_delivery_markers(tmp_path):
    path = tmp_path / 'agent.db'
    db = Database(path)
    now = datetime.now(timezone.utc)
    _notification(db, 1)
    [first] = db.ready_outbox(now + timedelta(seconds=5))
    db.claim_outbox(first['id'])
    db.outbox_sent(first['id'], 'provider-1')
    second_id = _notification(db, 2)
    third_id = _notification(db, 3)
    db.enqueue_mail_notification(third_id, 't', 'thread', 'telegram', '42', OutMessage(text='duplicate'), now, False)
    assert not db.ready_outbox(now + timedelta(seconds=5))
    db.close()
    db = Database(path)
    [followup] = db.ready_outbox(now + timedelta(minutes=6))
    payload = json.loads(followup['payload'])
    assert 'Content 2' in payload['text'] and 'Content 3' in payload['text']
    assert 'duplicate' not in payload['text'] and '2 conversation updates' in payload['title']
    db.outbox_sent(followup['id'], 'provider-2')
    assert db.get_message(second_id)['notified_at'] and db.get_message(third_id)['notified_at']
    db.close()


def test_grouping_never_mutates_inflight_retry_or_urgent_alerts():
    db = Database(':memory:')
    now = datetime.now(timezone.utc)
    _notification(db, 1)
    [first] = db.ready_outbox(now + timedelta(seconds=5))
    claimed = db.claim_outbox(first['id'])
    _notification(db, 2)
    assert db.claim_outbox(first['id']) is None
    assert db._one('select payload from outbox where id=?', (first['id'],))['payload'] == claimed['payload']
    db.reset_outbox_claims()
    assert db._one('select attempts from outbox where id=?', (first['id'],))['attempts'] == 1
    _notification(db, 3, urgent=True)
    _notification(db, 4, mailbox='other')
    _notification(db, 5, thread='another')
    ready = db.ready_outbox(now + timedelta(seconds=5))
    assert len(ready) == 4  # retry, urgent, other mailbox, other thread; ordinary follow-up waits
    db.close()


def test_large_burst_has_bounded_groups_without_losing_items():
    db = Database(':memory:')
    for uid in range(1, 36):
        _notification(db, uid)
    rows = db.ready_outbox(datetime.now(timezone.utc) + timedelta(minutes=6))
    assert len(rows) == 4
    assert db._one('select count(*) n from notification_items')['n'] == 35
    for row in rows:
        assert db._one('select count(*) n from notification_items where outbox_id=?', (row['id'],))['n'] <= 10
    db.close()


def test_overdue_review_is_read_only_until_owner_taps_and_paginates():
    from test_suggest import _agent, _ib

    async def run():
        agent, db, cap, _ = _agent(None)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
        for i in range(7):
            db.upsert_todo(dedupe_key=f'task:{i}', title=f'Old task {i}', kind='task',
                           due_date=yesterday, due_time=None, timezone_name='UTC', remind_at=None)
        await agent.handle(_ib(text='/overdue 2'))
        [(mid, card)] = list(cap.msgs.items())
        assert 'Page 2/2' in card.text and len(card.buttons) == 2
        assert len(db.open_todos()) == 7
        await agent.handle(_ib(callback_data=card.buttons[0][1].data, message_id=mid))
        assert len(db.open_todos()) == 6
        assert 'Page 2/2' in cap.msgs[mid].text
        assert db._one("select count(*) n from todos where status='dismissed'")['n'] == 1
        await agent.handle(_ib(callback_data=card.buttons[0][1].data, message_id=mid))
        assert len(db.open_todos()) == 6

    asyncio.run(run())


def test_old_overdue_backlog_cannot_hide_imminent_tasks():
    from mailagent.agent.scheduler import Scheduler

    db = Database(':memory:')
    today = datetime.now(timezone.utc).date()
    for i in range(110):
        db.upsert_todo(dedupe_key=f'old:{i}', title='Old', kind='task',
                       due_date=(today - timedelta(days=10)).isoformat(), due_time=None,
                       timezone_name='UTC', remind_at=None)
    db.upsert_todo(dedupe_key='soon', title='Tomorrow', kind='deadline',
                   due_date=(today + timedelta(days=1)).isoformat(), due_time=None,
                   timezone_name='UTC', remind_at=None)
    scheduler = Scheduler(Config(), db, None)
    scheduler.cfg.prefs.timezone = 'UTC'
    tasks = scheduler._digest_todos()
    assert tasks[0]['title'] == 'Tomorrow' and len(tasks) == 6
    assert db.stats()['open_todos'] == 111
    msg, _ = asyncio.run(scheduler.compose_digest('test', manual=True))
    assert '110 overdue' in msg.text and '/overdue' in msg.text
    db.close()


def test_cli_error_category_survives_banner_without_logging_mail_or_tokens():
    from mailagent.ai.llm.codex_cli import failure_detail

    raw = 'CLI banner ' * 100 + 'private email to user@example.invalid secret-token\nHTTP error: 426 Upgrade Required'
    assert failure_detail(raw) == 'HTTP 426'
    assert failure_detail('HTTP/1.1 401 unauthorized token=secret') == 'HTTP 401'
    assert 'secret' not in failure_detail('arbitrary diagnostic secret')


def test_quiet_hour_followup_is_not_merged_into_an_earlier_ready_alert():
    from test_ai_offline import _mail

    db = Database(':memory:')
    _notification(db, 1)
    [msg_id] = db.ingest_messages('t', 1, [replace(_mail(), uid=2, gm_msgid='2', gm_thrid='thread')])
    now = datetime.now(timezone.utc)
    db.enqueue_mail_notification(msg_id, 't', 'thread', 'telegram', '42', OutMessage(text='quiet follow-up'),
                                 now + timedelta(hours=8), False)
    [ready] = db.ready_outbox(now + timedelta(minutes=1))
    assert 'quiet follow-up' not in ready['payload']
    assert len(db.ready_outbox(now + timedelta(hours=9))) == 2
    db.close()


def test_exhausted_analysis_can_be_explicitly_retried_and_handled_mail_is_retired():
    from test_agent_units import _FlakyLLM, _process_one_mail

    db, msg_id = _process_one_mail(_FlakyLLM(99), 3)
    now = datetime.now(timezone.utc)
    db.seed_analysis_recovery(['me'], now)
    db.analysis_recovery_failed(msg_id, 'test failure', None)
    assert db.retry_failed_analyses(['other'], now) == 0
    assert db.retry_failed_analyses(['me'], now) == 1
    assert db.recovery_counts() == (1, 0)
    db.mark_handled(msg_id)
    db.seed_analysis_recovery(['me'], now)
    assert db.recovery_counts() == (0, 0)
    db.close()


def test_grouped_card_keeps_earlier_updates_after_latest_mail_is_marked_done():
    from test_suggest import _agent, _ib

    async def run():
        agent, db, cap, _ = _agent(None)
        _notification(db, 1)
        latest = _notification(db, 2)
        await agent.handle(_ib(callback_data=f'seen:{latest}', message_id='group-card'))
        card = cap.msgs['group-card']
        assert 'Content 1' in card.text and 'Content 2' in card.text
        assert db.get_message(latest)['handled'] == 1
        assert card.buttons == []

    asyncio.run(run())


def test_recovered_deadlines_commit_atomically_and_do_not_repeat_with_new_wording():
    from test_ai_offline import _mail

    db = Database(':memory:')
    [msg_id] = db.ingest_messages('t', 1, [_mail()])
    row = dict(dedupe_key='first', title='First task', kind='deadline', due_date='2030-01-01',
               due_time=None, timezone='UTC', remind_at=None)
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_mail_todos(msg_id, 't', '', [row, {**row, 'dedupe_key': 'second', 'title': None}])
    assert not db.open_todos()
    assert len(db.insert_mail_todos(msg_id, 't', '', [row, {**row, 'dedupe_key': 'second'}])) == 2
    assert db.insert_mail_todos(msg_id, 't', '', [{**row, 'dedupe_key': 'reworded'}]) == []
    assert len(db.open_todos()) == 2
    db.close()


def test_analysis_recovery_survives_restart_without_replaying_mail(monkeypatch, tmp_path):
    from mailagent.agent import pipeline as module
    from mailagent.agent.pipeline import Pipeline
    from test_agent_units import _FlakyLLM, _ImportantJev, _process_one_mail

    original, msg_id = _process_one_mail(_FlakyLLM(99), 3)
    path = tmp_path / 'agent.db'
    with sqlite3.connect(path) as dest:
        original._conn.backup(dest)
    original.close()
    cfg = Config(mailboxes=[MailboxConfig('me', 'me@example.invalid', 'test')],
                 telegram=TelegramConfig(bot_token='test', owner_chat_id='42'))
    db = Database(path)
    now = [datetime.now(timezone.utc)]
    monkeypatch.setattr(module, 'utcnow', lambda: now[0])
    pipe = Pipeline(cfg, db, _ImportantJev(), _FlakyLLM(0))
    asyncio.run(pipe.recover_analysis())
    assert db.recovery_counts() == (1, 0)
    db.close()
    db = Database(path)
    pipe = Pipeline(cfg, db, _ImportantJev(), _FlakyLLM(0))
    now[0] += timedelta(minutes=16)
    asyncio.run(pipe.recover_analysis())
    asyncio.run(pipe.recover_analysis())
    assert db.get_message(msg_id)['analysis_source'] == 'llm'
    assert db.recovery_counts() == (0, 0)
    assert len(db.open_todos()) == 1
    rows = db.ready_outbox(now[0] + timedelta(days=1))
    assert sum(r['dedupe_key'] == f'notify:{msg_id}' for r in rows) == 1
    assert sum(r['dedupe_key'].startswith('analysis:degraded:') for r in rows) == 1
    assert sum(r['dedupe_key'].startswith('analysis:recovered:') for r in rows) == 1
    db.close()


def test_analysis_recovery_is_bounded_and_does_not_create_fake_dates(monkeypatch):
    from mailagent.agent import pipeline as module
    from mailagent.agent.pipeline import Pipeline
    from test_agent_units import _FlakyLLM, _ImportantJev, _process_one_mail

    db, msg_id = _process_one_mail(_FlakyLLM(99), 3)
    cfg = Config(mailboxes=[MailboxConfig('me', 'me@example.invalid', 'test')])
    model = _FlakyLLM(99)
    pipe = Pipeline(cfg, db, _ImportantJev(), model)
    now = [datetime.now(timezone.utc)]
    monkeypatch.setattr(module, 'utcnow', lambda: now[0])
    asyncio.run(pipe.recover_analysis())
    for _ in range(8):
        now[0] += timedelta(hours=5)
        asyncio.run(pipe.recover_analysis())
    assert model.calls == module.RECOVERY_ATTEMPTS
    assert db.recovery_counts() == (0, 1)
    assert db.get_message(msg_id)['analysis_source'] == 'template'
    assert not db.open_todos()
    db.close()


def test_backup_includes_wal_and_restores_without_touching_live_db(tmp_path):
    from mailagent.backup import create_backup, verify_backup

    path = tmp_path / 'agent.db'
    db = Database(path)
    db.kv_set('committed_in_wal', 'kept')
    assert path.with_suffix('.db-wal').exists()
    snapshot = create_backup(path)
    db.kv_set('after_snapshot', 'not in snapshot')
    counts = verify_backup(snapshot)
    with sqlite3.connect(snapshot) as restored:
        assert restored.execute("select value from kv where key='committed_in_wal'").fetchone() == ('kept',)
        assert restored.execute("select value from kv where key='after_snapshot'").fetchone() is None
    assert counts['kv'] == 1 and db.kv_get('after_snapshot') == 'not in snapshot'
    assert snapshot.stat().st_mode & 0o777 == 0o600
    assert snapshot.parent.stat().st_mode & 0o777 == 0o700
    db.close()


def test_backup_retention_and_invalid_restore(tmp_path):
    from mailagent.backup import create_backup, verify_backup

    source = tmp_path / 'agent.db'
    db = Database(source)
    snapshots = [create_backup(source) for _ in range(8)]
    assert not snapshots[0].exists()
    assert len(list((tmp_path / 'backups').glob('mail-agent-*.sqlite3'))) == 7
    bad = tmp_path / 'bad.db'
    bad.write_bytes(b'not sqlite')
    with pytest.raises(sqlite3.DatabaseError):
        verify_backup(bad)
    assert not list((tmp_path / 'backups').glob('.snapshot-*'))
    db.close()


def test_daily_backup_persists_schedule_and_failure_retries(monkeypatch, tmp_path):
    from mailagent.agent import scheduler as module
    from mailagent.agent.scheduler import Scheduler

    db = Database(tmp_path / 'agent.db')
    clock = [0.0]
    calls = []

    def snapshot(path):
        calls.append(path)
        if len(calls) == 1:
            raise OSError('disk full')
        return tmp_path / 'snapshot.sqlite3'

    monkeypatch.setattr(module, 'create_backup', snapshot)
    monkeypatch.setattr(module._time, 'monotonic', lambda: clock[0])
    scheduler = Scheduler(Config(), db, None)
    asyncio.run(scheduler.backup_if_due())
    assert db.kv_get('backup_error') == 'OSError'
    asyncio.run(scheduler.backup_if_due())
    assert len(calls) == 1
    clock[0] = 3601
    asyncio.run(scheduler.backup_if_due())
    assert db.kv_get('backup_last_success') and db.kv_get('backup_error') == ''
    asyncio.run(Scheduler(Config(), db, None).backup_if_due())
    assert len(calls) == 2
    db.close()
