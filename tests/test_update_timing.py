"""Update timing: slow updates are traced to the step that caused them."""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from printers.monitor import PrinterMonitor
from printers.timing import LoopLagProbe, StepTimer, span, use_timer


def make_monitor(**settings) -> tuple[PrinterMonitor, Mock]:
    values = dict(update_interval=0.05, update_deviation_warn=0.03, stats_log_interval=None,
                  auto_restart_printers=False, update_time_budget=5)
    values.update(settings)
    pm = Mock()
    pm.numbers.return_value = []
    pm.reconnect_if_needed = AsyncMock()
    ms = Mock(storage=Mock(), log_message=AsyncMock(), update_status_message=AsyncMock(),
              flush_logs=AsyncMock())
    return PrinterMonitor(pm, ms, SimpleNamespace(**values)), ms


def logged(ms: Mock) -> list[str]:
    return [c.args[0] for c in ms.log_message.await_args_list]


class StepTimerTests(unittest.TestCase):
    def test_breakdown_lists_slowest_steps_first(self):
        timer = StepTimer()
        timer.spans = {'status msg': 5.2, 'P4 PRINT_FINISHED': 31.4, 'P1 poll': 0.01}
        self.assertEqual(timer.breakdown(), 'P4 PRINT_FINISHED 31.40s, status msg 5.20s')

    def test_span_outside_an_update_is_a_no_op(self):
        with span('anything'):
            pass  # no current timer: must not raise

    def test_nested_and_open_steps(self):
        timer = StepTimer()
        with use_timer(timer):
            with span('outer'):
                with span('inner'):
                    self.assertEqual(timer.open_steps(), ['outer', 'inner'])
        self.assertEqual(timer.open_steps(), [])
        self.assertEqual(set(timer.spans), {'outer', 'inner'})


class LoopLagTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocking_call_shows_up_as_loop_lag(self):
        probe = LoopLagProbe(interval=0.01)
        probe.start()
        await asyncio.sleep(0.03)
        time.sleep(0.2)  # blocks the event loop, like a synchronous library call
        await asyncio.sleep(0.03)
        probe.stop()
        self.assertGreater(probe.take_max(), 0.15)
        self.assertEqual(probe.take_max(), 0.0)


class SlowUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_time_update_is_not_reported(self):
        monitor, ms = make_monitor(update_interval=5, update_deviation_warn=0.5)
        await monitor._check_slow_update(5.4, 5, 0.0)
        ms.log_message.assert_not_awaited()

    async def test_late_update_logs_the_previous_updates_steps(self):
        monitor, ms = make_monitor(update_interval=5, update_deviation_warn=0.5)
        monitor._last_timer = StepTimer()
        monitor._last_timer.spans = {'P4 camera': 20.0, 'P4 PRINT_FINISHED': 31.4, 'status msg': 2.9}
        monitor._last_timer.total = 34.3
        await monitor._check_slow_update(39.28, 5, 0.0)
        text = logged(ms)[0]
        self.assertIn('Slow update 39.28s (+34.28s)', text)
        self.assertIn('previous update took 34.30s [P4 PRINT_FINISHED 31.40s, P4 camera 20.00s', text)
        self.assertTrue(ms.log_message.await_args.kwargs.get('stdout_only'))

    async def test_disabled_report_still_tracks_the_slowest_for_stats(self):
        monitor, ms = make_monitor(update_interval=5, update_deviation_warn=None)
        await monitor._check_slow_update(39.28, 5, 0.0)
        ms.log_message.assert_not_awaited()
        self.assertEqual(monitor._slowest[0], 39.28)

    async def test_slow_updates_are_grouped_for_the_log_chat(self):
        monitor, ms = make_monitor(update_interval=5, update_deviation_warn=0.5)
        for gap in (6.0, 12.0, 7.0):
            await monitor._check_slow_update(gap, 5, 0.0)
        ms.log_message.reset_mock()
        await monitor._report_slow_updates_if_due()
        ms.log_message.assert_not_awaited()  # not a minute yet

        monitor._slow_reported_at -= 61
        await monitor._report_slow_updates_if_due()
        text = logged(ms)[0]
        self.assertTrue(text.startswith('3 slow updates in the last 1m, worst 12.00s'), text)
        self.assertFalse(ms.log_message.await_args.kwargs.get('stdout_only'))

    async def test_stats_include_p95_count_and_slowest_breakdown(self):
        monitor, ms = make_monitor(update_interval=5, update_deviation_warn=0.5, stats_log_interval=1)
        monitor.pm.get_status_lines.return_value = []
        monitor._tick_gaps = [5.1] * 19 + [39.28]
        monitor._slowest = (39.28, '39.28s (+34.28s): previous update took ...')
        monitor._stats_since -= 2
        await monitor.log_stats_if_due()
        text = logged(ms)[0]
        self.assertIn('p95 39.28s, max 39.28s, 1 over 5.5s', text)
        self.assertIn('Slowest: 39.28s (+34.28s)', text)
        self.assertIsNone(monitor._slowest)

    async def test_run_names_the_step_that_made_an_update_late(self):
        monitor, ms = make_monitor()

        async def slow_status(_text):
            await asyncio.sleep(0.15)
        ms.update_status_message.side_effect = slow_status

        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        slow = [t for t in logged(ms) if t.startswith('Slow update')]
        self.assertTrue(slow, logged(ms))
        self.assertIn('status msg 0.1', slow[0])


if __name__ == '__main__':
    unittest.main()
