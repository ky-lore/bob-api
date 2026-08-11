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
    assert job == {"status": "done", "result": {"answer": 42}, "error": None}


def test_start_job_records_a_raised_exception_instead_of_crashing():
    def _boom():
        raise RuntimeError("real failure")

    job_id = job_tracker.start_job(_boom)

    job = _wait_until_not_running(job_id)
    assert job["status"] == "error"
    assert job["result"] is None
    assert "real failure" in job["error"]


def test_two_jobs_are_tracked_independently():
    job_id_1 = job_tracker.start_job(lambda: {"which": 1})
    job_id_2 = job_tracker.start_job(lambda: {"which": 2})

    assert _wait_until_not_running(job_id_1)["result"] == {"which": 1}
    assert _wait_until_not_running(job_id_2)["result"] == {"which": 2}
