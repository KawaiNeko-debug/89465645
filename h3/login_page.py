import re
import time
from collections.abc import Callable, Iterable


ACCOUNT_INPUT_SELECTORS = (
    'input[autocomplete="username"]',
    'input[name="username"]',
    'input[name="account"]',
    'input[placeholder*="手机号码"]',
    'input[placeholder*="手机号"]',
    'input[placeholder*="邮箱"]',
    'input[placeholder*="账号"]',
    'input[placeholder*="客编"]',
)

PASSWORD_INPUT_SELECTORS = (
    'input[type="password"]',
    'input[autocomplete="current-password"]',
    'input[name="password"]',
    'input[placeholder*="登录密码"]',
    'input[placeholder*="密码"]',
)

AGREEMENT_SELECTORS = (
    '.consent-agreement input[type="checkbox"]',
    'label:has-text("同意") input[type="checkbox"]',
    'input[type="checkbox"]',
    '.consent-agreement img:last-child',
    '#__layout .consent-agreement img:nth-child(2)',
)

PASSWORD_LOGIN_SWITCH_SELECTORS = (
    'text=/账号密码登录|密码登录/',
    '[role="tab"]:has-text("密码")',
    'button:has-text("密码登录")',
)

SUBMIT_BUTTON_SELECTORS = (
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("下一步")',
    'button:has-text("继续")',
    'button:has-text("登录")',
    '[role="button"]:has-text("下一步")',
    '[role="button"]:has-text("登录")',
)


def is_mobile_account(username: str) -> bool:
    return bool(re.fullmatch(r"1\d{10}", str(username or "").strip()))


def _first_visible(page, selectors: Iterable[str]):
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = locator.count()
        except Exception:
            continue
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible(timeout=250):
                    return candidate, selector
            except Exception:
                continue
    return None, ""


def wait_for_visible(page, selectors: Iterable[str], timeout_ms: int):
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while True:
        locator, selector = _first_visible(page, selectors)
        if locator is not None:
            return locator, selector
        if time.monotonic() >= deadline:
            return None, ""
        page.wait_for_timeout(200)


def _form_submit_button(field):
    try:
        form = field.locator("xpath=ancestor::form[1]")
        if form.count() == 0:
            return None, ""
        return _first_visible(form, SUBMIT_BUTTON_SELECTORS)
    except Exception:
        return None, ""


def _click_submit(page, field) -> str:
    button, selector = _form_submit_button(field)
    if button is None:
        button, selector = _first_visible(page, SUBMIT_BUTTON_SELECTORS)
    if button is None:
        try:
            field.press("Enter")
            return "Enter"
        except Exception as exc:
            raise RuntimeError("未找到可用的登录按钮") from exc
    button.click(timeout=5000)
    return selector


def _accept_agreement(page) -> bool:
    agreement, _ = _first_visible(page, AGREEMENT_SELECTORS)
    if agreement is None:
        return False
    try:
        if agreement.get_attribute("type") == "checkbox":
            agreement.set_checked(True, timeout=5000)
        else:
            agreement.click(timeout=5000)
        return True
    except Exception:
        return False


def _switch_to_password_login(page):
    switch, selector = _first_visible(page, PASSWORD_LOGIN_SWITCH_SELECTORS)
    if switch is None:
        return ""
    try:
        switch.click(timeout=5000)
        return selector
    except Exception:
        return ""


def fill_password_login(
    page,
    username: str,
    password: str,
    log: Callable[[str], None],
    account_timeout_ms: int = 30000,
    password_timeout_ms: int = 15000,
) -> None:
    """Fill and submit the mobile password-login form without fixed DOM paths."""

    account, account_selector = wait_for_visible(
        page, ACCOUNT_INPUT_SELECTORS, account_timeout_ms
    )
    password_field, password_selector = _first_visible(page, PASSWORD_INPUT_SELECTORS)

    if account is None and password_field is None:
        if _switch_to_password_login(page):
            account, account_selector = wait_for_visible(
                page, ACCOUNT_INPUT_SELECTORS, 5000
            )
            password_field, password_selector = _first_visible(
                page, PASSWORD_INPUT_SELECTORS
            )

    if account is None:
        raise RuntimeError("登录页未找到账号输入框")

    log(f"✅ 登录页加载完成（账号框: {account_selector}）")
    account.fill(username)
    log("✅ 已填写账号")

    if password_field is None:
        switch_selector = _switch_to_password_login(page)
        if switch_selector:
            log(f"✅ 已切换密码登录模式（控件: {switch_selector}）")
            password_field, password_selector = wait_for_visible(
                page, PASSWORD_INPUT_SELECTORS, 5000
            )

    if _accept_agreement(page):
        log("✅ 已确认登录协议")
    else:
        log("ℹ️ 登录页未显示可操作的协议控件")

    if password_field is None:
        submit_selector = _click_submit(page, account)
        log(f"✅ 已进入密码步骤（按钮: {submit_selector}）")
        password_field, password_selector = wait_for_visible(
            page, PASSWORD_INPUT_SELECTORS, password_timeout_ms
        )

    if password_field is None:
        if _switch_to_password_login(page):
            password_field, password_selector = wait_for_visible(
                page, PASSWORD_INPUT_SELECTORS, 5000
            )

    if password_field is None:
        raise RuntimeError("登录页未找到密码输入框，页面结构可能已更新")

    log(f"✅ 密码框已出现（定位: {password_selector}）")
    password_field.fill(password)
    log("✅ 已填写密码")

    submit_selector = _click_submit(page, password_field)
    log(f"✅ 已提交登录（按钮: {submit_selector}）")
