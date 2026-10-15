"""The sales dashboard's access-password gate (app/sales_access.py + the login routes). The password here is a TEST value;
the real one lives only in the Railway env."""
import json
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.routers.sales_audit as router_mod
from app import sales_access as acc
from app.config import get_settings
from app.db import Base, get_db
from app.models import SalesAuditRun
from app.sales_access import SalesAccessMiddleware
from app.sales_host import SalesHostMiddleware

PW = "test-gate-pw-123"
SALES = "sales.advancedmarketers.co"
HDR = {"x-forwarded-for": "203.0.113.9"}


def _build(tmp_path, monkeypatch, password):
    if password is None:
        monkeypatch.delenv("SALES_DASHBOARD_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("SALES_DASHBOARD_PASSWORD", password)
    monkeypatch.setenv("SALES_DASHBOARD_HOST", SALES)
    get_settings.cache_clear()
    acc.throttle.reset_all()
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(bind=engine)
    sf = sessionmaker(bind=engine)
    with sf() as db:
        db.add(SalesAuditRun(run_at=datetime(2026, 10, 15, 12), report_json=json.dumps(
            {"generated_at": "2026-10-15T12:00:00+00:00", "count": 0, "rows": [], "summary": {}, "source_errors": {}, "rules": {}})))
        db.commit()
    app = FastAPI()
    app.add_middleware(SalesAccessMiddleware)       # same order as main.py: gate first,
    app.add_middleware(SalesHostMiddleware)         # host rewrite outermost
    app.include_router(router_mod.router)
    app.dependency_overrides[get_db] = lambda: sf()

    @app.get("/reports/atlas-account-status/pulse")
    def pulse():
        return {"pulse": True}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return TestClient(app, follow_redirects=False)


@pytest.fixture
def gated(tmp_path, monkeypatch):
    yield _build(tmp_path, monkeypatch, PW)
    get_settings.cache_clear()


def _login(c, password=PW, nxt="", headers=None):
    return c.post("/reports/sales-audit/login", content=f"password={password}&next={nxt}", headers={"content-type": "application/x-www-form-urlencoded", **HDR, **(headers or {})})


# ---------------------------------------------------------------------------------------------- off / on

def test_gate_is_off_when_no_password_is_configured(tmp_path, monkeypatch):
    c = _build(tmp_path, monkeypatch, None)
    assert c.get("/reports/sales-audit").status_code == 200 and c.get("/reports/sales-audit/latest").status_code == 200
    assert c.get("/reports/sales-audit/login").status_code == 303        # nothing to log in to -> straight to the dashboard
    assert "Sign out" not in c.get("/reports/sales-audit").text            # no sign-out button when there's no gate


def test_unauthenticated_page_redirects_to_login_remembering_where_you_were_going(gated):
    r = gated.get("/reports/sales-audit/view/1")
    assert r.status_code == 303 and r.headers["location"] == "/reports/sales-audit/login?next=%2Freports%2Fsales-audit%2Fview%2F1"
    assert gated.get("/reports/sales-audit").headers["location"].startswith("/reports/sales-audit/login")


def test_unauthenticated_api_calls_get_401_json_not_a_redirect(gated):
    for method, path in (("get", "/reports/sales-audit/latest"), ("post", "/reports/sales-audit/run"), ("get", "/reports/sales-audit/run/abc"),
                         ("post", "/reports/sales-audit/overrides"), ("post", "/reports/sales-audit/push"), ("delete", "/reports/sales-audit/overrides/atlas:1")):
        r = getattr(gated, method)(path)
        assert r.status_code == 401 and r.json() == {"detail": "access password required"}, (method, path)


def test_login_logout_and_brand_images_stay_reachable_and_other_routes_are_untouched(gated):
    assert gated.get("/reports/sales-audit/login").status_code == 200
    assert gated.get("/reports/sales-audit/assets/wordmark-black.png").status_code == 200       # the login page shows the wordmark
    assert gated.get("/health").json() == {"status": "ok"} and gated.get("/reports/atlas-account-status/pulse").json() == {"pulse": True}


# ---------------------------------------------------------------------------------------------- logging in

def test_wrong_password_is_rejected_with_a_message_and_no_cookie(gated):
    r = _login(gated, "nope")
    assert r.status_code == 401 and "Incorrect password" in r.text and "set-cookie" not in r.headers
    assert gated.get("/reports/sales-audit").status_code == 303


def test_correct_password_sets_a_hardened_cookie_that_unlocks_everything(gated):
    r = _login(gated, nxt="/reports/sales-audit/view/1")
    assert r.status_code == 303 and r.headers["location"] == "/reports/sales-audit/view/1"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=2592000" in cookie and "path=/" in cookie
    assert PW not in r.headers["set-cookie"]                                                     # the cookie never contains the password
    assert gated.get("/reports/sales-audit").status_code == 200 and gated.get("/reports/sales-audit/latest").status_code == 200
    assert "Sign out" in gated.get("/reports/sales-audit").text


def test_cookie_is_marked_secure_only_over_https(gated):
    assert "secure" not in _login(gated).headers["set-cookie"].lower()
    assert "secure" in _login(gated, headers={"x-forwarded-proto": "https"}).headers["set-cookie"].lower()


def test_the_password_never_appears_in_any_page(gated):
    _login(gated)
    for path in ("/reports/sales-audit", "/reports/sales-audit/login", "/reports/sales-audit/latest"):
        assert PW not in gated.get(path).text


def test_logout_clears_the_session(gated):
    _login(gated)
    assert gated.get("/reports/sales-audit").status_code == 200
    r = gated.post("/reports/sales-audit/logout")
    assert r.status_code == 303 and r.headers["location"] == "/reports/sales-audit/login"
    gated.cookies.clear()
    assert gated.get("/reports/sales-audit").status_code == 303


# ---------------------------------------------------------------------------------------------- tokens

def test_forged_tampered_expired_and_other_password_tokens_are_all_rejected(gated):
    good = acc.make_token(PW, now=1_000_000)
    assert acc.valid_token(good, PW, now=1_000_100)
    exp, _, sig = good.partition(".")
    assert not acc.valid_token(f"{int(exp) + 999}.{sig}", PW, now=1_000_100)                     # changed expiry, same signature
    assert not acc.valid_token(f"{exp}.{'0' * 64}", PW, now=1_000_100)                            # forged signature
    assert not acc.valid_token(good, PW, now=1_000_000 + acc.SESSION_SECONDS + 1)                 # expired
    assert not acc.valid_token(good, "a-different-password", now=1_000_100)                       # password changed -> sessions die
    assert not acc.valid_token(None, PW) and not acc.valid_token("garbage", PW) and not acc.valid_token("x.y", PW)
    gated.cookies.set(acc.COOKIE, f"{exp}.{'f' * 64}")
    assert gated.get("/reports/sales-audit").status_code == 303


def test_script_clients_can_use_the_password_header(gated):
    assert gated.get("/reports/sales-audit/latest", headers={"x-sales-password": PW}).status_code == 200
    assert gated.get("/reports/sales-audit/latest", headers={"x-sales-password": "wrong"}).status_code == 401


# ---------------------------------------------------------------------------------------------- brute force

def test_repeated_failures_are_rate_limited_per_ip_even_for_the_right_password(gated):
    for _ in range(acc.MAX_FAILURES):
        assert _login(gated, "nope").status_code == 401
    blocked = _login(gated, PW)
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) > 0 and "Too many attempts" in blocked.text
    other = _login(gated, PW, headers={"x-forwarded-for": "198.51.100.7"})                         # a different client is unaffected
    assert other.status_code == 303


