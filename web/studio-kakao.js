const MAP_DOCUMENT = `<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
@font-face{font-family:Jeju;src:url('/fonts/NotoSansKR-VF-subset.woff2');font-weight:100 900}*{box-sizing:border-box}html,body,#map{width:100%;height:100%;margin:0;overflow:hidden}body{font:14px/1.4 Jeju,sans-serif}.pin{border:1px solid #d18c4d;border-radius:22px;background:#fffaf0;color:#70491f;padding:6px 13px;min-height:36px;white-space:nowrap;box-shadow:0 3px 10px #49372033;font-weight:700;cursor:pointer}.pin.selected{background:#d8792d;color:#fff;border-color:#bd6522}.pin:focus-visible{outline:3px solid #604b37;outline-offset:2px}.map-label{padding:2px 5px;border-radius:3px;background:#fffaf0bb;color:#4a5d50;font-weight:700;white-space:nowrap;pointer-events:none}.site-point{display:block;width:14px;height:14px;border:3px solid #fff8e8;border-radius:50%;background:#9c542c;box-shadow:0 1px 5px #32291f88;pointer-events:none}.pin.comparison{border-color:#4f86a8;background:#eef7fc;color:#2b5471;box-shadow:0 2px 8px #2b547126;pointer-events:none}.pin.comparison.quiet{border-style:dashed;background:#fbfbfa;color:#7b756a}
</style></head><body><div id="map" aria-label="카카오 읍면동 지도"></div><script>
(() => {
  let hostOrigin = null;
  let api = null;
  let map = null;
  let origin = null;
  let currentState = null;
  let cameraKey = null;
  let ready = false;
  let readyTimer = null;
  let polygons = [];
  let overlays = [];
  let circles = [];

  function notify(message) {
    if (hostOrigin) parent.postMessage({source: 'studio-kakao', ...message}, hostOrigin);
  }

  function removeOverlays() {
    for (const polygon of polygons) polygon.setMap(null);
    for (const overlay of overlays) overlay.setMap(null);
    for (const circle of circles) circle.setMap(null);
    polygons = [];
    overlays = [];
    circles = [];
  }

  function toLatLng(point) {
    const [longitude, latitude] = origin;
    const lonPerM = 1 / (111320 * Math.cos(latitude * Math.PI / 180));
    return new api.maps.LatLng(latitude - point[1] / 111320, longitude + point[0] * lonPerM);
  }

  function toLocal(point) {
    const [longitude, latitude] = origin;
    return {
      x: (point.getLng() - longitude) * 111320 * Math.cos(latitude * Math.PI / 180),
      z: -(point.getLat() - latitude) * 111320
    };
  }

  // 비교 행은 이름 옆에 엔진이 준 실제 값·단위·상태를 붙인다. 근거가 없으면 미확인이고 0이 아니다.
  function comparisonSuffix(state, code) {
    const row = (state?.comparison || []).find(item => String(item.id) === String(code));
    return row?.note ? ' · ' + row.note : '';
  }

  function categoryStyle(area, state) {
    const categoryIndex = state.categories.indexOf(state.industryCategory);
    const count = categoryIndex < 0 ? null : Number(area.byCat?.[categoryIndex]);
    const total = Number(area.total);
    const share = count != null && Number.isFinite(count) && total > 0 ? count / total : null;
    const candidateSelected = String(area.code) === state.selectedCandidateId;
    const focused = String(area.code) === state.focus;
    const compared = (state.comparison || []).find(row => String(row.id) === String(area.code));
    const fillOpacity = candidateSelected ? 0.32
      : compared ? (compared.status === 'missing' || compared.status === 'excluded' ? 0.07 : 0.2)
      : focused ? 0.24
      : share == null ? 0.025 : 0.06 + Math.min(share, 1) * 0.48;
    return {
      fillColor: candidateSelected || focused ? '#df984b' : compared ? '#5b93b5' : '#e99445',
      fillOpacity,
      strokeColor: candidateSelected ? '#c36c28' : compared ? '#2f6484' : focused ? '#a7672e' : '#687d73',
      strokeOpacity: candidateSelected || focused || compared ? 0.95 : 0.72,
      strokeWeight: candidateSelected ? 2.5 : compared ? 2.4 : focused ? 2 : 1,
      strokeStyle: compared && (compared.status === 'missing' || compared.status === 'excluded') ? 'dashed' : 'solid'
    };
  }

  // 비교 동네가 있으면 초점 동네까지 함께 보여 준다. 같은 조건의 두 장소가 한 화면에 들어온다.
  function framedPoints(state) {
    const codes = (state.comparison || []).map(row => String(row.id));
    if (state.focus && !codes.includes(state.focus)) codes.push(state.focus);
    const rows = codes.length ? codes : state.focus ? [state.focus] : [];
    return rows
      .map(code => state.areas.find(area => String(area.code) === code))
      .filter(Boolean)
      .map(area => toLatLng(area.center));
  }

  function framedBounds(state) {
    const points = framedPoints(state);
    if (points.length < 2) return points[0] || null;
    const north = Math.max(...points.map(point => point.getLat()));
    const south = Math.min(...points.map(point => point.getLat()));
    const east = Math.max(...points.map(point => point.getLng()));
    const west = Math.min(...points.map(point => point.getLng()));
    if (north - south < 1e-6 && east - west < 1e-6) return points[0];
    return new api.maps.LatLngBounds(
      new api.maps.LatLng(south, west),
      new api.maps.LatLng(north, east)
    );
  }

  function cameraKeyOf(state) {
    if (state.comparison?.length) {
      return 'comparison:' + state.comparison.map(row => row.id).join(',') + '|' + (state.focus || '');
    }
    return state.focus ? 'focus:' + state.focus : state.candidateMapActive ? 'candidates' : 'island';
  }

  function render(nextState) {
    if (!map || !ready || !nextState) return;
    currentState = nextState;
    const nextCameraKey = cameraKeyOf(nextState);
    if (nextCameraKey !== cameraKey) {
      const area = nextState.focus && nextState.areas.find(item => String(item.code) === nextState.focus);
      const bounds = nextState.comparison?.length
        ? framedBounds(nextState)
        : area ? toLatLng(area.center) : null;
      if (bounds?.getSouthWest) {
        map.setBounds(bounds);
      } else {
        map.setCenter(bounds || new api.maps.LatLng(origin[1], origin[0]));
        map.setLevel(nextState.comparison?.length ? 8 : area ? 7 : 10);
      }
      cameraKey = nextCameraKey;
    }
    removeOverlays();
    const areaByCode = new Map(nextState.areas.map(area => [String(area.code), area]));
    for (const area of nextState.areas) {
      const paths = (area.rings || []).map(ring => ring.map(toLatLng)).filter(path => path.length > 2);
      if (!paths.length) continue;
      const polygon = new api.maps.Polygon({
        map,
        path: paths.length === 1 ? paths[0] : paths,
        ...categoryStyle(area, nextState)
      });
      api.maps.event.addListener(polygon, 'click', event => {
        const code = String(area.code);
        const isNotebookCandidate = nextState.candidateMapActive &&
          nextState.notebookResults.some(item => String(item.id) === code);
        if (isNotebookCandidate) {
          notify({type: 'candidate', id: code, answerId: nextState.activeAnswerId});
        } else if (nextState.canInspect && code === nextState.focus) {
          notify({type: 'inspect', point: toLocal(event.latLng)});
        } else {
          notify({type: 'area', code});
        }
      });
      polygons.push(polygon);
      if (/[읍면]$/.test(area.name) || String(area.code) === nextState.focus) {
        const label = document.createElement('span');
        label.className = 'map-label';
        label.textContent = area.name;
        const overlay = new api.maps.CustomOverlay({
          map,
          position: toLatLng(area.center),
          content: label,
          xAnchor: 0.5,
          yAnchor: 0.5,
          zIndex: 2
        });
        overlays.push(overlay);
      }
    }

    const candidates = nextState.candidateMapActive && nextState.notebookResults.length
      ? nextState.notebookResults.map((item, index) => ({
        code: String(item.id), name: item.name, rank: index + 1, answerId: nextState.activeAnswerId, notebook: true
      }))
      : nextState.rank.filter(item => areaByCode.has(String(item.code)));
    if ((!nextState.candidateMapActive || !nextState.notebookResults.length) && nextState.focus &&
        !candidates.some(item => String(item.code) === nextState.focus)) {
      candidates.push({code: nextState.focus, letter: '●'});
    }
    for (const item of candidates) {
      const code = String(item.code);
      const area = areaByCode.get(code);
      if (!area || (!item.notebook && area.name === '추자면')) continue;
      const center = !item.notebook && code === currentState.focus
        ? area.storeCenter || area.center
        : area.center;
      const marker = document.createElement('button');
      marker.type = 'button';
      marker.className = 'pin' + ((item.notebook && code === nextState.selectedCandidateId) ||
        (!item.notebook && code === nextState.focus) ? ' selected' : '');
      marker.textContent = item.notebook
        ? item.rank + ' · ' + (item.name || area.name) + comparisonSuffix(nextState, code)
        : item.letter + ' · ' + area.name + comparisonSuffix(nextState, code);
      marker.addEventListener('click', event => {
        event.stopPropagation();
        if (item.notebook) notify({type: 'candidate', id: code, answerId: item.answerId});
        else if (code === currentState.focus && currentState.canInspect) {
          notify({type: 'inspect', point: {x: center[0], z: center[1]}});
        } else notify({type: 'area', code});
      });
      const overlay = new api.maps.CustomOverlay({
        map,
        position: toLatLng(center),
        content: marker,
        xAnchor: 0.5,
        yAnchor: 1.1,
        zIndex: code === nextState.selectedCandidateId ? 5 : 4,
        clickable: true
      });
      overlays.push(overlay);
    }

    // 비교 동네 표식은 보기 전용이다. 클릭 리스너가 없으므로 지도에서 대화 턴을 만들지 않는다.
    const pinned = new Set(candidates.map(item => String(item.code)));
    for (const row of nextState.comparison || []) {
      const code = String(row.id);
      const area = areaByCode.get(code);
      if (!area || pinned.has(code)) continue;
      const label = document.createElement('span');
      label.className = 'pin comparison' + (row.status === 'missing' || row.status === 'excluded' ? ' quiet' : '');
      label.textContent = (row.name || area.name) + comparisonSuffix(nextState, code);
      overlays.push(new api.maps.CustomOverlay({
        map,
        position: toLatLng(area.center),
        content: label,
        xAnchor: 0.5,
        yAnchor: 1.1,
        zIndex: 3,
        clickable: false
      }));
    }

    if (nextState.sitePoint) {
      const center = toLatLng([nextState.sitePoint.x, nextState.sitePoint.z]);
      if (nextState.siteRadiusM) {
        circles.push(new api.maps.Circle({
          map,
          center,
          radius: nextState.siteRadiusM,
          strokeWeight: 2,
          strokeColor: '#d27934',
          strokeOpacity: 0.9,
          strokeStyle: 'shortdash',
          fillColor: '#e18b36',
          fillOpacity: 0.13
        }));
      }
      const point = document.createElement('span');
      point.className = 'site-point';
      point.setAttribute('aria-label', '조사 지점');
      overlays.push(new api.maps.CustomOverlay({map, position: center, content: point, xAnchor: 0.5, yAnchor: 0.5, zIndex: 6}));
    }
  }

  function initialize() {
    const mapElement = document.getElementById('map');
    map = new api.maps.Map(mapElement, {
      center: new api.maps.LatLng(origin[1], origin[0]),
      level: 10,
      draggable: true,
      scrollwheel: true
    });
    api.maps.event.addListener(map, 'tilesloaded', () => {
      if (!ready) {
        ready = true;
        clearTimeout(readyTimer);
        render(currentState);
        notify({type: 'ready'});
      }
    });
    readyTimer = setTimeout(() => { if (!ready) notify({type: 'failed'}); }, 12000);
  }

  function bootstrap(event) {
    if (event.source !== parent || event.data?.type !== 'boot' || typeof event.data.key !== 'string') return;
    hostOrigin = event.origin;
    origin = event.data.origin;
    if (!Array.isArray(origin) || origin.length !== 2 || !origin.every(Number.isFinite)) {
      notify({type: 'failed'});
      return;
    }
    const sdk = new URL('https://dapi.kakao.com/v2/maps/sdk.js');
    sdk.searchParams.set('appkey', event.data.key);
    sdk.searchParams.set('autoload', 'false');
    const script = document.createElement('script');
    script.src = sdk.href;
    script.onerror = () => notify({type: 'failed'});
    script.onload = () => {
      if (!window.kakao?.maps?.load) {
        notify({type: 'failed'});
        return;
      }
      api = window.kakao;
      api.maps.load(initialize);
    };
    document.head.append(script);
    window.removeEventListener('message', bootstrap);
    window.addEventListener('message', receive);
  }

  function receive(event) {
    if (event.source !== parent || event.origin !== hostOrigin) return;
    if (event.data?.type === 'state') {
      currentState = event.data.state;
      render(currentState);
    } else if (event.data?.type === 'resize' && map && ready) {
      map.relayout();
    }
  }

  window.addEventListener('message', bootstrap);
})();
</script></body></html>`;

