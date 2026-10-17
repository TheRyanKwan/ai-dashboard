# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent polling keeps network requests out of the animation loop."""

import copy
import logging
import threading
import time

from ._vendor.turing.ai_dashboard import (
    AccountCache,
    DailyCallsCache,
    codex_snapshot,
    quota_to_rmb,
)

log = logging.getLogger(__name__)


def demo_codex(state="working"):
    return dict(state=state, session_tokens=1250000, last_task_tokens=24360,
                session_cost=1.2846, last_task_cost=.0372, cost_currency="CNY",
                project="turzx-dashboard")


class DashboardData:
    def __init__(self, settings):
        self.settings = dict(settings)
        self.account = AccountCache()
        self.daily = DailyCallsCache()
        self.chart = DailyCallsCache(latest_only=True)
        self._lock = threading.Lock()
        self.stop = threading.Event()
        self.threads = []
        self._values = dict(balance=None, spend=None, average=None, calls=None,
                            codex=dict(state="idle"),
                            account_status="Collecting balance", daily_status="Collecting totals",
                            chart_status="Collecting calls", codex_status="")

    def _publish(self, **values):
        with self._lock:
            self._values.update(values)

    def poll_account(self):
        self.account.refresh(self.settings)
        balance = (quota_to_rmb(self.account.data["quota"], self.settings)
                   if self.account.data is not None else None)
        self._publish(balance=balance, account_status=self.account.error)

    def poll_daily(self):
        self.daily.refresh(self.settings)
        calls = self.daily.calls
        spend = (quota_to_rmb(sum(cost for _, cost in calls), self.settings)
                 if calls is not None else None)
        average = spend / len(calls) if calls else None
        self._publish(spend=spend, average=average, daily_status=self.daily.error)

    def poll_chart(self):
        self.chart.refresh(self.settings)
        calls = ([dict(self.chart.usage.get(identity, {}),
                       cost=quota_to_rmb(cost, self.settings))
                  for identity, cost in self.chart.calls[-60:]]
                 if self.chart.calls is not None else None)
        self._publish(calls=calls, chart_status=self.chart.error)

    def poll_codex(self):
        self._publish(codex=codex_snapshot(self.settings), codex_status="")

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._values)

    def refresh(self):
        """One bounded collection for a still preview; live playback uses workers."""
        for poll in (self.poll_account, self.poll_daily, self.poll_chart, self.poll_codex):
            poll()
        return self.snapshot()

    def start(self):
        for poll in (self.poll_account, self.poll_daily, self.poll_chart, self.poll_codex):
            thread = threading.Thread(target=self._loop, args=(poll,), daemon=True,
                                      name=poll.__name__)
            self.threads.append(thread)
            thread.start()

    def _loop(self, poll):
        while not self.stop.is_set():
            try:
                poll()
            except Exception as exc:
                # Never log request exceptions: URLs or headers can contain credentials.
                log.warning("%s failed (%s); will retry", poll.__name__, type(exc).__name__)
                field = poll.__name__.removeprefix("poll_") + "_status"
                self._publish(**{field: "Collection failed; retrying"})
            self.stop.wait(.5)

    def close(self):
        self.stop.set()
        deadline = time.monotonic() + 1
        for thread in self.threads:
            thread.join(max(0, deadline - time.monotonic()))
