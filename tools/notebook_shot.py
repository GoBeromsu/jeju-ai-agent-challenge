"""Exercise the approved consultation, map, site-radius, and population UI at /.

    python3 tools/notebook_shot.py --browser chrome --out shots/release-chrome
    python3 tools/notebook_shot.py --browser msedge --out shots/release-edge

Starts the source server in offline mode on a free loopback port. It follows
visible A/B choices, submits the B switch through the composer, zooms the map,
selects the real 조천읍 survey point, and checks actual 300/500m controls and the
population response. Missing controls are reported as blocked rather than
replaced with direct API calls. The report does not prove the clean-Windows ZIP
or double-click gate; that must be performed and recorded separately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.server import make_server  # noqa: E402

PROMPT = "제주에 작은 카페를 열고 싶어요"
UNKNOWN_CHOICE = "아직 모름"
ANCHOR_PREFERENCE = "생활시설 주변"
SHARE_PREFERENCE = "동종 가게 비중 낮은 곳"
SHARE_REQUEST = "동종 가게 비중이 낮은 곳을 더 중요하게 봐 주세요."
SITE_DONG = "조천읍"
SITE_DONG_CODE = "50110259"
SITE_LAT_LON = (33.542, 126.669)
MAP_ORIGIN_LAT_LON = (33.38, 126.55)
EXPECTED_SITE_MAP_POINT = (11061.841238433644, -18033.84)
EXPECTED_STORE_DATE = "2025-09-18"
EXPECTED_SITE_COUNTS = {300: (12, 130), 500: (22, 217)}
SCREENSHOTS = (
    "00-home.png",
    "01-region-question.png",
    "02-preference-question.png",
    "03-a-results.png",
    "04-a-card-rank-2.png",
    "05-a-pin-rank-1.png",
    "06-a-zoom.png",
    "07-b-results.png",
    "08-b-card-rank-2.png",
    "09-b-zoom.png",
    "10-chochoon.png",
    "11-site-point-zoomed.png",
    "12-site-300m.png",
    "13-site-500m.png",
    "14-population-inquiry.png",
)


class ScenarioBlocked(AssertionError):
    """The real UI does not expose an approved interaction without a workaround."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def data_receipt() -> dict:
    data_root = ROOT / "web" / "data"
    meta = json.loads((data_root / "meta.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256()
    files = sorted(path for path in data_root.rglob("*") if path.is_file())
    for path in files:
        relative = path.relative_to(data_root).as_posix().encode("utf-8")
        digest.update(relative + b"\0" + bytes.fromhex(sha256(path)))
    return {
        "store_date": meta.get("storeDate"),
        "files": len(files),
        "population_snapshot_included": (data_root / "population_snapshot.json").is_file(),
        "sha256": digest.hexdigest(),
    }


def visible_choice_buttons(page) -> list[tuple[object, str]]:
    buttons = page.locator("#options").get_by_role("button")
    choices = []
    for index in range(buttons.count()):
        button = buttons.nth(index)
        if not button.is_visible():
            continue
        spans = button.locator("span")
        label = spans.nth(spans.count() - 1).inner_text() if spans.count() >= 2 else button.inner_text()
        if spans.count() < 2:
            label = re.sub(r"^\s*\d+\s*", "", label)
        choices.append((button, " ".join(label.split())))
    return choices


def wait_for_choices(page, timeout_ms: int) -> list[tuple[object, str]]:
    page.locator("#options").get_by_role("button").first.wait_for(state="visible", timeout=timeout_ms)
    choices = visible_choice_buttons(page)
    if not choices:
        raise AssertionError("상담 선택지가 화면에 표시되지 않았습니다")
    return choices


def latest_candidate_article(page):
    articles = page.locator("#feed article")
    for index in range(articles.count() - 1, -1, -1):
        article = articles.nth(index)
        if article.locator(".candidate-card").count():
            return article
    raise AssertionError("상담 답변에 후보 카드가 없습니다")


def read_controls(locator, surface: str) -> list[dict]:
    rows = []
    for index in range(locator.count()):
        control = locator.nth(index)
        if not control.is_visible():
            raise AssertionError(f"{surface} {index + 1}번 후보가 화면에서 보이지 않습니다")
        candidate_id = control.get_attribute("data-candidate-id")
        rank = control.get_attribute("data-rank")
        pressed = control.get_attribute("aria-pressed")
        if not candidate_id or rank is None or pressed not in {"true", "false"}:
            raise AssertionError(f"{surface} 후보에 ID·순번·aria-pressed가 없습니다")
        text = " ".join(control.inner_text().split())
        if not text:
            raise AssertionError(f"{surface} 후보에 화면 이름이 없습니다")
        name = control.locator(".candidate-name").inner_text() if control.locator(".candidate-name").count() else ""
        reason = control.locator(".candidate-reason").inner_text() if control.locator(".candidate-reason").count() else ""
        caution = control.locator(".candidate-caution").inner_text() if control.locator(".candidate-caution").count() else ""
        metadata = control.locator(".candidate-metadata").inner_text() if control.locator(".candidate-metadata").count() else ""
        rows.append({
            "id": candidate_id,
            "rank": int(rank),
            "pressed": pressed,
            "text": text,
            "name": " ".join(name.split()),
            "reason": " ".join(reason.split()),
            "caution": " ".join(caution.split()),
            "metadata": " ".join(metadata.split()),
        })
    return rows


def marker_name(text: str) -> str:
    name = re.sub(r"^\s*(?:[A-C]|[1-3](?:위)?)\s*[·.):\-]?\s*", "", text)
    return " ".join(name.split())


def selection_state(page, cards, candidate_id: str) -> bool:
    card_rows = read_controls(cards, "카드")
    pin_rows = read_controls(page.locator(".candidate-marker"), "지도 핀")
    card_active = [row["id"] for row in card_rows if row["pressed"] == "true"]
    pin_active = [row["id"] for row in pin_rows if row["pressed"] == "true"]
    return card_active == [candidate_id] and pin_active == [candidate_id]


def wait_for_selection(page, cards, candidate_id: str, timeout_ms: int) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if selection_state(page, cards, candidate_id):
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"카드와 지도 핀이 같은 후보를 강조하지 않습니다: {candidate_id}")


def wait_for_candidate_answer(page, timeout_ms: int):
    page.wait_for_function(
        """() => !document.querySelector('#submit').disabled
          && [...document.querySelectorAll('#feed article')].at(-1)
            ?.querySelectorAll('.candidate-card').length >= 2""",
        timeout=timeout_ms,
    )
    return latest_candidate_article(page)


def current_candidate_snapshot(page) -> tuple[list[dict], list[dict]]:
    article = latest_candidate_article(page)
    cards = read_controls(article.locator(".candidate-card"), "후보 카드")
    pins = read_controls(page.locator(".candidate-marker"), "지도 핀")
    if not 2 <= len(cards) <= 3 or len(pins) != len(cards):
        raise AssertionError(f"카드·지도에 같은 2~3개 후보가 있어야 합니다: cards={len(cards)}, pins={len(pins)}")
    ranks = list(range(1, len(cards) + 1))
    if [row["rank"] for row in cards] != ranks or [row["rank"] for row in pins] != ranks:
        raise AssertionError("후보 카드와 지도 핀 순번이 1부터 같은 순서로 표시되지 않습니다")
    if [(row["id"], row["rank"]) for row in cards] != [(row["id"], row["rank"]) for row in pins]:
        raise AssertionError("후보 카드와 지도 핀의 읍면동 ID·순번이 다릅니다")
    for card, pin in zip(cards, pins):
        name = marker_name(pin["text"])
        if not name or name not in card["text"]:
            raise AssertionError(f"지도 이름과 카드 이름이 일치하지 않습니다: pin={pin['text']!r}, card={card['text']!r}")
    return cards, pins


def candidate_details(rows: list[dict]) -> list[tuple]:
    return [
        (row["id"], row["rank"], row["name"], row["reason"], row["caution"], row["metadata"])
        for row in rows
    ]


def map_projection() -> tuple[float, float]:
    lat, lon = SITE_LAT_LON
    origin_lat, origin_lon = MAP_ORIGIN_LAT_LON
    x = (lon - origin_lon) * 111_320 * math.cos(math.radians(origin_lat))
    z = -(lat - origin_lat) * 111_320
    if not math.isclose(x, EXPECTED_SITE_MAP_POINT[0], abs_tol=1e-6) or not math.isclose(
        z, EXPECTED_SITE_MAP_POINT[1], abs_tol=1e-6
    ):
        raise AssertionError(f"WGS84 지점의 지도 투영이 승인된 검증 좌표와 다릅니다: {(x, z)}")
    return x, z


def dong_store_center(dong_code: str) -> tuple[float, float]:
    dongs = json.loads((ROOT / "web" / "data" / "dongs.json").read_text(encoding="utf-8"))
    dong = next((item for item in dongs if item["code"] == dong_code), None)
    if dong is None or not isinstance(dong.get("storeCenter"), list) or len(dong["storeCenter"]) != 2:
        raise AssertionError(f"배포 동네 자료에서 {dong_code} 점포 중심을 찾지 못했습니다")
    return float(dong["storeCenter"][0]), float(dong["storeCenter"][1])


def map_point_geometry(page, x: float, z: float, dong_name: str) -> dict:
    return page.locator("#map").evaluate(
        """(map, {x, z, dongName}) => {
          const point = new DOMPoint(x, z);
          const ctm = map.getScreenCTM();
          const screen = point.matrixTransform(ctm);
          const rect = map.getBoundingClientRect();
          const path = [...map.querySelectorAll('path.district')]
            .find(item => item.getAttribute('aria-label') === `${dongName} 선택`);
          const hit = document.elementFromPoint(screen.x, screen.y);
          const pin = hit?.closest?.('.candidate-marker');
          const pinRect = pin?.getBoundingClientRect();
          const inside = Boolean(pinRect && screen.x >= pinRect.left && screen.x <= pinRect.right
            && screen.y >= pinRect.top && screen.y <= pinRect.bottom);
          return {
            mapPoint: {x, z},
            screenPoint: {x: screen.x, y: screen.y},
            mapRect: {left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom,
              width: rect.width, height: rect.height},
            ctm: {a: ctm.a, b: ctm.b, c: ctm.c, d: ctm.d, e: ctm.e, f: ctm.f},
            pixelsPerMeter: Math.min(Math.abs(ctm.a), Math.abs(ctm.d)),
            metersPerCssPixel: 1 / Math.min(Math.abs(ctm.a), Math.abs(ctm.d)),
            viewBoxWidth: map.viewBox.baseVal.width,
            inViewport: screen.x >= rect.left && screen.x <= rect.right
              && screen.y >= rect.top && screen.y <= rect.bottom,
            inDistrict: Boolean(path?.isPointInFill(point)),
            hitTarget: hit ? {tag: hit.tagName.toLowerCase(), id: hit.id,
              className: typeof hit.className === 'string' ? hit.className : hit.getAttribute('class'),
              ariaLabel: hit.getAttribute('aria-label')} : null,
            hitIsMapChild: Boolean(hit && map.contains(hit)),
            focusPin: pin ? {text: pin.innerText, rect: {left: pinRect.left, top: pinRect.top,
              right: pinRect.right, bottom: pinRect.bottom, width: pinRect.width, height: pinRect.height}}
              : null,
            pinCoversPoint: inside,
            scroll: {windowX: scrollX, windowY: scrollY}
          };
        }""",
        {"x": x, "z": z, "dongName": dong_name},
    )


def nearest_map_click_pixel(page, x: float, z: float, dong_name: str, tolerance_m: float = 2) -> dict | None:
    return page.locator("#map").evaluate(
        """(map, {x, z, dongName, toleranceM}) => {
          const screen = new DOMPoint(x, z).matrixTransform(map.getScreenCTM());
          const rect = map.getBoundingClientRect();
          const path = [...map.querySelectorAll('path.district')]
            .find(item => item.getAttribute('aria-label') === `${dongName} 선택`);
          if (!path) return null;
          const nearest = [];
          for (let y = Math.floor(screen.y) - 3; y <= Math.ceil(screen.y) + 3; y++) {
            for (let px = Math.floor(screen.x) - 3; px <= Math.ceil(screen.x) + 3; px++) {
              if (px < rect.left || px > rect.right || y < rect.top || y > rect.bottom) continue;
              const hit = document.elementFromPoint(px, y);
              if (hit !== path) continue;
              const point = new DOMPoint(px, y).matrixTransform(map.getScreenCTM().inverse());
              const errorM = Math.hypot(point.x - x, point.y - z);
              if (errorM <= toleranceM) nearest.push({
                screenX: px, screenY: y, errorM,
                mapPoint: {x: point.x, z: point.y},
                hitTarget: {tag: hit.tagName.toLowerCase(), className: hit.getAttribute('class'),
                  ariaLabel: hit.getAttribute('aria-label')}
              });
            }
          }
          nearest.sort((a, b) => a.errorM - b.errorM || a.screenY - b.screenY || a.screenX - b.screenX);
          return nearest[0] || null;
        }""",
        {"x": x, "z": z, "dongName": dong_name, "toleranceM": tolerance_m},
    )


def zoom_to_clickable_map_point(page, x: float, z: float, dong_name: str, timeout_ms: int) -> tuple[dict, list[dict]]:
    """Use real wheel input away from the focus pin until click rounding is sub-meter."""
    measurements: list[dict] = []
    for step in range(1, 25):
        geometry = map_point_geometry(page, x, z, dong_name)
        click_pixel = nearest_map_click_pixel(page, x, z, dong_name)
        ready = (
            geometry["inViewport"]
            and geometry["inDistrict"]
            and geometry["metersPerCssPixel"] <= 2
            and click_pixel is not None
        )
        if ready:
            geometry["clickPixel"] = click_pixel
            return geometry, measurements

        target = geometry["screenPoint"]
        # The selected-dong pin is a sibling overlay above the SVG. Zooming with
        # the cursor on the actual point sends wheel events to that button instead
        # of #map. Place the real pointer beyond the measured pin bounds, while
        # keeping the target in the viewport and preserving it as the zoom focus.
        candidates = [
            (target["x"] + 150, target["y"]),
            (target["x"] - 150, target["y"]),
            (target["x"], target["y"] + 100),
            (target["x"], target["y"] - 100),
            (target["x"] + 220, target["y"]),
            (target["x"] - 220, target["y"]),
            (target["x"], target["y"] + 160),
            (target["x"], target["y"] - 160),
        ]
        anchor = page.locator("#map").evaluate(
            """(map, candidates) => {
              for (const [x, y] of candidates) {
                const rect = map.getBoundingClientRect();
                if (x < rect.left + 4 || x > rect.right - 4 || y < rect.top + 4 || y > rect.bottom - 4) continue;
                const hit = document.elementFromPoint(x, y);
                if (hit && map.contains(hit) && !hit.closest?.('.candidate-marker')) return {x, y};
              }
              return null;
            }""",
            candidates,
        )
        if anchor is None:
            measurements.append({"step": step, "target": geometry, "status": "blocked_no_safe_wheel_target"})
            raise ScenarioBlocked(
                f"{dong_name} 조사점의 위치를 가리는 지도 핀을 피하면서 지도 위 휠 위치를 찾지 못했습니다: {geometry}"
            )

        before = geometry["viewBoxWidth"]
        page.mouse.move(anchor["x"], anchor["y"])
        page.mouse.wheel(0, -120)
        try:
            page.wait_for_function(
                "width => document.querySelector('#map').viewBox.baseVal.width < width",
                arg=before,
                timeout=min(timeout_ms, 3000),
            )
        except Exception as error:
            after = page.locator("#map").evaluate("map => map.viewBox.baseVal.width")
            measurements.append({
                "step": step,
                "wheel_anchor": anchor,
                "viewBoxWidthBefore": before,
                "viewBoxWidthAfter": after,
                "status": "blocked_wheel_not_received_by_svg_map",
            })
            raise ScenarioBlocked(
                f"실제 휠 입력이 지도 줌을 바꾸지 않았습니다. 포인터 위치와 SVG 지도를 확인하세요: {anchor}"
            ) from error
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        after_geometry = map_point_geometry(page, x, z, dong_name)
        measurements.append({
            "step": step,
            "wheel_anchor": anchor,
            "viewBoxWidthBefore": before,
            "viewBoxWidthAfter": after_geometry["viewBoxWidth"],
            "targetScreenPointAfter": after_geometry["screenPoint"],
            "metersPerCssPixelAfter": after_geometry["metersPerCssPixel"],
            "hitTargetAfter": after_geometry["hitTarget"],
            "nearestAccurateDistrictPixel": nearest_map_click_pixel(page, x, z, dong_name),
            "focusPinCoversTargetAfter": after_geometry["pinCoversPoint"],
            "status": "zoomed_by_visible_map_wheel",
        })
    raise ScenarioBlocked(
        f"{dong_name} 실제 조사점이 24회 사용자 휠 줌 뒤에도 2m/CSS픽셀 이하의 지도 클릭 위치에 도달하지 못했습니다"
    )


def capture_next_map_click(page) -> None:
    page.evaluate(
        """() => {
          window.__notebookMapClick = null;
          document.addEventListener('click', event => {
            const map = document.querySelector('#map');
            const inMap = map.contains(event.target);
            const point = inMap
              ? new DOMPoint(event.clientX, event.clientY).matrixTransform(map.getScreenCTM().inverse())
              : null;
            window.__notebookMapClick = {
              clientX: event.clientX, clientY: event.clientY,
              target: {tag: event.target.tagName.toLowerCase(), className: event.target.getAttribute('class'),
                ariaLabel: event.target.getAttribute('aria-label')},
              path: event.composedPath().slice(0, 6).map(node => ({tag: node.tagName?.toLowerCase(),
                id: node.id || '', className: typeof node.className === 'string' ? node.className : node.getAttribute?.('class')})),
              inMap,
              mapPoint: point ? {x: point.x, z: point.y} : null
            };
          }, {capture: true, once: true});
        }"""
    )


def wait_for_latest_reply(page, user_text: str, timeout_ms: int):
    page.wait_for_function(
        """text => {
          const submit = document.querySelector('#submit');
          const users = [...document.querySelectorAll('#feed article.turn.user p')];
          const replies = [...document.querySelectorAll('#feed article.turn.agent')];
          return !submit.disabled && users.at(-1)?.innerText.trim() === text
            && replies.length > 0 && !replies.at(-1).classList.contains('busy');
        }""",
        arg=user_text,
        timeout=timeout_ms,
    )
    return page.locator("#feed article.turn.agent").last


def select_visible_choice(page, label: str, timeout_ms: int) -> None:
    choices = wait_for_choices(page, timeout_ms)
    matching = [button for button, actual in choices if actual == label]
    if len(matching) != 1:
        raise AssertionError(f"화면에 실제 '{label}' 선택지가 하나 있어야 합니다: {[text for _, text in choices]}")
    matching[0].click()


def send_visible_message(page, text: str, timeout_ms: int) -> None:
    page.get_by_role("textbox", name="창업 상담 질문").fill(text)
    page.get_by_role("button", name="질문 보내기").click()
    page.wait_for_function("() => !document.querySelector('#submit').disabled", timeout=timeout_ms)


def main() -> int:
    parser = argparse.ArgumentParser(description="/ 상담 화면의 오프라인 데스크톱 E2E")
    parser.add_argument("--browser", choices=("chrome", "msedge"), required=True, help="설치된 Chrome 또는 Edge 채널")
    parser.add_argument("--out", type=Path, required=True, help="새 스크린샷·report.json 출력 폴더")
    parser.add_argument("--headless", action="store_true", help="화면 없는 실행(실제 Windows 게이트를 대체하지 않음)")
    parser.add_argument("--timeout-ms", type=int, default=30000)
    args = parser.parse_args()
    if args.timeout_ms < 1000:
        parser.error("--timeout-ms는 1000 이상이어야 합니다")

    out = args.out.expanduser().resolve()
    outputs = [out / "report.json", *(out / filename for filename in SCREENSHOTS)]
    existing = [path for path in outputs if path.exists()]
    if existing:
        parser.error(f"기존 증거를 덮어쓰지 않습니다. 새 --out 폴더를 지정하세요: {existing[0]}")
    out.mkdir(parents=True, exist_ok=True)

    report = {
        "status": "failed",
        "scenario": "root-A-B-site-radius-population-offline-desktop",
        "browser": args.browser,
        "headless": args.headless,
        "viewport": {"width": 1920, "height": 1080, "device_scale_factor": 1},
        "app": "source server, offline=True, loopback free port, route /",
        "data": data_receipt(),
        "release_gates": {
            "source_browser_e2e": "failed",
            "clean_windows_double_click": "not_performed",
            "packaged_zip_ui": "not_performed",
        },
        "steps": [],
        "errors": [],
    }
    server = None
    server_thread = None
    browser = None
    try:
        server = make_server(host="127.0.0.1", port=0, offline=True)
        server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        server_thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}/"
        origin = urlsplit(base_url).netloc
        console_errors: list[str] = []
        page_errors: list[str] = []
        http_errors: list[dict] = []
        external_requests: list[str] = []

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=args.browser, headless=args.headless)
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                device_scale_factor=1,
                locale="ko-KR",
            )
            page = context.new_page()
            page.set_default_timeout(args.timeout_ms)
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on("response", lambda response: http_errors.append({"status": response.status, "url": response.url})
                    if response.status >= 400 else None)

            def guard_external(route) -> None:
                parsed = urlsplit(route.request.url)
                if parsed.scheme == "http" and parsed.netloc == origin:
                    route.continue_()
                else:
                    external_requests.append(route.request.url)
                    route.abort()

            page.route("**/*", guard_external)
            response = page.goto(base_url, wait_until="networkidle", timeout=args.timeout_ms)
            if response is None or response.status != 200:
                raise AssertionError(f"루트 페이지 응답이 정상이 아닙니다: {response.status if response else '없음'}")
            if urlsplit(page.url).path != "/":
                raise AssertionError(f"상담은 /에서 열려야 합니다: {page.url}")
            question = page.get_by_role("textbox", name="창업 상담 질문")
            question.wait_for(state="visible")
            page.screenshot(path=str(out / SCREENSHOTS[0]))
            report["browser_version"] = browser.version
            report["url"] = page.url
            report["steps"].append({"action": "open_root", "status": "passed", "screenshot": SCREENSHOTS[0]})

            send_visible_message(page, PROMPT, args.timeout_ms)
            region_choices = wait_for_choices(page, args.timeout_ms)
            region_labels = [label for _, label in region_choices]
            has_region = any("제주시" in label or "서귀포시" in label for label in region_labels)
            if not 2 <= len(region_labels) <= 3 or UNKNOWN_CHOICE not in region_labels or not has_region:
                raise AssertionError(f"지역 질문에 실제 2~3개 선택지와 '{UNKNOWN_CHOICE}'가 있어야 합니다: {region_labels}")
            page.screenshot(path=str(out / SCREENSHOTS[1]))
            report["steps"].append({
                "action": "send_first_consultation",
                "prompt": PROMPT,
                "visible_region_choices": region_labels,
                "status": "passed",
                "screenshot": SCREENSHOTS[1],
            })

            select_visible_choice(page, UNKNOWN_CHOICE, args.timeout_ms)
            page.wait_for_function("() => !document.querySelector('#submit').disabled", timeout=args.timeout_ms)
            preference_choices = wait_for_choices(page, args.timeout_ms)
            preference_labels = [label for _, label in preference_choices]
            if ANCHOR_PREFERENCE not in preference_labels or SHARE_PREFERENCE not in preference_labels:
                raise AssertionError(
                    "지역 미정 다음에 실제 A/B 선호 선택지가 모두 보여야 합니다: "
                    f"{preference_labels}"
                )
            page.screenshot(path=str(out / SCREENSHOTS[2]))
            report["steps"].append({
                "action": "choose_unknown_region",
                "choice": UNKNOWN_CHOICE,
                "visible_preference_choices": preference_labels,
                "status": "passed",
                "screenshot": SCREENSHOTS[2],
            })

            select_visible_choice(page, ANCHOR_PREFERENCE, args.timeout_ms)
            a_answer = wait_for_candidate_answer(page, args.timeout_ms)
            a_summary = a_answer.locator("p").first.inner_text()
            if ANCHOR_PREFERENCE not in a_summary and "생활시설 등록점포 수 순서" not in a_summary:
                raise AssertionError(f"A 결과가 생활시설 기준임을 답변에서 확인할 수 없습니다: {a_summary}")
            a_cards, a_pins = current_candidate_snapshot(page)
            if not all(row["reason"] and "생활시설 등록점포" in row["reason"] for row in a_cards):
                raise AssertionError("A 카드의 실제 생활시설 근거가 보이지 않습니다")
            a_details = candidate_details(a_cards)
            page.screenshot(path=str(out / SCREENSHOTS[3]))
            report["steps"].append({
                "action": "choose_A_lifestyle_preference",
                "visible_choice": ANCHOR_PREFERENCE,
                "answer": a_summary,
                "candidate_order": [
                    {"id": card["id"], "rank": card["rank"], "name": card["name"], "reason": card["reason"]}
                    for card in a_cards
                ],
                "status": "passed",
                "screenshot": SCREENSHOTS[3],
            })

            a_article = latest_candidate_article(page)
            a_card_controls = a_article.locator(".candidate-card")
            second_a = a_cards[1]
            a_card_controls.nth(1).click()
            wait_for_selection(page, a_card_controls, second_a["id"], args.timeout_ms)
            page.screenshot(path=str(out / SCREENSHOTS[4]))
            report["steps"].append({
                "action": "select_A_rank_2_card",
                "candidate_id": second_a["id"],
                "rank": second_a["rank"],
                "status": "passed",
                "screenshot": SCREENSHOTS[4],
            })

            first_a = a_cards[0]
            page.locator(".candidate-marker").nth(0).click()
            wait_for_selection(page, a_card_controls, first_a["id"], args.timeout_ms)
            page.screenshot(path=str(out / SCREENSHOTS[5]))
            report["steps"].append({
                "action": "select_A_rank_1_map_pin",
                "candidate_id": first_a["id"],
                "rank": first_a["rank"],
                "status": "passed",
                "screenshot": SCREENSHOTS[5],
            })

            a_final_cards = read_controls(a_card_controls, "A 후보 카드")
            if candidate_details(a_final_cards) != a_details:
                raise AssertionError("A 카드·핀 선택에서 순서, 근거, 주의점 또는 숫자가 바뀌었습니다")
            a_final_pins = read_controls(page.locator(".candidate-marker"), "A 지도 핀")
            if [(row["id"], row["rank"], row["text"]) for row in a_final_pins] != [
                (row["id"], row["rank"], row["text"]) for row in a_pins
            ]:
                raise AssertionError("A 카드·핀 선택 중 지도 후보 순서가 바뀌었습니다")

            map = page.locator("#map")
            map_box = map.bounding_box()
            if not map_box or map_box["width"] < 100 or map_box["height"] < 100:
                raise AssertionError("줌 입력을 받을 수 있는 가시적 2D 지도가 없습니다")
            width_before_zoom = map.evaluate("element => element.viewBox.baseVal.width")
            page.mouse.move(map_box["x"] + map_box["width"] / 2, map_box["y"] + map_box["height"] / 2)
            page.mouse.wheel(0, -240)
            page.wait_for_function(
                """width => document.querySelector('#map').viewBox.baseVal.width < width""",
                arg=width_before_zoom,
                timeout=args.timeout_ms,
            )
            width_after_zoom = map.evaluate("element => element.viewBox.baseVal.width")
            zoom_cards = read_controls(a_card_controls, "줌 뒤 A 후보 카드")
            if candidate_details(zoom_cards) != a_details:
                raise AssertionError("지도 줌이 A 순위·근거·수치·주의점을 바꿨습니다")
            zoom_pins = read_controls(page.locator(".candidate-marker"), "줌 뒤 지도 핀")
            if not zoom_pins or any(
                row["id"] not in {item["id"] for item in a_cards}
                or row["rank"] != next(item["rank"] for item in a_cards if item["id"] == row["id"])
                for row in zoom_pins
            ):
                raise AssertionError("줌 뒤 지도 핀에 후보에 없는 ID나 바뀐 순번이 있습니다")
            page.screenshot(path=str(out / SCREENSHOTS[6]))
            report["steps"].append({
                "action": "zoom_map_without_changing_A_evidence",
                "viewbox_width_before": width_before_zoom,
                "viewbox_width_after": width_after_zoom,
                "visible_card_count_before_and_after": [len(a_cards), len(zoom_cards)],
                "candidate_order_reasons_and_counts_unchanged": True,
                "visible_map_pins_after_zoom": len(zoom_pins),
                "status": "passed",
                "screenshot": SCREENSHOTS[6],
            })

            send_visible_message(page, SHARE_REQUEST, args.timeout_ms)
            b_answer = wait_for_candidate_answer(page, args.timeout_ms)
            b_summary = b_answer.locator("p").first.inner_text()
            if "동종 업종 등록점포 비중이 낮은 순서" not in b_summary:
                raise AssertionError(f"B 전환 결과와 기준이 화면에서 확인되지 않습니다: {b_summary}")
            b_cards, b_pins = current_candidate_snapshot(page)
            if not all(
                row["reason"] and ("같은 업종" in row["reason"] or "등록점포 분모" in row["reason"])
                for row in b_cards
            ):
                raise AssertionError("B 카드에 같은 업종 비중 또는 확인 불가 분모의 실제 근거가 보이지 않습니다")
            a_order = [(row["id"], row["rank"]) for row in a_cards]
            b_order = [(row["id"], row["rank"]) for row in b_cards]
            if a_order == b_order:
                raise AssertionError("B 선택 뒤 실제 1~3위가 A 순서에서 바뀌지 않았습니다")
            a_reasons = [row["reason"] for row in a_cards]
            b_reasons = [row["reason"] for row in b_cards]
            if a_reasons == b_reasons:
                raise AssertionError("B 선택 뒤 카드에 표시된 실제 선정 이유가 바뀌지 않았습니다")
            b_details = candidate_details(b_cards)
            page.screenshot(path=str(out / SCREENSHOTS[7]))
            report["steps"].append({
                "action": "switch_user_visible_preference_to_B",
                "user_message": SHARE_REQUEST,
                "answer": b_summary,
                "A_order": a_order,
                "B_order": b_order,
                "A_reasons": a_reasons,
                "B_reasons": b_reasons,
                "ranks_and_reasons_changed": True,
                "status": "passed",
                "screenshot": SCREENSHOTS[7],
            })

            b_article = latest_candidate_article(page)
            b_card_controls = b_article.locator(".candidate-card")
            second_b = b_cards[1]
            b_card_controls.nth(1).click()
            wait_for_selection(page, b_card_controls, second_b["id"], args.timeout_ms)
            page.screenshot(path=str(out / SCREENSHOTS[8]))
            report["steps"].append({
                "action": "select_B_rank_2_card",
                "candidate_id": second_b["id"],
                "rank": second_b["rank"],
                "status": "passed",
                "screenshot": SCREENSHOTS[8],
            })

            width_before_b_zoom = map.evaluate("element => element.viewBox.baseVal.width")
            b_map_box = map.bounding_box()
            if not b_map_box:
                raise AssertionError("B 결과의 2D 지도가 보이지 않습니다")
            page.mouse.move(b_map_box["x"] + b_map_box["width"] / 2, b_map_box["y"] + b_map_box["height"] / 2)
            page.mouse.wheel(0, -240)
            page.wait_for_function(
                """width => document.querySelector('#map').viewBox.baseVal.width < width""",
                arg=width_before_b_zoom,
                timeout=args.timeout_ms,
            )
            zoom_b_cards = read_controls(b_card_controls, "줌 뒤 B 후보 카드")
            if candidate_details(zoom_b_cards) != b_details:
                raise AssertionError("지도 줌이 B 후보 순위·근거·수치·주의점을 바꿨습니다")
            b_zoom_pins = read_controls(page.locator(".candidate-marker"), "줌 뒤 B 지도 핀")
            if not b_zoom_pins or any(
                row["id"] not in {item["id"] for item in b_cards}
                or row["rank"] != next(item["rank"] for item in b_cards if item["id"] == row["id"])
                for row in b_zoom_pins
            ):
                raise AssertionError("줌 뒤 B 지도 핀에 후보에 없는 ID나 바뀐 순번이 있습니다")
            page.screenshot(path=str(out / SCREENSHOTS[9]))
            report["steps"].append({
                "action": "zoom_map_without_changing_B_evidence",
                "viewbox_width_before": width_before_b_zoom,
                "viewbox_width_after": map.evaluate("element => element.viewBox.baseVal.width"),
                "visible_card_count_before_and_after": [len(b_cards), len(zoom_b_cards)],
                "candidate_order_reasons_and_counts_unchanged": True,
                "visible_map_pins_after_zoom": len(b_zoom_pins),
                "status": "passed",
            })

            if report["data"]["store_date"] != EXPECTED_STORE_DATE:
                raise AssertionError(
                    f"승인된 조천 반경 기대치는 상가 기준일 {EXPECTED_STORE_DATE}이며, "
                    f"현재 자료는 {report['data']['store_date']}입니다"
                )
            select = page.locator("#select")
            option = select.locator(f'option[value="{SITE_DONG_CODE}"]')
            option.wait_for(state="attached", timeout=args.timeout_ms)
            option_label = " ".join(option.inner_text().split())
            if SITE_DONG not in option_label:
                raise AssertionError(f"동네 선택 목록에 승인된 {SITE_DONG}가 없습니다: {option_label}")
            select.select_option(value=SITE_DONG_CODE)
            try:
                page.wait_for_function(
                    """name => document.querySelector('#place').innerText === name
                      && !document.querySelector('#submit').disabled""",
                    arg=SITE_DONG,
                    timeout=args.timeout_ms,
                )
            except Exception as error:
                visible_heading = page.locator("#place").inner_text()
                page.screenshot(path=str(out / SCREENSHOTS[10]))
                report["steps"].append({
                    "action": "select_chochoon_from_visible_dong_list",
                    "dong_code": SITE_DONG_CODE,
                    "visible_option": option_label,
                    "visible_heading_after_selection": visible_heading,
                    "status": "blocked",
                    "reason": "The rendered consultation/map did not retain the selected dong.",
                    "screenshot": SCREENSHOTS[10],
                })
                raise ScenarioBlocked(
                    f"선택 목록에서 {SITE_DONG}를 눌렀지만 화면 초점이 {visible_heading!r}로 유지되어 "
                    "해당 읍면동 지도 조사를 진행할 수 없습니다"
                ) from error
            if "제주시" not in page.locator("#area").inner_text():
                page.screenshot(path=str(out / SCREENSHOTS[10]))
                report["steps"].append({
                    "action": "select_chochoon_from_visible_dong_list",
                    "dong_code": SITE_DONG_CODE,
                    "visible_heading": page.locator("#place").inner_text(),
                    "status": "blocked",
                    "reason": "The selected dong's municipality is absent from the map breadcrumb.",
                    "screenshot": SCREENSHOTS[10],
                })
                raise ScenarioBlocked("조천읍 지도 경로에서 제주시를 확인할 수 없습니다")
            if "카페" not in page.locator(".profile-bar").inner_text():
                raise AssertionError("조천읍 선택 뒤에도 카페 업종 조건이 화면에 남아 있지 않습니다")
            page.screenshot(path=str(out / SCREENSHOTS[10]))
            report["steps"].append({
                "action": "select_chochoon_from_visible_dong_list",
                "dong_code": SITE_DONG_CODE,
                "visible_option": option_label,
                "visible_heading": page.locator("#place").inner_text(),
                "status": "passed",
                "screenshot": SCREENSHOTS[10],
            })

            x, z = map_projection()
            focus_store_center = dong_store_center(SITE_DONG_CODE)
            focus_store_center_offset_m = math.hypot(
                focus_store_center[0] - x, focus_store_center[1] - z
            )
            initial_geometry = map_point_geometry(page, x, z, SITE_DONG)
            if not initial_geometry["inDistrict"] or not initial_geometry["inViewport"]:
                page.screenshot(path=str(out / SCREENSHOTS[10]))
                report["steps"].append({
                    "action": "click_actual_chochoon_survey_location",
                    "wgs84_lat_lon": {"lat": SITE_LAT_LON[0], "lon": SITE_LAT_LON[1]},
                    "map_geometry": initial_geometry,
                    "status": "blocked",
                    "reason": "The actual approved location is not a clickable point inside the visible dong map.",
                    "screenshot": SCREENSHOTS[10],
                })
                raise ScenarioBlocked(f"실제 조사 위치가 보이는 조천읍 지도에서 클릭할 수 없습니다: {initial_geometry}")

            final_geometry, zoom_steps = zoom_to_clickable_map_point(
                page, x, z, SITE_DONG, args.timeout_ms
            )
            target_point = final_geometry["screenPoint"]
            click_pixel = final_geometry["clickPixel"]
            page.screenshot(path=str(out / SCREENSHOTS[11]))
            report["steps"].append({
                "action": "zoom_visible_map_to_unobstructed_actual_site_point",
                "zoom_input_count": len(zoom_steps),
                "initial_point_hit": initial_geometry["hitTarget"],
                "initial_focus_pin": initial_geometry["focusPin"],
                "initial_site_point_covered_by_focus_pin": initial_geometry["pinCoversPoint"],
                "focus_pin_source_store_center": {"x": focus_store_center[0], "z": focus_store_center[1]},
                "focus_pin_center_offset_from_requested_site_m": focus_store_center_offset_m,
                "actual_wheel_steps": zoom_steps,
                "final_point_hit": final_geometry["hitTarget"],
                "nearest_accurate_click_pixel": click_pixel,
                "final_meters_per_css_pixel": final_geometry["metersPerCssPixel"],
                "status": "passed",
                "screenshot": SCREENSHOTS[11],
            })

            capture_next_map_click(page)
            page.mouse.click(click_pixel["screenX"], click_pixel["screenY"])
            page.wait_for_function("() => window.__notebookMapClick !== null", timeout=args.timeout_ms)
            observed_click = page.evaluate("() => window.__notebookMapClick")
            expected_label = f"{SITE_DONG} 선택"
            click_hit_correct_region = (
                observed_click["inMap"]
                and observed_click["target"]["tag"] == "path"
                and "district" in str(observed_click["target"].get("className", "")).split()
                and observed_click["target"].get("ariaLabel") == expected_label
                and observed_click["mapPoint"] is not None
            )
            if not click_hit_correct_region:
                page.screenshot(path=str(out / SCREENSHOTS[11]))
                report["steps"].append({
                    "action": "click_actual_chochoon_survey_location",
                    "wgs84_lat_lon": {"lat": SITE_LAT_LON[0], "lon": SITE_LAT_LON[1]},
                    "map_point": {"x": x, "z": z},
                    "measured_screen_point": target_point,
                    "actual_click_pixel": click_pixel,
                    "actual_event": observed_click,
                    "status": "blocked",
                    "reason": "The browser click did not reach 조천읍's SVG district path; no API fallback was used.",
                    "screenshot": SCREENSHOTS[11],
                })
                raise ScenarioBlocked(
                    f"실제 조사 지점 클릭이 조천읍 SVG 경계가 아닌 요소에 도달했습니다: {observed_click}"
                )
            try:
                page.locator(".site-radius h3").wait_for(state="visible", timeout=args.timeout_ms)
            except Exception as error:
                page.screenshot(path=str(out / SCREENSHOTS[11]))
                report["steps"].append({
                    "action": "click_actual_chochoon_survey_location",
                    "wgs84_lat_lon": {"lat": SITE_LAT_LON[0], "lon": SITE_LAT_LON[1]},
                    "map_point": {"x": x, "z": z},
                    "actual_event": observed_click,
                    "status": "blocked",
                    "reason": "The SVG district click did not open its site-radius panel; no API fallback was used.",
                    "screenshot": SCREENSHOTS[11],
                })
                raise ScenarioBlocked(
                    "조천읍 SVG 지도 경계를 눌렀지만 실제 점포 반경 선택 패널이 열리지 않았습니다"
                ) from error
            site_heading = page.locator(".site-radius h3").inner_text()
            site_receipt = page.locator(".site-radius .receipt").inner_text()
            if (
                "조사 지점" not in site_heading
                or "카페" not in site_heading
                or EXPECTED_STORE_DATE not in site_receipt
                or "793" not in site_receipt
            ):
                raise AssertionError(f"실제 지도 클릭에 대한 793 출처·기준일이 보이지 않습니다: {site_heading} {site_receipt}")
            site_point = page.locator("#map .site-point")
            if not site_point.count():
                raise AssertionError("실제 지점 클릭 뒤에도 지도에 선택 지점이 표시되지 않습니다")
            rendered_point = site_point.evaluate("circle => ({x: Number(circle.getAttribute('cx')), z: Number(circle.getAttribute('cy'))})")
            clicked_map_point = observed_click["mapPoint"]
            click_error_m = math.hypot(clicked_map_point["x"] - x, clicked_map_point["z"] - z)
            if math.hypot(rendered_point["x"] - clicked_map_point["x"], rendered_point["z"] - clicked_map_point["z"]) > 0.01:
                raise AssertionError(f"지도 원 중심이 브라우저의 실제 SVG 클릭 좌표와 다릅니다: {rendered_point} vs {clicked_map_point}")
            if click_error_m > 2:
                raise AssertionError(
                    f"실제 브라우저 클릭 좌표가 WGS84 조사 지점에서 {click_error_m:.2f}m 벗어났습니다: "
                    f"target={(x, z)}, click={clicked_map_point}"
                )

            radius_results = {}
            for screenshot, radius in ((SCREENSHOTS[12], 300), (SCREENSHOTS[13], 500)):
                radius_button = page.get_by_role("button", name=f"{radius}m", exact=True)
                if not radius_button.is_visible():
                    raise AssertionError(f"실제 지도 조사 뒤 {radius}m 반경 선택지가 보이지 않습니다")
                radius_button.click()
                if radius_button.get_attribute("aria-pressed") != "true":
                    raise AssertionError(f"{radius}m 반경이 화면에서 선택 상태로 표시되지 않습니다")
                result_text = page.locator(".site-radius .radius-result").inner_text()
                numerator, denominator = EXPECTED_SITE_COUNTS[radius]
                if f"{numerator}곳" not in result_text or f"{denominator}곳 중" not in result_text:
                    raise AssertionError(
                        f"실제 {radius}m 카페/음식점 집계가 승인된 결과와 다릅니다: {result_text}"
                    )
                page.screenshot(path=str(out / screenshot))
                radius_results[str(radius)] = result_text
                report["steps"].append({
                    "action": f"select_actual_site_radius_{radius}m",
                    "result": result_text,
                    "status": "passed",
                    "screenshot": screenshot,
                })
            if radius_results["300"] == radius_results["500"]:
                raise AssertionError("300m와 500m 화면 집계가 달라지지 않았습니다")
            report["site"] = {
                "dong": SITE_DONG,
                "dong_code": SITE_DONG_CODE,
                "wgs84_lat_lon": {"lat": SITE_LAT_LON[0], "lon": SITE_LAT_LON[1]},
                "projection_origin_lon_lat": {"lon": MAP_ORIGIN_LAT_LON[1], "lat": MAP_ORIGIN_LAT_LON[0]},
                "map_point": {"x": x, "z": z},
                "screen_point": target_point,
                "browser_click_pixel": click_pixel,
                "actual_svg_click": observed_click,
                "rendered_site_center": rendered_point,
                "click_error_m": click_error_m,
                "initial_overlay_diagnostic": {
                    "hit_target": initial_geometry["hitTarget"],
                    "focus_pin": initial_geometry["focusPin"],
                    "focus_pin_covered_target": initial_geometry["pinCoversPoint"],
                    "focus_pin_source_store_center": {"x": focus_store_center[0], "z": focus_store_center[1]},
                    "focus_pin_center_offset_from_requested_site_m": focus_store_center_offset_m,
                },
                "map_zoom": {
                    "steps": zoom_steps,
                    "final_meters_per_css_pixel": final_geometry["metersPerCssPixel"],
                },
                "source": site_heading,
                "receipt": site_receipt,
                "independent_expected_counts": EXPECTED_SITE_COUNTS,
                "independent_sources": ["docs/데이터활용명세.md §1", "tests/test_place_guard.py::RequestShapeTest.test_site_radius_matches_documented_sites_with_shipped_stores"],
                "radii": radius_results,
            }

            population_question = f"{SITE_DONG} 누가 오가나요?"
            send_visible_message(page, population_question, args.timeout_ms)
            population_article = wait_for_latest_reply(page, population_question, args.timeout_ms)
            population_article_text = population_article.inner_text()
            page.screenshot(path=str(out / SCREENSHOTS[14]))
            records_text = page.locator("#records").inner_text()
            population_panel = population_article.locator(".population-card")
            records_population = page.locator("#records h3", has_text="내국인 유동인구 · 관측")
            panel_text = population_panel.first.inner_text() if population_panel.count() else ""
            visible_text = "\n".join((population_article_text, panel_text, records_text, page.locator("#hint").inner_text()))
            missing_markers = (
                "제주데이터허브 583 관측 자료가 없습니다",
                "583 자료 없음",
                "유동인구 자료가 없습니다",
                "유동인구 관측 자료가 없습니다",
                "인구 자료를 확인하지 못",
            )
            missing = next((marker for marker in missing_markers if marker in visible_text), None)
            if missing:
                if (
                    SITE_DONG not in visible_text
                    or "읍면동" not in visible_text
                    or "0명으로 해석하지 않습니다" not in visible_text
                ):
                    report["steps"].append({
                        "action": "ask_population_in_visible_composer",
                        "user_message": population_question,
                        "visible_answer": visible_text,
                        "status": "blocked",
                        "reason": "Missing-data text lacks the requested dong scope or truthful nonzero interpretation.",
                        "screenshot": SCREENSHOTS[14],
                    })
                    raise ScenarioBlocked(
                        f"{SITE_DONG} 인구 자료 없음 안내에 읍면동 범위 또는 0명 오해 방지가 없습니다: {visible_text}"
                    )
                report["population"] = {
                    "status": "truthful_missing",
                    "visible_missing_statement": missing,
                    "scope": f"{SITE_DONG} · 읍면동",
                    "explicitly_not_zero": True,
                    "fresh_assistant_article": population_article_text,
                }
            else:
                receipt = (
                    population_panel.locator(".receipt").all_inner_texts()
                    if population_panel.count()
                    else page.locator("#records .receipt").all_inner_texts()
                )
                population_receipts = [text for text in receipt if "제주데이터허브 583" in text]
                observed_text = " ".join((panel_text, records_text, *population_receipts))
                if (
                    not (population_panel.count() or records_population.count())
                    or not population_receipts
                    or "읍면동" not in observed_text
                    or SITE_DONG not in observed_text
                ):
                    report["steps"].append({
                        "action": "ask_population_in_visible_composer",
                        "user_message": population_question,
                        "visible_answer": visible_text,
                        "status": "blocked",
                        "reason": "The fresh UI reply contains neither scoped/date-stamped 583 observation nor explicit truthful missing data.",
                        "screenshot": SCREENSHOTS[14],
                    })
                    raise ScenarioBlocked(
                        f"{SITE_DONG} 인구 문의 뒤 583 읍면동 관측·자료 없음 중 어느 쪽도 확인되지 않습니다: {visible_text}"
                    )
                if not re.search(r"20\d{2}-\d{2}-\d{2}", " ".join(population_receipts)):
                    raise AssertionError(f"인구 자료의 실제 기준일이 보이지 않습니다: {population_receipts}")
                report["population"] = {
                    "status": "observed",
                    "scope": f"{SITE_DONG} · 읍면동",
                    "visible_section": panel_text or records_text,
                    "receipt": population_receipts,
                }
            report["steps"].append({
                "action": "ask_population_in_visible_composer",
                "user_message": f"{SITE_DONG} 누가 오가나요?",
                "scope": "읍면동",
                "result": report["population"],
                "status": "passed",
                "fresh_assistant_article": True,
                "screenshot": SCREENSHOTS[14],
            })

            report["network"] = {
                "external_requests": external_requests,
                "http_errors": http_errors,
                "console_errors": console_errors,
                "page_errors": page_errors,
            }
            if any(report["network"].values()):
                raise AssertionError(f"브라우저 E2E 오류 또는 외부 요청이 있습니다: {report['network']}")
            report["runtime"] = {
                "device_pixel_ratio": page.evaluate("window.devicePixelRatio"),
                "viewport": page.evaluate("({width: innerWidth, height: innerHeight})"),
            }
            if report["runtime"] != {
                "device_pixel_ratio": 1,
                "viewport": {"width": 1920, "height": 1080},
            }:
                raise AssertionError(f"실행 뷰포트가 1920×1080, 배율 100%가 아닙니다: {report['runtime']}")
            context.close()
            browser.close()
            report["release_gates"]["source_browser_e2e"] = "passed"
            report["status"] = "passed"
    except ScenarioBlocked as error:
        report["status"] = "blocked"
        report["release_gates"]["source_browser_e2e"] = "blocked"
        report["errors"].append(f"{type(error).__name__}: {error}")
    except Exception as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        if server:
            try:
                server.shutdown()
                server.server_close()
            except Exception as error:
                report["status"] = "failed"
                report["release_gates"]["source_browser_e2e"] = "failed"
                report["errors"].append(f"ServerCleanupError: {error}")
        if server_thread:
            server_thread.join(timeout=5)
            if server_thread.is_alive():
                report["status"] = "failed"
                report["release_gates"]["source_browser_e2e"] = "failed"
                report["errors"].append("ServerCleanupError: local server thread did not stop")

    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
