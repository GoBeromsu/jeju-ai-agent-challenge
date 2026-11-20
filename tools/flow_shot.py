# 한 대화로 완주: 문장 → 역질문 → 후보 → 2D 핀 → 3D 점포 → 정류장 가정 보행
import sys, json
from playwright.sync_api import sync_playwright
out, w, h = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
steps = []
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=False, args=["--window-position=-3000,0"])
    pg = b.new_page(viewport={"width": w, "height": h})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:8765/?debug"); pg.wait_for_selector("#loading.done", state="attached", timeout=30000); pg.add_style_tag(content="#metrics{display:none!important}"); pg.wait_for_timeout(800)
    def shot(n, note):
        pg.screenshot(path=f"{out}/{n}.png"); steps.append({"shot": n, "note": note, "message": pg.inner_text("#message")[:160], "work": pg.inner_text("#workLog") if pg.is_visible("#workLog") else ""})
    shot("1-home", "첫 화면")
    pg.fill("#text", "제주에 작은 카페를 열고 싶어요"); pg.press("#text", "Enter"); pg.wait_for_function("!document.getElementById('message').innerText.includes('살펴보는 중')", timeout=30000); pg.wait_for_timeout(1500)
    shot("2-question", "역질문")
    pg.locator("#chips button", has_text="아직 모름").first.click(); pg.wait_for_timeout(3000)
    shot("3-compare", "아직 모름 비교")
    pg.locator("#chips button", has_text="B처럼 찾아오는").first.click(); pg.wait_for_timeout(3500)
    shot("4-candidates", "후보 A/B/C")
    pin = pg.locator(".label.pin", has_text="B ").first
    steps.append({"pinText": pin.inner_text()})
    try:
        pin.click(timeout=4000); steps.append({"via": "지도 핀"})
    except Exception:
        pg.locator("#chips button", has_text="B ").first.click(); steps.append({"via": "후보 버튼(핀이 시트에 가려짐)"})
    pg.wait_for_timeout(4000)
    shot("5-pin-dong", "2D 핀으로 후보 B 선택")
    # 표선면으로 옮겨 점포와 정류장 보행
    pg.evaluate("window.__state ? 0 : 0")
    pg.fill("#text", "표선면 보여 줘") if pg.is_visible("#text") else None
    if pg.is_visible("#text"):
        pg.press("#text", "Enter")
    else:
        pg.evaluate("document.querySelector('#cardClose').click()"); pg.wait_for_timeout(300); pg.fill("#text", "표선면 보여 줘"); pg.press("#text", "Enter")
    pg.wait_for_function("document.getElementById('message').innerText.includes('표선면(')", timeout=40000); pg.wait_for_timeout(4000)
    shot("6-pyoseon", "표선면 3D")
    if pg.is_visible("#seeStores"):
        pg.click("#seeStores")
    else:
        pg.evaluate("document.getElementById('seeStores') && document.getElementById('seeStores').click()")
    pg.wait_for_timeout(2500)
    st = pg.evaluate("__storeScreen('same')"); steps.append({"store": st})
    if not st:
        steps.append({"store": "화면 안에 고를 점포 점이 없음 → 여기서 멈춤"}); print(json.dumps({"steps": steps, "errors": errs}, ensure_ascii=False)); b.close(); sys.exit(0)
    pg.mouse.click(st["x"], st["y"]); pg.wait_for_timeout(2000)
    shot("7-store", "3D 점포 선택")
    if pg.is_visible("#chips button"):
        pg.locator("#chips button", has_text="여기 왜").first.click(); pg.wait_for_timeout(3000)
    shot("8-why", "여기 왜")
    pg.evaluate("document.getElementById('walkBtn').click()"); pg.wait_for_timeout(3500)
    shot("9-walk", "정류장에서 목적지까지 가정 보행")
    opt = pg.locator("#cardBody .opts .opt:not(.on)").first
    if opt.count():
        opt.click(); pg.wait_for_timeout(3000); shot("10-walk-dest", "목적지 바꾸기")
    print(json.dumps({"steps": steps, "errors": errs}, ensure_ascii=False))
    b.close()
