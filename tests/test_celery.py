"""Tests for the Celery configuration: tasks run inline and never reach a broker."""

import pytest

from mtg_gauntlet.celery import app


def test_tests_run_tasks_inline():
    assert app.conf.task_always_eager


def test_a_message_to_the_broker_fails_the_test():
    with pytest.raises(AssertionError, match="broker"):
        app.send_task("playtest.run_match", args=["x"])


def test_the_worker_takes_one_long_task_at_a_time_and_keeps_no_results():
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.task_ignore_result
