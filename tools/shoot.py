"""브라우저로 앱을 열어 P1·P2 시나리오를 칩으로 밟고 단계별 스크린샷과 지표를 남긴다.

    uv run --with playwright python tools/shoot.py [--out shots/v0]\n\n서버를 스스로 띄운다.

설치된 Chrome 을 쓴다 (playwright 브라우저 다운로드 불필요).
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.server import make_server  # noqa: E402

SCENARIOS = {
    "p1": ["제주시 동 지역에서 반찬가게를 하고 싶어요", "매일 들르는 단골", "@enter"],
    "p2": ["서귀포 읍면에서 작은 카페 하고 싶어요", "아직 모름", "@enter"],
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="shots/latest")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--llm", action="store_true", help="자유 문장을 실제 Gemini 로 (기본: 끔, 결정적 촬영)")
    args = ap.parse_args()
    server = make_server(port=args.port, use_llm=args.llm)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{args.port}/index.html?debug"
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report: dict = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", args=["--use-angle=metal", "--enable-gpu"])
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))
        page.goto(url)
        page.wait_for_selector("#loading.done", state="attached", timeout=30000)
        page.wait_for_timeout(800)
        page.screenshot(path=str(out / "00-island.png"))
        report["metrics"] = page.evaluate("({...window.__metrics})")
        for name, steps in SCENARIOS.items():
            page.click("#home")
            page.fill("#text", "처음부터")
            page.press("#text", "Enter")
            page.wait_for_timeout(2600)
            for i, step in enumerate(steps):
                if i == 0:
                    page.fill("#text", step)
                    page.press("#text", "Enter")
                elif step == "@enter":
                    page.locator("#cardBody button[data-letter='A']").first.click()
                else:
                    page.locator("#chips button", has_text=step).first.click()
                page.wait_for_timeout(2700)
                page.screenshot(path=str(out / f"{name}-{i + 1}.png"))
            report[name] = {
                "message": page.inner_text("#message"),
                "card": page.inner_text("#cardBody") if page.is_visible("#card") else None,
            }
        # 동네 단계에서 3초 돌리며 프레임 (카드 열린 상태)
        report["fpsDong"] = page.evaluate("window.__orbitFps(3000)")
        page.click("#home")
        page.wait_for_timeout(2600)
        report["fpsIsland"] = page.evaluate("window.__orbitFps(3000)")
        report["errors"] = errors
        browser.close()
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
