from playwright.sync_api import Page, expect


def test_login_and_chat(page: Page) -> None:
    page.goto("http://localhost:3000/login")
    page.get_by_role("button", name="Reject optional").click()
    page.get_by_placeholder("name@company.com").fill("admin@example.com")
    page.get_by_placeholder("name@company.com").fill("admin@example.com")
    page.locator("#password").fill("admin123")
    page.get_by_role("button", name="Login").click()
    page.get_by_role("button", name="Login").click()
    expect(page.get_by_text("New chat")).to_be_visible(timeout=15000)
    page.goto("http://localhost:3000/chat")
    page.get_by_placeholder("Type a message...").fill("What is 21 plus 21? Reply with only the number.")
    page.get_by_placeholder("Type a message...").press("Enter")
    expect(page.get_by_text("42")).to_be_visible(timeout=30000)
