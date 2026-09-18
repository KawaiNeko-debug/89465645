from h3.login_page import fill_password_login, is_mobile_account


def test_mobile_account_detection_only_matches_mainland_mobile_shape():
    assert is_mobile_account("13800138000")
    assert is_mobile_account(" 13800138000 ")
    assert not is_mobile_account("ABC12345")
    assert not is_mobile_account("23800138000")


class FakeLocator:
    def __init__(self, page, selector, index=0):
        self.page = page
        self.selector = selector
        self.index = index

    def count(self):
        return len(self.page.visible.get(self.selector, []))

    def nth(self, index):
        return FakeLocator(self.page, self.selector, index)

    def is_visible(self, timeout=0):
        return self.index < self.count()

    def fill(self, value):
        self.page.filled.append((self.selector, value))

    def get_attribute(self, name):
        if name == "type" and "checkbox" in self.selector:
            return "checkbox"
        return None

    def set_checked(self, checked, timeout=0):
        self.page.checked.append((self.selector, checked))

    def click(self, timeout=0):
        self.page.clicked.append(self.selector)
        if self.selector in self.page.reveal_password_on_click:
            self.page.visible.setdefault('input[type="password"]', [True])

    def press(self, key):
        self.page.pressed.append((self.selector, key))

    def locator(self, selector):
        if selector == "xpath=ancestor::form[1]":
            return FakeForm(self.page, self.selector)
        return FakeLocator(self.page, selector)


class FakeForm(FakeLocator):
    def __init__(self, page, field_selector):
        super().__init__(page, f"form-for:{field_selector}")
        self.field_selector = field_selector

    def count(self):
        return 1

    def locator(self, selector):
        scoped = f"{self.field_selector}::{selector}"
        return FakeLocator(self.page, scoped)


class FakePage:
    def __init__(self, visible):
        self.visible = {key: list(value) for key, value in visible.items()}
        self.reveal_password_on_click = set()
        self.filled = []
        self.checked = []
        self.clicked = []
        self.pressed = []

    def locator(self, selector):
        return FakeLocator(self, selector)

    def wait_for_timeout(self, timeout_ms):
        return None


def test_two_step_login_uses_semantic_password_selector():
    account = 'input[placeholder*="手机号"]'
    submit = f'{account}::button[type="submit"]'
    page = FakePage(
        {
            account: [True],
            submit: [True],
            '.consent-agreement input[type="checkbox"]': [True],
            'input[type="password"]::button[type="submit"]': [True],
        }
    )
    page.reveal_password_on_click.add(submit)

    fill_password_login(page, "account", "secret", lambda _message: None)

    assert (account, "account") in page.filled
    assert ('input[type="password"]', "secret") in page.filled
    assert submit in page.clicked
    assert 'input[type="password"]::button[type="submit"]' in page.clicked


def test_single_page_login_does_not_click_intermediate_submit():
    account = 'input[autocomplete="username"]'
    password = 'input[autocomplete="current-password"]'
    final_submit = f'{password}::button[type="submit"]'
    page = FakePage(
        {
            account: [True],
            password: [True],
            final_submit: [True],
        }
    )

    fill_password_login(page, "account", "secret", lambda _message: None)

    assert page.clicked == [final_submit]
    assert (password, "secret") in page.filled


def test_switches_from_default_login_mode_before_submitting_account():
    account = 'input[placeholder*="手机号"]'
    switch = 'text=/账号密码登录|密码登录/'
    final_submit = 'input[type="password"]::button[type="submit"]'
    page = FakePage(
        {
            account: [True],
            switch: [True],
            final_submit: [True],
        }
    )
    page.reveal_password_on_click.add(switch)

    fill_password_login(page, "account", "secret", lambda _message: None)

    assert switch in page.clicked
    assert final_submit in page.clicked
    assert ('input[type="password"]', "secret") in page.filled


def test_login_fails_with_clear_error_when_password_field_never_appears():
    account = 'input[name="account"]'
    submit = f'{account}::button[type="submit"]'
    page = FakePage({account: [True], submit: [True]})

    try:
        fill_password_login(
            page,
            "account",
            "secret",
            lambda _message: None,
            password_timeout_ms=0,
        )
    except RuntimeError as exc:
        assert "未找到密码输入框" in str(exc)
    else:
        raise AssertionError("expected password selector failure")