export function createKakaoMap(container, onAction) {
  let frame = null;
  let ready = false;
  let currentState = null;
  let resolveStart = null;
  let timeout = null;

  function finish(success) {
    if (!resolveStart) return;
    clearTimeout(timeout);
    const resolve = resolveStart;
    resolveStart = null;
    ready = success;
    if (!success) {
      frame?.remove();
      frame = null;
    } else if (currentState) {
      update(currentState);
    }
    resolve(success);
  }

  function receive(event) {
    if (!frame || event.source !== frame.contentWindow || event.data?.source !== 'studio-kakao') return;
    const {type, ...payload} = event.data;
    if (type === 'ready') finish(true);
    else if (type === 'failed') finish(false);
    else if (ready) onAction({type, ...payload});
  }

  function update(state) {
    currentState = state;
    if (ready && frame?.contentWindow) frame.contentWindow.postMessage({type: 'state', state}, '*');
  }

  return {
    start(key, origin) {
      if (frame) return Promise.resolve(ready);
      frame = document.createElement('iframe');
      frame.title = '카카오 지도 바탕 지도';
      frame.setAttribute('sandbox', 'allow-scripts');
      frame.setAttribute('referrerpolicy', 'strict-origin-when-cross-origin');
      frame.srcdoc = MAP_DOCUMENT;
      frame.addEventListener('load', () => {
        if (frame?.contentWindow) frame.contentWindow.postMessage({type: 'boot', key, origin}, '*');
      }, {once: true});
      window.addEventListener('message', receive);
      container.hidden = false;
      container.classList.add('loading');
      container.append(frame);
      return new Promise(resolve => {
        resolveStart = resolve;
        timeout = setTimeout(() => finish(false), 16000);
      });
    },
    update,
    resize() {
      if (ready && frame?.contentWindow) frame.contentWindow.postMessage({type: 'resize'}, '*');
    },
    destroy() {
      clearTimeout(timeout);
      resolveStart?.(false);
      resolveStart = null;
      ready = false;
      window.removeEventListener('message', receive);
      frame?.remove();
      frame = null;
      currentState = null;
      container.classList.remove('loading');
      container.hidden = true;
      container.replaceChildren();
    }
  };
}
