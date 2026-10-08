import signal
import unittest
from unittest.mock import Mock, patch

from stock_guide_agent.lifecycle import run_polling


class LifecycleTests(unittest.TestCase):
    def test_stop_finishes_poll_and_closes_before_restoring_handlers(self):
        handlers = {}
        order = []
        def register(sig, handler):
            prior = handlers.get(sig, signal.SIG_DFL)
            handlers[sig] = handler
            return prior
        runtime = Mock()
        def poll(**kwargs):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            order.append("poll_finished")
        def close():
            self.assertTrue(callable(handlers[signal.SIGTERM]))
            order.append("close")
        runtime.poll_once.side_effect = poll
        runtime.close.side_effect = close
        with patch("stock_guide_agent.lifecycle.signal.signal", side_effect=register):
            run_polling(runtime)
        runtime.poll_once.assert_called_once_with(timeout_seconds=30)
        self.assertEqual(order, ["poll_finished", "close"])
        self.assertEqual(handlers[signal.SIGTERM], signal.SIG_DFL)

    def test_poll_failure_still_closes_and_restores_handlers(self):
        runtime = Mock()
        runtime.poll_once.side_effect = RuntimeError("failure")
        with patch("stock_guide_agent.lifecycle.signal.signal", return_value=signal.SIG_DFL) as register:
            with self.assertRaises(RuntimeError):
                run_polling(runtime)
        runtime.close.assert_called_once()
        self.assertEqual(register.call_count, 4)

    def test_once_returns_result_and_closes(self):
        runtime, callback = Mock(), Mock()
        with patch("stock_guide_agent.lifecycle.signal.signal"):
            run_polling(runtime, once=True, on_once=callback)
        runtime.poll_once.assert_called_once_with(timeout_seconds=0)
        callback.assert_called_once_with(runtime.poll_once.return_value)
        runtime.close.assert_called_once()
