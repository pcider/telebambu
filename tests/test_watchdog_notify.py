"""deploy/openwrt/telebambu-notify: watchdog reports to the log chat through the Telegram API."""
import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

_PATH = os.path.join(os.path.dirname(__file__), '..', 'deploy', 'openwrt', 'telebambu-notify')
_loader = importlib.machinery.SourceFileLoader('telebambu_notify', _PATH)
_spec = importlib.util.spec_from_loader('telebambu_notify', _loader)
notify = importlib.util.module_from_spec(_spec)
_loader.exec_module(notify)

TOKEN = '123456:SECRET-TOKEN'


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Notify(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.config = os.path.join(self.dir.name, 'config.yml')
        self.queue = os.path.join(self.dir.name, 'pending')
        self.write_config("'-100123/7'")
        self.requests = []

    def tearDown(self):
        self.dir.cleanup()

    def write_config(self, log_chat_id: str):
        with open(self.config, 'w') as f:
            f.write(f"telegram_bot_token: '{TOKEN}'\nlog_chat_id: {log_chat_id}\n")

    def fake_urlopen(self, ok: bool = True):
        def urlopen(request, timeout):
            if not ok:
                raise OSError(f'network unreachable for {request.full_url}')
            self.requests.append((request.full_url, urllib.parse.parse_qs(request.data.decode()), timeout))
            return FakeResponse(b'{"ok": true}')
        return urlopen

    def run_notify(self, *args: str, ok: bool = True) -> int:
        with patch.object(notify.urllib.request, 'urlopen', self.fake_urlopen(ok)):
            return notify.main(['--config', self.config, '--queue', self.queue, *args])

    def test_sends_to_the_log_chat_topic(self):
        self.assertEqual(self.run_notify('Bot is down (no process). Restarting it.'), 0)
        url, fields, timeout = self.requests[0]
        self.assertEqual(url, f'https://api.telegram.org/bot{TOKEN}/sendMessage')
        self.assertEqual(fields['chat_id'], ['-100123'])
        self.assertEqual(fields['message_thread_id'], ['7'])
        self.assertTrue(fields['text'][0].startswith('Watchdog ('))
        self.assertIn('Bot is down (no process). Restarting it.', fields['text'][0])
        self.assertEqual(timeout, notify.TIMEOUT)

    def test_plain_chat_id_has_no_thread(self):
        self.write_config("'-100123'")
        self.run_notify('hello')
        self.assertNotIn('message_thread_id', self.requests[0][1])

    def test_failed_send_is_queued_and_sent_first_next_time(self):
        with patch('sys.stderr') as stderr:
            self.assertEqual(self.run_notify('Bot is down', ok=False), 1)
        errors = ''.join(call.args[0] for call in stderr.write.call_args_list)
        self.assertNotIn(TOKEN, errors)  # the token must never end up in the watchdog log
        with open(self.queue) as f:
            self.assertEqual(len(f.readlines()), 1)

        self.assertEqual(self.run_notify('Restarted after: bot was down.'), 0)
        texts = [fields['text'][0] for _url, fields, _t in self.requests]
        self.assertIn('Bot is down', texts[0])
        self.assertIn('Restarted after', texts[1])
        self.assertFalse(os.path.exists(self.queue))

    def test_flush_only_sends_just_the_queue(self):
        with open(self.queue, 'w') as f:
            f.write(json.dumps('queued report') + '\n')
        self.assertEqual(self.run_notify('--flush-only'), 0)
        self.assertEqual([fields['text'][0] for _u, fields, _t in self.requests], ['queued report'])

    def test_missing_log_chat_keeps_the_report_queued(self):
        with open(self.config, 'w') as f:
            f.write(f"telegram_bot_token: '{TOKEN}'\nlog_chat_id: null\n")
        with patch('sys.stderr'):
            self.assertEqual(self.run_notify('Bot is down'), 1)
        self.assertTrue(os.path.exists(self.queue))

    def test_long_reports_are_cut_to_telegrams_limit(self):
        self.run_notify('x' * 10000)
        self.assertEqual(len(self.requests[0][1]['text'][0]), notify.MAX_TEXT)


if __name__ == '__main__':
    unittest.main()