def test_failures_expire_and_success_resets_the_counter():
    t = acc._Throttle()
    for i in range(acc.MAX_FAILURES):
        t.record_failure("ip", now=1000 + i)
    assert t.retry_after("ip", now=1100) > 0
    assert t.retry_after("ip", now=1000 + acc.FAILURE_WINDOW + 60) == 0
    t.clear("ip")
    assert t.retry_after("ip", now=1100) == 0


# ---------------------------------------------------------------------------------------------- redirects

@pytest.mark.parametrize("evil", ["https://evil.example", "//evil.example", "/other/path", "/reports/sales-audit//evil", "javascript:alert(1)", '/reports/sales-audit"><script>', "", None])
def test_next_can_only_point_back_into_the_dashboard(evil):
    assert acc.safe_next(evil) == "/reports/sales-audit"


def test_next_allows_real_dashboard_urls():
    for ok in ("/reports/sales-audit", "/reports/sales-audit/view/12", "/reports/sales-audit?x=1", "/reports/sales-audit/view/3#%7B%22screen%22%3A%22deals%22%7D"):
        assert acc.safe_next(ok) == ok


def test_login_page_does_not_reflect_an_unsafe_next(gated):
    html = gated.get("/reports/sales-audit/login", params={"next": '"><script>alert(1)</script>'}).text
    assert "<script>alert(1)" not in html and 'value="/reports/sales-audit"' in html


# ---------------------------------------------------------------------------------------------- the sales hostname

def test_the_sales_hostname_root_is_gated_then_serves_the_dashboard(gated):
    r = gated.get("/", headers={"host": SALES})
    assert r.status_code == 303 and r.headers["location"].startswith("/reports/sales-audit/login")
    _login(gated, headers={"host": SALES})
    assert "Sales Audit" in gated.get("/", headers={"host": SALES}).text
