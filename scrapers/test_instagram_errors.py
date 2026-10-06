"""IG 錯誤分類與冷卻的自我檢查。全用假例外，不會對 IG 發任何請求。
執行：python -m scrapers.test_instagram_errors"""
import instaloader
import instaloader.exceptions as E

from scrapers import instagram as ig

ig._BACKOFF_FILE = ig.ROOT / ".cache" / "_test_ig_backoff.json"
ig._ensure_login = lambda loader, user: None
ig._cooldown_ok = lambda username: 0


class _Ctx:
    is_logged_in = True


class _Loader:
    context = _Ctx()
    login_ok = None

    def test_login(self):
        return self.login_ok


_loader = _Loader()
ig._make_loader = lambda: _loader


def _run(exc, login_ok=None, logged_in=True):
    """回傳 (錯誤訊息, 是否進入冷卻)。"""
    ig._BACKOFF_FILE.unlink(missing_ok=True)
    _loader.login_ok, _Ctx.is_logged_in = login_ok, logged_in

    def boom(*a):
        raise exc
    instaloader.Profile.from_username = boom
    try:
        ig.fetch("shaoqicaifuxinzhouqi", 3)
    except SystemExit as e:
        return str(e), ig.backoff_remaining() > 0
    raise AssertionError("fetch 應該結束並說明原因")


if __name__ == "__main__":
    # 限流、403、checkpoint：都要進冷卻
    assert _run(E.ConnectionException('401 Unauthorized "Please wait a few minutes"'))[1]
    assert _run(E.QueryReturnedForbiddenException("403 Forbidden"))[1]
    assert _run(E.ConnectionException("checkpoint_required"))[1]
    # 回「不存在」但登入驗證失敗：其實是被限制，不能說帳號不存在
    msg, cooled = _run(E.ProfileNotExistsException("x"), login_ok=None)
    assert cooled and "不代表" in msg
    # 登入正常才相信「不存在」
    msg, cooled = _run(E.ProfileNotExistsException("x"), login_ok="me")
    assert not cooled and "找不到帳號" in msg
    # 未登入、單純網路錯誤：不進冷卻
    assert not _run(E.ProfileNotExistsException("x"), logged_in=False)[1]
    assert not _run(E.ConnectionException("Max retries exceeded"))[1]
    # 冷卻中：不發任何請求就擋下
    ig._set_backoff("test")
    instaloader.Profile.from_username = lambda *a: (_ for _ in ()).throw(AssertionError("冷卻中不該發請求"))
    try:
        ig.fetch("shaoqicaifuxinzhouqi", 3)
    except SystemExit as e:
        assert "30 分鐘後" in str(e)
    ig._BACKOFF_FILE.unlink()
    print("ok")
