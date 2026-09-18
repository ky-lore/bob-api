import threading
import time

from app.tasks import job_tracker


def _wait_until_not_running(job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    job = job_tracker.get_job(job_id)
    while job["status"] == "running":
        if time.time() > deadline:
            raise TimeoutError(f"job {job_id} still running after {timeout}s")
        time.sleep(0.01)
        job = job_tracker.get_job(job_id)
    return job


def test_get_job_returns_none_for_unknown_id():
    assert job_tracker.get_job("does-not-exist") is None


def test_start_job_runs_fn_on_a_background_thread_and_records_the_result():
    job_id = job_tracker.start_job(lambda: {"answer": 42})

    job = _wait_until_not_running(job_id)
    assert job == {"status": "done", "result": {"answer": 42}, "error": None, "progress": None}


def test_start_job_records_a_raised_exception_instead_of_crashing():
    def _boom():
        raise RuntimeError("real failure")

    job_id = job_tracker.start_job(_boom)

    job = _wait_until_not_running(job_id)
    assert job["status"] == "error"
    assert job["result"] is None
    assert "real failure" in job["error"]


def test_a_zero_arg_fn_has_no_progress_while_running():
    started = threading.Event()
    release = threading.Event()

    def _slow():
        started.set()
        release.wait(timeout=5.0)
        return {"done": True}

    job_id = job_tracker.start_job(_slow)
    started.wait(timeout=5.0)

    assert job_tracker.get_job(job_id)["progress"] is None

    release.set()
    _wait_until_not_running(job_id)


def test_a_one_arg_fn_receives_a_report_progress_callback_visible_mid_run():
    started = threading.Event()
    release = threading.Event()

    def _slow_with_progress(report_progress):
        report_progress({"phase": "gathering", "completed": 0, "total": 3})
        started.set()
        release.wait(timeout=5.0)
        report_progress({"phase": "gathering", "completed": 3, "total": 3})
        return {"done": True}

    job_id = job_tracker.start_job(_slow_with_progress)
    started.wait(timeout=5.0)

    # Visible WHILE running -- the whole point, since get_job() only exposes
    # the final result once status flips to done.
    mid_run = job_tracker.get_job(job_id)
    assert mid_run["status"] == "running"
    assert mid_run["progress"] == {"phase": "gathering", "completed": 0, "total": 3}

    release.set()
    job = _wait_until_not_running(job_id)
    # Progress is cleared once done -- the final result speaks for itself.
    assert job == {"status": "done", "result": {"done": True}, "error": None, "progress": None}


def test_two_jobs_are_tracked_independently():
    job_id_1 = job_tracker.start_job(lambda: {"which": 1})
    job_id_2 = job_tracker.start_job(lambda: {"which": 2})

    assert _wait_until_not_running(job_id_1)["result"] == {"which": 1}
    assert _wait_until_not_running(job_id_2)["result"] == {"which": 2}
