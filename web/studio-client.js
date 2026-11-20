import {createKakaoMap} from './studio-kakao.js';

const $ = id => document.getElementById(id);
const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  if (className) element.className = className;
  return element;
};
const button = (label, action) => {
  const element = node('button', label, 'btn');
  element.type = 'button';
  element.addEventListener('click', action);
  return element;
};
const svg = (tag, attrs) => {
  const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
  return element;
};

let areas = [];
let profile = null;
let focus = null;
let rank = [];
let notebookResults = [];
let comparison = null;
let candidateMapActive = false;
let selectedCandidateId = null;
let activeAnswerId = null;
let answerCount = 0;
let contextMode = 'general';
let contextId = null;
let contextGeneration = 0;
let pendingController = null;
let contextReady = true;
let busy = false;
let streetAvailable = false;
let selectedPlace = null;
let currentWalk = null;
let currentBuilding = null;
const islandBox = '-42000 -28000 84000 65000';
let mapBase = null;
let roadImage = null;
let dragged = false;
let sitePoint = null;
let siteRadiusStats = null;
let selectedRadiusM = 300;
let kakaoMap = null;
let kakaoMapReady = false;
let kakaoMapGeneration = 0;
let mapCategories = [];
let mapSubcategories = [];
let mapOrigin = null;
const streetCenters = new Map();
const comparisonStyle = node('style');
comparisonStyle.textContent = `
.comparison-card{margin:14px 0 4px;padding:14px 15px;border:1px solid #cfdce6;border-radius:14px;background:#f7fbfd}
.comparison-head{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline;margin:0 0 10px}
.comparison-head h3{margin:0;font-size:14px;color:#3c5568}
.comparison-basis{font-size:12px;font-weight:650;color:#5d7b90}
.comparison-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:9px}
.comparison-place{display:grid;gap:6px;align-content:start;padding:11px 12px;border:1px solid #d7e3ea;border-radius:12px;background:#fff}
.comparison-place.status-missing,.comparison-place.status-excluded{border-style:dashed;background:#fbfbfa}
.comparison-place h4{margin:0;font-size:15px;font-weight:720;color:#2f4657}
.comparison-value{margin:0;font-size:22px;line-height:1.25;font-weight:750;font-variant-numeric:tabular-nums;color:#2c5d7d}
.comparison-value.missing{font-size:15px;font-weight:650;color:#8a8578}
.comparison-bar{position:relative;height:6px;border-radius:3px;background:#e7eef3;overflow:hidden}
.comparison-bar span{position:absolute;inset:0 auto 0 0;border-radius:3px;background:#5b93b5}
.comparison-detail{margin:0;font-size:12px;line-height:1.6;color:#5e6d78}
.comparison-status{justify-self:start;padding:2px 7px;border-radius:20px;background:#e8f2f7;color:#3b6d8a;font-size:11px;font-weight:700}
.comparison-status.status-excluded{background:#fbeee4;color:#96603a}
.comparison-status.status-missing{background:#f1f0ec;color:#7b756a}
.comparison-status.status-outsideTop3{background:#eef1f8;color:#54658f}
.comparison-summary{margin:10px 0 0;font-size:13px;line-height:1.7;color:#455663}
.comparison-note{margin:6px 0 0;font-size:11px;line-height:1.6;color:#7d8a93}
.candidate-marker.comparison-marker{pointer-events:none;border-color:#4f86a8;background:#eef7fc;color:#2b5471;font-weight:700}
.candidate-marker.comparison-marker.status-missing,.candidate-marker.comparison-marker.status-excluded{border-style:dashed;background:#fbfbfa;color:#7b756a}
#map path.district.comparison-selected{stroke:#2f6484;stroke-width:2.4px;fill:rgb(79 134 168 / .22)}
#map path.district.comparison-selected.chosen{fill:rgb(79 134 168 / .3)}
`;
document.head.append(comparisonStyle);
const count = value => value == null ? '미확인' : `${value.toLocaleString('ko-KR')}곳`;
const profileBar = node('div', null, 'profile-bar');
profileBar.hidden = true;
$('consult').querySelector('.section-title').after(profileBar);

function showProfile(slots, state, businessName) {
  profileBar.hidden = !state?.business && state?.business !== 0;
  if (profileBar.hidden) return;
  profileBar.replaceChildren(node('strong', '조사 조건'));
  const conditions = [businessName, state.region, state.area];
  if (state.visit === '목적') conditions.push('목적 방문');
  if (state.visit === '지나가다') conditions.push('주민 단골');
  if (state.competition) conditions.push('경쟁 적은 곳');
  if (state.neighbors) conditions.push('생활시설 주변');
  if (state.growth) conditions.push('사업체 증가');
  for (const value of conditions.filter(Boolean)) profileBar.append(node('span', value));
}
function createStreetFrame() {
  const element = node('iframe');
  element.id = 'streetview';
  element.title = '동네 3D 답사 지도';
  element.hidden = true;
  element.dataset.ready = 'false';
  element.addEventListener('load', () => {
    if (frame !== element) return;
    element.dataset.ready = 'true';
    element.contentWindow.postMessage({source: 'jeju-chat', business: profile?.business ?? null}, location.origin);
    if (currentWalk?.supported) element.contentWindow.postMessage({source: 'jeju-chat', walk: currentWalk}, location.origin);
    if (currentBuilding) element.contentWindow.postMessage({source: 'jeju-chat', building: currentBuilding}, location.origin);
  });
  $('mwrap').append(element);
  return element;
}
let frame;
frame = createStreetFrame();

function resetStreetFrame() {
  frame.remove();
  frame = createStreetFrame();
}
const markerLayer = node('div', null, 'candidate-markers');
$('mwrap').append(markerLayer);
const mapCredit = node('a', '지형 © Copernicus DEM · 도로 © OpenStreetMap', 'map-credit');
mapCredit.href = 'studio-sources.txt';
mapCredit.target = '_blank';
mapCredit.rel = 'noopener';
$('mwrap').append(mapCredit);
const mapHint = node('p', '', 'map-hint');
$('mapLegend')?.remove();
$('maphead').append(mapHint);
// 상권 근거는 대화 답사 카드 안에서만 보여 준다. 고정 #facts 는 접고, 지도 조사 반경 조절만 따로 둔다.
$('facts').hidden = true;
const sitePanel = node('section', null, 'site-radius');
sitePanel.hidden = true;
sitePanel.style.cssText = 'padding:18px 32px 20px;background:#fffdf8;border-top:1px solid #e8ddca;max-height:35vh;overflow:auto';
$('facts').after(sitePanel);

function drawSiteRadiusOverlay() {
  $('map').querySelectorAll('.site-radius-ring, .site-point').forEach(element => element.remove());
  if (!sitePoint) return;
  if (siteRadiusStats) {
    $('map').append(svg('circle', {
      class: 'site-radius-ring', cx: sitePoint.x, cy: sitePoint.z, r: selectedRadiusM,
      'aria-label': `${selectedRadiusM}m 반경`
    }));
  }
  $('map').append(svg('circle', {class: 'site-point', cx: sitePoint.x, cy: sitePoint.z}));
  scaleMapLabels();
}

function showSiteRadius(stats) {
  siteRadiusStats = stats;
  sitePoint = stats?.center || null;
  if (stats?.radii?.length && !stats.radii.some(radius => radius.radiusM === selectedRadiusM)) {
    selectedRadiusM = stats.radii[0].radiusM;
  }
  drawSiteRadiusOverlay();
  updateKakaoMap();
  sitePanel.replaceChildren();
  sitePanel.hidden = !stats;
  if (!stats) return;
  sitePanel.append(node('h3', `조사 지점 · ${stats.business.name}`));
  sitePanel.append(node('p', '반경을 선택하면 해당 거리의 집계와 지도 원이 바뀝니다. 지도 확대·축소는 보기만 바꿉니다.', 'radius-note'));
  const radii = node('div', null, 'radius-options');
  radii.setAttribute('role', 'group');
  radii.setAttribute('aria-label', '점포 집계 반경');
  for (const radius of stats.radii) {
    const choice = button(`${radius.radiusM.toLocaleString('ko-KR')}m`, () => {
      selectedRadiusM = radius.radiusM;
      showSiteRadius(siteRadiusStats);
    });
    choice.classList.add('radius-choice');
    choice.setAttribute('aria-pressed', String(radius.radiusM === selectedRadiusM));
    radii.append(choice);
  }
  sitePanel.append(radii);
  const radius = stats.radii.find(item => item.radiusM === selectedRadiusM);
  if (radius) {
    const row = node('div', null, 'row radius-result');
    row.append(
      node('span', `${radius.radiusM.toLocaleString('ko-KR')}m 직선반경`),
      node('b', `${radius.numerator.toLocaleString('ko-KR')}곳 · ${radius.share == null ? '비중 미확인' : `${radius.share.toFixed(1)}%`}`),
      node('small', `${stats.business.majorCategory} 점포 ${radius.denominator.toLocaleString('ko-KR')}곳 중`)
    );
    sitePanel.append(row);
  }
  sitePanel.append(node('p',
    `제주데이터허브 793 상가(상권) 정보 · ${stats.date} 기준입니다. 같은 소분류 점포 수를 같은 대분류 점포 수로 나눈 비중이며, 경계 밖 인접 동네도 반경에 포함합니다.`,
    'receipt'));
}

const walkBadge = node('div', '가정 보행 예시 · 관측된 유동인구가 아닙니다.', 'walk-badge');
walkBadge.hidden = true;
$('mwrap').append(walkBadge);
const viewSwitch = button('3D 거리', () => {
  if (frame.hidden) showStreet(focus);
  else showMapView();
});
viewSwitch.id = 'viewSwitch';
viewSwitch.hidden = true;
$('mwrap').append(viewSwitch);

function kakaoState() {
  const businessId = profile?.business == null ? Number.NaN : Number(profile.business);
  const industryCategory = Number.isInteger(businessId) ? mapSubcategories[businessId]?.cat || null : null;
  return {
    areas: areas.map(({code, name, rings, center, storeCenter, total, byCat}) =>
      ({code, name, rings, center, storeCenter, total, byCat})),
    categories: mapCategories,
    industryCategory,
    focus: focus == null ? null : String(focus),
    candidateMapActive,
    notebookResults: notebookResults.map(({id, name}) => ({id: String(id), name})),
    activeAnswerId,
    selectedCandidateId,
    // 비교는 보기 전용 강조다. 지도 클릭은 이 목록으로 대화를 만들지 않는다.
    // 값·단위는 여기서 한 번만 문장으로 만들어 두 지도와 카드가 같은 숫자를 읽게 한다.
    comparison: comparisonRows().map(row => ({
      id: row.id,
      name: row.name,
      status: row.status,
      note: `${comparisonValueLabel(row) ?? '미확인'} · ${row.statusLabel}`
    })),
    rank: rank.map(({code, name, letter}) => ({code: String(code), name, letter})),
    sitePoint: sitePoint ? {x: sitePoint.x, z: sitePoint.z} : null,
    siteRadiusM: siteRadiusStats ? selectedRadiusM : null,
    canInspect: contextMode === 'general' && profile?.business != null
  };
}

function updateKakaoMap() {
  if (contextMode === 'general') kakaoMap?.update(kakaoState());
}

function updateMapPresentation() {
  const streetVisible = !frame.hidden;
  const kakaoVisible = kakaoMapReady && !streetVisible;
  $('kakaoMap').classList.toggle('loading', Boolean(kakaoMap && !kakaoMapReady));
  $('kakaoMap').hidden = !kakaoVisible;
  $('map').toggleAttribute('hidden', streetVisible || kakaoVisible);
  markerLayer.hidden = streetVisible || kakaoVisible;
  $('maphead').hidden = streetVisible;
  $('mapbar').hidden = streetVisible;
  mapCredit.hidden = streetVisible || kakaoVisible;
  const category = kakaoVisible ? kakaoState().industryCategory : null;
  mapHint.textContent = category ? `주황색 · ${category} 점포 비중` : '';
  mapHint.hidden = streetVisible || !category;
  mapCredit.textContent = '지형 © Copernicus DEM · 도로 © OpenStreetMap';
  if (kakaoVisible) kakaoMap?.resize();
}

function disableKakaoMap() {
  kakaoMapGeneration++;
  const instance = kakaoMap;
  kakaoMap = null;
  kakaoMapReady = false;
  try {
    instance?.destroy();
  } catch {
    // Removing the isolated frame below is the privacy boundary.
  }
  $('kakaoMap').replaceChildren();
  $('kakaoMap').classList.remove('loading');
  $('kakaoMap').hidden = true;
  updateMapPresentation();
  if (contextMode === 'general') drawMap();
  return !$('kakaoMap').querySelector('iframe');
}

async function enableKakaoMap() {
  if (contextMode !== 'general' || !contextId) return;
  if (kakaoMap) {
    updateKakaoMap();
    return;
  }
  const generation = ++kakaoMapGeneration;
  try {
    const [config, metadata, subcategories] = await Promise.all([
      fetch(`/api/map-config?${new URLSearchParams({contextId})}`, {cache: 'no-store'}).then(result => {
        if (!result.ok) throw new Error('map-config');
        return result.json();
      }),
      fetch('/data/meta.json').then(result => {
        if (!result.ok) throw new Error('map-meta');
        return result.json();
      }),
      fetch('/data/subcats.json').then(result => {
        if (!result.ok) throw new Error('map-subcategories');
        return result.json();
      })
    ]);
    if (generation !== kakaoMapGeneration || contextMode !== 'general') return;
    if (typeof config.javascriptKey !== 'string' || !config.javascriptKey.trim()) return;
    mapOrigin = metadata.origin;
    mapCategories = metadata.categories || [];
    mapSubcategories = subcategories;

    const instance = createKakaoMap($('kakaoMap'), action => {
      if (contextMode !== 'general' || kakaoMap !== instance) return;
      if (action.type === 'candidate') selectCandidate(String(action.id), String(action.answerId), true);
      else if (action.type === 'area') focusPlace(String(action.code));
      else if (action.type === 'inspect' && Number.isFinite(action.point?.x) && Number.isFinite(action.point?.z)) {
        inspectMapPoint(null, [action.point.x, action.point.z]);
      }
    });
    kakaoMap = instance;
    instance.update(kakaoState());
    const loaded = await instance.start(config.javascriptKey, mapOrigin);
    if (generation !== kakaoMapGeneration || contextMode !== 'general') {
      instance.destroy();
      return;
    }
    if (!loaded) {
      instance.destroy();
      kakaoMap = null;
      kakaoMapReady = false;
      updateMapPresentation();
      drawMap();
      return;
    }
    kakaoMapReady = true;
    updateKakaoMap();
    updateMapPresentation();
  } catch {
    if (generation !== kakaoMapGeneration || contextMode !== 'general') return;
    disableKakaoMap();
  }
}

function showStreet(code) {
  if (!code || !streetAvailable) return;
  const address = `/index.html?embed=1&dong=${encodeURIComponent(code)}`;
  if (frame.getAttribute('src') !== address) {
    frame.dataset.ready = 'false';
    frame.src = address;
  }
  frame.hidden = false;
  $('maphead').hidden = true;
  $('map').setAttribute('hidden', '');
  $('kakaoMap').hidden = true;
  $('mapbar').hidden = true;
  markerLayer.hidden = true;
  mapCredit.hidden = true;
  mapHint.hidden = true;
  viewSwitch.hidden = false;
  viewSwitch.textContent = '2D 지도';
}

function showMapView() {
  frame.hidden = true;
  updateMapPresentation();
  viewSwitch.textContent = '3D 거리';
  requestAnimationFrame(drawMarkers);
}

function appendTurn(role, text) {
  $('feed').querySelector('.intro')?.remove();
  const article = node('article', null, `turn ${role === '나' ? 'user' : 'agent'}`);
  article.append(node('small', role), node('p', text));
  $('feed').append(article);
  $('feed').scrollTop = $('feed').scrollHeight;
  return article;
}

function showOptions(options) {
  $('options').replaceChildren(...options.slice(0, 4).map((option, index) => {
    const choice = button(null, () => send(option.text || option.label, option.dong || null));
    choice.append(node('span', String(index + 1), 'option-index'), node('span', option.label));
    return choice;
  }));
}

function setContextMode(mode) {
  contextMode = mode;
  return mode !== 'restricted' || disableKakaoMap();
}

function enterRestrictedContext() {
  if (!setContextMode('restricted')) return false;
  profile = null;
  rank = [];
  notebookResults = [];
  comparison = null;
  candidateMapActive = false;
  selectedCandidateId = null;
  activeAnswerId = null;
  selectedPlace = null;
  currentWalk = null;
  currentBuilding = null;
  selectedRadiusM = 300;
  walkBadge.hidden = true;
  showProfile([], null);
  showOptions([]);
  clearFocus();
  resetStreetFrame();
  return !$('kakaoMap').querySelector('iframe');
}

function clearLocalContext() {
  profile = null;
  focus = null;
  rank = [];
  notebookResults = [];
  comparison = null;
  candidateMapActive = false;
  selectedCandidateId = null;
  activeAnswerId = null;
  selectedPlace = null;
  currentWalk = null;
  currentBuilding = null;
  selectedRadiusM = 300;
  walkBadge.hidden = true;
  $('question').value = '';
  $('feed').replaceChildren();
  showProfile([], null);
  showOptions([]);
  showSiteRadius(null);
  resetStreetFrame();
  showMapView();
  clearFocus();
}

async function startNewConsultation(force = false) {
  const mode = 'general';
  if (!force && mode === contextMode && contextReady) return;
  const generation = ++contextGeneration;
  const previousContextId = contextId;
  pendingController?.abort();
  pendingController = new AbortController();
  const controller = pendingController;
  if (contextMode === 'restricted') disableKakaoMap();
  setContextMode(mode);
  contextReady = false;
  clearLocalContext();
  busy = true;
  $('submit').disabled = true;
  const article = appendTurn('상권하르방', '새 상담 공간을 준비하고 있습니다.');
  article.classList.add('busy');
  try {
    const reset = {contextMode: mode, resetContext: true};
    if (previousContextId) reset.contextId = previousContextId;
    const response = await fetch('/api/turn', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(reset),
      signal: controller.signal
    });
    if (!response.ok) throw new Error('context-reset');
    const result = await response.json();
    if (generation !== contextGeneration) return;
    if (result.contextMode !== mode) {
      if (result.contextMode === 'restricted') {
        setContextMode('restricted');
        if (result.contextId) contextId = String(result.contextId);
      } else {
        disableKakaoMap();
      }
      throw new Error('context-boundary');
    }
    if (result.contextId == null || result.contextId === '') {
      disableKakaoMap();
      throw new Error('context-id');
    }
    contextId = String(result.contextId);
    contextReady = true;
    article.querySelector('p').textContent = '새 상담이 시작되었습니다. 이전 대화는 이어지지 않습니다.';
    void enableKakaoMap();
  } catch {
    if (generation !== contextGeneration) return;
    contextReady = false;
    article.querySelector('p').textContent = '새 상담을 시작하지 못했습니다. 다시 초기화해 주세요.';
    article.append(button('새 상담 재시도', () => startNewConsultation(true)));
  } finally {
    if (generation === contextGeneration) {
      article.classList.remove('busy');
      pendingController = null;
      busy = false;
      $('submit').disabled = false;
    }
  }
}

function populationValue(value) {
  return Number.isFinite(value) ? `${value.toLocaleString('ko-KR')}명` : '미확인';
}

function showPopulation(article, population, steps = [], focusCode = null) {
  const card = node('section', null, 'population-card');
  card.append(node('h3', '읍면동 인구 · 관측 자료'));
  const query = [...steps].reverse().find(step => step.tool === 'population');
  const target = query?.args?.dong;
  const rawCode = population?.dongCode || population?.code || areas.find(item => item.code === target || item.name === target)?.code || focusCode || focus;
  const code = rawCode == null ? null : String(rawCode);
  const area = areas.find(item => item.code === code);
  const name = population?.dong || population?.name || area?.name || target;
  if (name) card.append(node('p', `${name} · 읍면동 단위`));
  if (!population) {
    card.append(node('p', '제주데이터허브 583 관측 자료가 없습니다. 0명으로 해석하지 않습니다.'));
    article.append(card);
    return;
  }
  for (const [label, value] of [['거주', population.resd], ['근무', population.work], ['방문', population.visit]]) {
    const row = node('div', null, 'row');
    row.append(node('span', label), node('b', populationValue(value)), node('small', displayDate(population.date)));
    card.append(row);
  }
  const denominator = Number.isFinite(population.denominator)
    ? `${population.denominator.toLocaleString('ko-KR')}명`
    : '분모 미확인';
  const source = population.source === 'live' ? '제주데이터허브 583 API 조회'
    : population.source === 'snapshot' ? '제주데이터허브 583 저장 자료'
      : population.source || '출처 미확인';
  card.append(node('p', `${source} · ${displayDate(population.date)} 기준. 분모 ${denominator}. 읍면동 집계이며 골목 보행 수가 아닙니다.`, 'receipt'));
  article.append(card);
}

function isPopulationInquiry(text) {
  return /누가|유동인구|인구/.test(text || '');
}

// 문장에 동네 이름이 하나만 있으면 그 동네가 이번 질문의 장소다. 둘 이상이면 비교 문장으로 본다.
function typedAreaCode(text) {
  const hits = areas.filter(area => text.includes(area.name));
  return hits.length === 1 ? hits[0].code : null;
}

function metadataText(value, fallback) {
  if (value == null || value === '') return fallback;
  if (typeof value === 'number') return value.toLocaleString('ko-KR');
  if (Array.isArray(value)) return value.map(item => metadataText(item, '')).filter(Boolean).join(' · ');
  if (typeof value === 'object') {
    return Object.entries(value).map(([key, item]) => `${key} ${metadataText(item, '미확인')}`).join(' · ');
  }
  return String(value);
}

function displayDate(value) {
  return metadataText(value, '기준일 미확인').replace(/^(\d{4})(\d{2})(\d{2})$/, '$1-$2-$3');
}

// 비교 카드의 상태는 엔진이 정한 네 가지 문자열만 읽는다. 다른 문자열은 만들어 내지 않는다.
const comparisonStatus = {
  eligible: {label: '조건 충족', className: 'status-eligible'},
  outsideTop3: {label: '상위 3곳 밖', className: 'status-outsideTop3'},
  excluded: {label: '조건 제외', className: 'status-excluded'},
  missing: {label: '자료 없음', className: 'status-missing'}
};
function normalizeComparison(value) {
  const source = value && typeof value === 'object' ? value : null;
  const rows = Array.isArray(source?.rows) ? source.rows.filter(row => row && typeof row === 'object') : [];
  if (!rows.length) return null;
  const normalized = rows.map(row => {
    const id = row.id == null ? null : String(row.id);
    const area = id ? areas.find(item => item.code === id) : null;
    const status = Object.hasOwn(comparisonStatus, row.status) ? row.status : null;
    return {
      id,
      name: row.name || area?.name || id || '이름 미확인',
      // 값이 없으면 0이 아니라 결측이다. 숫자 0만 0으로 적는다.
      value: typeof row.value === 'number' && Number.isFinite(row.value) ? row.value : null,
      detail: typeof row.detail === 'string' ? row.detail : '',
      status,
      statusLabel: status ? comparisonStatus[status].label : '상태 미확인',
      statusClass: status ? comparisonStatus[status].className : 'status-missing'
    };
  });
  return {
    basisLabel: typeof source.basisLabel === 'string' && source.basisLabel ? source.basisLabel : '기준 미확인',
    source: source.source ?? null,
    date: source.date ?? null,
    summary: typeof source.summary === 'string' ? source.summary : '',
    max: Math.max(0, ...normalized.map(row => row.value == null ? 0 : Math.abs(row.value))),
    rows: normalized
  };
}

function showComparison(article, data) {
  if (!data?.rows?.length) return false;
  const card = node('section', null, 'comparison-card');
  const head = node('div', null, 'comparison-head');
  head.append(node('h3', '같은 조건에서 비교'), node('span', `${data.basisLabel} 기준`, 'comparison-basis'));
  card.append(head);
  const grid = node('div', null, 'comparison-grid');
  grid.setAttribute('role', 'group');
  grid.setAttribute('aria-label', '동네 비교');
  for (const row of data.rows) {
    const place = node('article', null, `comparison-place ${row.statusClass}`);
    place.append(node('h4', row.name));
    const shown = comparisonValueLabel(row);
    const valueText = node('p', shown == null ? '미확인' : shown, `comparison-value${shown == null ? ' missing' : ''}`);
    place.append(valueText);
    if (shown != null && data.max > 0 && row.value !== 0) {
      const bar = node('div', null, 'comparison-bar');
      bar.setAttribute('aria-hidden', 'true');
      const fill = node('span');
      fill.style.width = `${Math.min(100, Math.abs(row.value) / data.max * 100)}%`;
      bar.append(fill);
      place.append(bar);
    }
    place.append(node('span', row.statusLabel, `comparison-status ${row.statusClass}`));
    place.append(node('p', row.detail || '세부 근거 미확인', 'comparison-detail'));
    grid.append(place);
  }
  card.append(grid);
  if (data.summary) card.append(node('p', data.summary, 'comparison-summary'));
  const dated = data.date == null || data.date === ''
    ? '기준일 미확인'
    : `기준일 ${displayDate(data.date)}`;
  card.append(node('p',
    `출처 · ${metadataText(data.source, '미확인')} · ${dated}. 결측(미확인)과 0은 다릅니다.`,
    'comparison-note'));
  article.append(card);
  return true;
}

function comparisonRows() {
  return comparison?.rows.filter(row => row.id) || [];
}

// 값은 엔진이 준 숫자 그대로 읽고, 단위만 basisLabel이 말한 것으로 붙인다. 새 점수는 만들지 않는다.
function comparisonValueLabel(row) {
  if (row?.value == null) return null;
  if (/비중/.test(comparison?.basisLabel || '')) return `${(row.value * 100).toFixed(1)}%`;
  return `${row.value.toLocaleString('ko-KR')}곳`;
}

function comparisonRowFor(code) {
  return comparisonRows().find(row => row.id === String(code)) || null;
}

// 지도 표식도 이름 옆에 실제 측정값과 단위를 적는다. 근거가 없으면 0이 아니라 미확인이다.
function comparisonPinSuffix(row) {
  if (!row) return '';
  const shown = comparisonValueLabel(row);
  return ` · ${shown == null ? '미확인' : shown} · ${row.statusLabel}`;
}

// 지도 개요는 후보와 비교 동네를 모두 담는다. 두 지도가 같은 동네를 같은 화면에 보여 준다.
function overviewRows() {
  const rows = candidateMapActive ? notebookResults.map(item => ({id: String(item.id)})) : [];
  for (const row of comparisonRows()) {
    if (!rows.some(item => String(item.id) === row.id)) rows.push({id: row.id});
  }
  return rows;
}

function showNotebook(article, notebook) {
  // 지난 턴의 비교 카드는 지도 강조를 남기지 않는다. 새 응답만 비교를 싣는다.
  comparison = null;
  if (!notebook || !['question', 'result'].includes(notebook.state)) return false;
  for (const oldCard of document.querySelectorAll('.candidate-card')) {
    oldCard.disabled = true;
    oldCard.setAttribute('aria-pressed', 'false');
    oldCard.classList.remove('selected');
  }
  const answer = notebook.summary || notebook.question || '';
  article.querySelector('p').textContent = answer;
  if (notebook.question && notebook.state === 'question' && notebook.question !== answer) {
    article.append(node('p', notebook.question, 'notebook-question'));
  }
  if (notebook.state === 'result') {
    notebookResults = (notebook.results || []).slice(0, 3);
    candidateMapActive = notebookResults.length > 0;
    rank = [];
    selectedCandidateId = null;
    activeAnswerId = String(++answerCount);
    const answerId = activeAnswerId;
    if (notebookResults.length) {
      const list = node('div', null, 'candidate-list');
      list.setAttribute('aria-label', '답사 후보');
      notebookResults.forEach((candidate, index) => {
        const id = String(candidate.id);
        const card = button(null, () => selectCandidate(id, answerId, false));
        card.className = 'candidate-card';
        card.setAttribute('data-candidate-id', id);
        card.setAttribute('data-rank', String(index + 1));
        card.setAttribute('data-answer-id', answerId);
        card.setAttribute('aria-pressed', 'false');
        card.append(node('span', String(index + 1), 'candidate-rank'));
        card.append(node('strong', candidate.name || areas.find(area => area.code === id)?.name || id, 'candidate-name'));
        card.append(node('span', candidate.reason || '선정 이유 미확인', 'candidate-reason'));
        card.append(node('span', candidate.caution || '반대 근거 미확인', 'candidate-caution'));
        const metadata = node('span', null, 'candidate-metadata');
        metadata.append(
          node('span', `기준일 · ${displayDate(candidate.date)}`),
          node('span', `출처 · ${metadataText(candidate.source, '미확인')}`),
          node('span', `분모 · ${metadataText(candidate.denominator, '미확인')}`)
        );
        card.append(metadata);
        list.append(card);
      });
      article.append(list);
    }
  }
  if (notebook.state === 'question') {
    notebookResults = [];
    candidateMapActive = false;
    rank = [];
    selectedCandidateId = null;
    activeAnswerId = null;
  }
  comparison = normalizeComparison(notebook.comparison);
  // 비교 카드는 대화 말풍선 안, 후보 목록 옆에 같은 자료로 붙는다. 지도는 같은 동네를 함께 강조한다.
  if (comparison) showComparison(article, comparison);
  return true;
}

function syncCandidateHighlight(answerId = activeAnswerId) {
  for (const element of document.querySelectorAll('[data-candidate-id]')) {
    const pressed = element.getAttribute('data-answer-id') === answerId &&
      element.getAttribute('data-candidate-id') === selectedCandidateId;
    element.setAttribute('aria-pressed', String(pressed));
    element.classList.toggle('selected', pressed);
  }
  for (const path of $('map').querySelectorAll('.district')) {
    path.classList.toggle('candidate-selected', path.dataset.candidateId === selectedCandidateId);
  }
}

function selectCandidate(id, answerId, fromMap) {
  if (answerId !== activeAnswerId || !notebookResults.some(candidate => String(candidate.id) === id)) return;
  candidateMapActive = true;
  if (focus) showNotebookMap();
  selectedCandidateId = selectedCandidateId === id ? null : id;
  syncCandidateHighlight(answerId);
  drawMarkers();
  updateKakaoMap();
  if (fromMap && selectedCandidateId) {
    for (const card of document.querySelectorAll('.candidate-card')) {
      if (card.dataset.answerId === answerId && card.dataset.candidateId === id) {
        card.scrollIntoView({block: 'nearest'});
        break;
      }
    }
  }
}

function showTrace(article, steps) {
  if (!steps?.length) return;
  const details = node('details');
  details.append(node('summary', `조회 ${steps.length}건`));
  const list = node('ol', null, 'trace-list');
  for (const step of steps) {
    const item = node('li');
    const kind = step.kind === 'observed' ? '관측' : step.kind === 'assumed' ? '가정' : '계산';
    item.append(
      node('span', kind, `trace-kind ${step.kind || 'computed'}`),
      node('strong', step.label || step.tool),
      node('span', step.summary || '')
    );
    list.append(item);
  }
  details.append(list);
  article.append(details);
}

function zoomBox(area) {
  const points = area.rings.flat();
  const xs = points.map(point => point[0]);
  const ys = points.map(point => point[1]);
  const x = Math.min(...xs), y = Math.min(...ys);
  const width = Math.max(...xs) - x, height = Math.max(...ys) - y;
  const ratio = $('map').clientWidth / Math.max($('map').clientHeight, 1);
  const boxWidth = Math.max(width * 1.8, height * 1.8 * ratio, 9000);
  const boxHeight = boxWidth / ratio;
  return `${x + width / 2 - boxWidth / 2} ${y + height / 2 - boxHeight / 2} ${boxWidth} ${boxHeight}`;
}

function notebookOverviewBox(results) {
  const [x0, y0, width, height] = islandBox.split(' ').map(Number);
  const centers = results.map(result => areas.find(area => area.code === String(result.id))?.center).filter(Boolean);
  if (!centers.length) return islandBox;
  const xs = centers.map(point => point[0]);
  const ys = centers.map(point => point[1]);
  const pad = 5000;
  const left = Math.min(x0, ...xs) - pad;
  const top = Math.min(y0, ...ys) - pad;
  const right = Math.max(x0 + width, ...xs) + pad;
  const bottom = Math.max(y0 + height, ...ys) + pad;
  return `${left} ${top} ${right - left} ${bottom - top}`;
}

async function loadMapBase() {
  const [info, response] = await Promise.all([
    fetch('/data/terrain.json').then(result => result.json()),
    fetch('/data/roads.bin.gz')
  ]);
  mapBase = info.bounds;
  const bytes = await new Response(response.body.pipeThrough(new DecompressionStream('gzip'))).arrayBuffer();
  const roads = new DataView(bytes);
  const canvas = document.createElement('canvas');
  canvas.width = info.texture.w;
  canvas.height = info.texture.h;
  const context = canvas.getContext('2d');
  const colors = ['#8f999b', '#9ba4a4', '#a4aaa4', '#adb3aa', '#bbc0b4'];
  const widths = [5, 4, 2.5, 1.5, 0.8];
  let offset = 0;
  while (offset < roads.byteLength) {
    const type = roads.getUint8(offset);
    const points = roads.getUint16(offset + 1, true);
    let x = roads.getUint16(offset + 3, true);
    let y = roads.getUint16(offset + 5, true);
    offset += 7;
    context.beginPath();
    context.moveTo(x * canvas.width / 65535, y * canvas.height / 65535);
    for (let i = 1; i < points; i++) {
      x += roads.getInt16(offset, true);
      y += roads.getInt16(offset + 2, true);
      offset += 4;
      context.lineTo(x * canvas.width / 65535, y * canvas.height / 65535);
    }
    context.strokeStyle = colors[type];
    context.lineWidth = widths[type];
    context.stroke();
  }
  roadImage = URL.createObjectURL(await new Promise(resolve => canvas.toBlob(resolve, 'image/png')));
  drawMap();
}

function scaleMapLabels() {
  const scale = $('map').getScreenCTM()?.a;
  if (!scale) return;
  for (const label of $('map').querySelectorAll('text')) {
    label.setAttribute('font-size', String(14 / scale));
    label.setAttribute('stroke-width', String(3 / scale));
  }
  $('map').querySelector('.site-point')?.setAttribute('r', String(9 / scale));
}

async function inspectMapPoint(event, center = null) {
  const generation = contextGeneration;
  const map = $('map');
  const point = center
    ? {x: center[0], y: center[1]}
    : new DOMPoint(event.clientX, event.clientY).matrixTransform(map.getScreenCTM().inverse());
  const query = new URLSearchParams({x: point.x, z: point.y, business: profile.business});
  try {
    const response = await fetch(`/api/site-radius?${query}`);
    if (!response.ok) throw new Error('site-radius');
    const result = await response.json();
    if (generation !== contextGeneration) return;
    selectedPlace = null;
    showSiteRadius(result.siteRadius);
  } catch {
    if (generation !== contextGeneration) return;
    showSiteRadius(null);
    sitePanel.hidden = false;
    sitePanel.append(node('p', '조사 지점 자료를 불러오지 못했습니다.', 'receipt'));
  }
}

// 지도 조작은 대화를 만들지 않는다. 지도는 로컬 초점만 옮기고, 대화가 지도를 갱신한다.
function focusPlace(code) {
  if (!code || !areas.some(area => area.code === String(code))) return;
  // 후보 개요에서 한 동네로 좁히면 그 동네 지도가 지점 조사까지 받도록 후보 보기를 닫는다.
  candidateMapActive = false;
  showPlace(String(code));
}

// 비교 동네를 한 화면에 담는 상자. 동네 중심 사이를 기준으로 하고 행정경계가 들어가도록 여유를 둔다.
function comparisonBox(rows) {
  const centers = rows.map(row => areas.find(area => area.code === String(row.id))?.center).filter(Boolean);
  if (!centers.length) return islandBox;
  // 지도가 숨겨져 폭이 0일 때도 비교 상자가 무너지지 않게 비율에 하한을 둔다.
  const ratio = Math.max($('map').clientWidth / Math.max($('map').clientHeight, 1), 0.2);
  const pad = 7000;
  const xs = centers.map(point => point[0]);
  const ys = centers.map(point => point[1]);
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2;
  const cy = (Math.min(...ys) + Math.max(...ys)) / 2;
  let width = Math.max(Math.max(...xs) - Math.min(...xs) + pad * 2, 16000);
  let height = Math.max(Math.max(...ys) - Math.min(...ys) + pad * 2, 16000);
  if (width / height < ratio) width = height * ratio;
  else height = width / ratio;
  return `${cx - width / 2} ${cy - height / 2} ${width} ${height}`;
}

function drawMap() {
  const map = $('map');
  map.replaceChildren();
  const selected = areas.find(area => area.code === focus);
  mapHint.textContent = notebookResults.length
    ? '2D 읍면동 지도 · 번호는 답사 순서 · 휠 확대/축소는 집계에 영향 없음'
    : comparison
    ? '2D 읍면동 지도 · 같은 조건으로 비교한 동네를 함께 채움 · 지도 클릭은 대화를 만들지 않음'
    : focus
    ? profile?.business != null
      ? '2D 읍면동 지도 · 휠 확대/축소 · 조사 지점 반경은 별도 선택'
      : '2D 읍면동 지도 · 휠 확대/축소 · 업종 선택 후 지점 클릭'
    : '2D 읍면동 지도 · 휠 확대/축소 · 반경은 별도 선택';
  const compareCodes = new Set(comparisonRows().map(row => row.id));
  // 비교가 있으면 비교 동네를 중심으로 당겨 두 곳을 한 화면에 둔다.
  const framed = comparison ? comparisonBox(overviewRows())
    : candidateMapActive ? notebookOverviewBox(notebookResults)
    : selected ? zoomBox(selected)
    : islandBox;
  map.setAttribute('viewBox', framed);
  if (mapBase) {
    const [x0, y0, x1, y1] = mapBase;
    for (const href of ['/data/terrain_color.jpg', roadImage]) {
      map.append(svg('image', {href, x: x0, y: y0, width: x1 - x0, height: y1 - y0}));
    }
  }
  for (const area of areas) {
    const candidateIndex = candidateMapActive
      ? notebookResults.findIndex(candidate => String(candidate.id) === area.code)
      : -1;
    const path = svg('path', {
      d: area.rings.map(ring => `M${ring.map(point => point.join(',')).join('L')}Z`).join(''),
      'fill-rule': 'evenodd',
      class: `district${area.code === focus ? ' chosen' : ''}${String(area.code) === selectedCandidateId ? ' candidate-selected' : ''}${compareCodes.has(area.code) ? ' comparison-selected' : ''}`,
      role: 'button', tabindex: '0', 'aria-label': `${area.name} 선택`
    });
    if (candidateIndex >= 0) path.dataset.candidateId = area.code;
    path.addEventListener('click', event => {
      if (dragged) return;
      if (candidateIndex >= 0) selectCandidate(area.code, activeAnswerId, false);
      else if (focus === area.code && profile?.business != null) inspectMapPoint(event);
      else focusPlace(area.code);
    });
    path.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        if (candidateIndex >= 0) selectCandidate(area.code, activeAnswerId, false);
        else focusPlace(area.code);
      }
    });
    map.append(path);
  }
  for (const area of areas) {
    if (!/[읍면]$/.test(area.name) && area.code !== focus) continue;
    const label = svg('text', {x: area.center[0], y: area.center[1], 'text-anchor': 'middle'});
    label.textContent = area.name;
    map.append(label);
  }
  drawSiteRadiusOverlay();
  scaleMapLabels();
  requestAnimationFrame(drawMarkers);
  updateKakaoMap();
  updateMapPresentation();
}

$('map').addEventListener('wheel', event => {
  if ($('map').hasAttribute('hidden')) return;
  event.preventDefault();
  const map = $('map');
  const box = map.viewBox.baseVal;
  const pointer = new DOMPoint(event.clientX, event.clientY).matrixTransform(map.getScreenCTM().inverse());
  const width = Math.max(1000, Math.min(84000, box.width * (event.deltaY > 0 ? 1.25 : 0.8)));
  const ratio = width / box.width;
  map.setAttribute('viewBox', `${pointer.x - (pointer.x - box.x) * ratio} ${pointer.y - (pointer.y - box.y) * ratio} ${width} ${box.height * ratio}`);
  scaleMapLabels();
  requestAnimationFrame(drawMarkers);
}, {passive: false});

let pan = null;
$('map').addEventListener('pointerdown', event => {
  if (event.button !== 0 || $('map').hasAttribute('hidden')) return;
  const box = $('map').viewBox.baseVal;
  pan = {x: event.clientX, y: event.clientY, box: [box.x, box.y, box.width, box.height]};
  dragged = false;
});
$('map').addEventListener('pointermove', event => {
  if (!pan) return;
  const dx = event.clientX - pan.x;
  const dy = event.clientY - pan.y;
  if (Math.hypot(dx, dy) < 5 && !dragged) return;
  if (!dragged) $('map').setPointerCapture(event.pointerId);
  dragged = true;
  const matrix = $('map').getScreenCTM();
  $('map').setAttribute('viewBox', `${pan.box[0] - dx / matrix.a} ${pan.box[1] - dy / matrix.d} ${pan.box[2]} ${pan.box[3]}`);
  requestAnimationFrame(drawMarkers);
});
$('map').addEventListener('pointerup', () => {
  pan = null;
  setTimeout(() => { dragged = false; }, 0);
});

function drawMarkers() {
  markerLayer.replaceChildren();
  if (markerLayer.hidden) return;
  const map = $('map');
  const transform = map.getScreenCTM();
  if (!transform) return;
  const rect = $('mwrap').getBoundingClientRect();
  const candidates = notebookResults.length
    && candidateMapActive
    ? notebookResults.map((item, index) => ({
      id: String(item.id), name: item.name, rank: index + 1, answerId: activeAnswerId, notebook: true
    }))
    : rank.filter(item => areas.some(area => area.code === item.code));
  if ((!notebookResults.length || !candidateMapActive) && focus && !candidates.some(item => item.code === focus)) {
    candidates.push({code: focus, letter: '●'});
  }
  const occupied = [];
  const shown = new Set();
  for (const item of candidates) {
    const code = item.notebook ? item.id : item.code;
    const area = areas.find(candidate => candidate.code === code);
    if (!area) continue;
    if (area.name === '추자면' && !item.notebook) continue;
    const center = !item.notebook && code === focus ? streetCenters.get(area.code) || area.storeCenter : area.center;
    const point = new DOMPoint(...center).matrixTransform(transform);
    const x = point.x - rect.left;
    let y = point.y - rect.top;
    if (x < 30 || x > rect.width - 30 || y < 30 || y > rect.height - 30) continue;
    while (occupied.some(previous => Math.abs(previous.x - x) < 105 && Math.abs(previous.y - y) < 46)) y += 48;
    occupied.push({x, y});
    const baseLabel = item.notebook ? `${item.rank} · ${item.name || area.name}` : `${item.letter} · ${area.name}`;
    // 이미 순위 핀이 있는 비교 동네는 핀을 하나 더 만들지 않고 같은 핀에 측정값·단위를 붙인다.
    const label = baseLabel + comparisonPinSuffix(comparisonRowFor(code));
    const pin = button(label, event => {
      if (item.notebook) selectCandidate(code, item.answerId, true);
      else if (code === focus && profile?.business != null) inspectMapPoint(event, center);
      else focusPlace(area.code);
    });
    if (item.notebook) {
      pin.setAttribute('data-candidate-id', code);
      pin.setAttribute('data-rank', String(item.rank));
      pin.setAttribute('data-answer-id', item.answerId);
      pin.setAttribute('aria-pressed', String(code === selectedCandidateId));
    }
    shown.add(code);
    pin.className = `candidate-marker${item.notebook && code === selectedCandidateId || !item.notebook && code === focus ? ' selected' : ''}`;
    pin.style.left = `${x}px`;
    pin.style.top = `${y}px`;
    markerLayer.append(pin);
  }
  // 비교한 동네는 보기 전용 표식이다. 클릭해도 대화 턴을 만들지 않는다.
  for (const row of comparisonRows()) {
    if (shown.has(row.id)) continue;
    const area = areas.find(candidate => candidate.code === row.id);
    if (!area) continue;
    const point = new DOMPoint(...area.center).matrixTransform(transform);
    const x = point.x - rect.left;
    let y = point.y - rect.top;
    if (x < 30 || x > rect.width - 30 || y < 30 || y > rect.height - 30) continue;
    while (occupied.some(previous => Math.abs(previous.x - x) < 105 && Math.abs(previous.y - y) < 46)) y += 48;
    occupied.push({x, y});
    const pin = node('span', `${row.name}${comparisonPinSuffix(row)}`, `candidate-marker comparison-marker ${row.statusClass}`);
    pin.setAttribute('data-comparison-id', row.id);
    pin.style.left = `${x}px`;
    pin.style.top = `${y}px`;
    markerLayer.append(pin);
  }
}

function showPlaceSummary(article, card) {
  if (!card || card.code == null) return;
  const area = areas.find(item => item.code === card.code);
  // 고정 상권 패널 대신 대화 답사 카드 안에 같은 출처·기준일로 한 번만 붙인다.
  const summary = node('section', null, 'place-card');
  summary.style.cssText = 'margin:14px 0 4px;padding:12px 15px;border:1px solid #ebd8b9;border-radius:13px;background:#fffaf0';
  const title = node('h3', `${card.name || area?.name || card.code} · 읍면동 배경`);
  title.style.cssText = 'margin:0 0 8px;color:#584a37;font-size:14px';
  summary.append(title);
  for (const [label, value, note] of [
    ['등록 점포', count(card.total), card.storeDate],
    [card.business?.name ? `${card.business.name} · 읍면동` : '동종 업종 비중',
      card.business ? `${card.business.count.toLocaleString('ko-KR')}곳 · ${card.business.share}%` : '미확인',
      card.business ? `읍면동 전체 점포 ${card.total.toLocaleString('ko-KR')}곳 중` : ''],
    ['활동 사업체 변화',
      card.bizTrend?.growth == null ? '미확인' : `${card.bizTrend.growth > 0 ? '+' : ''}${card.bizTrend.growth}%`,
      '2018→2023 · 2018년 활동 사업체 분모']
  ]) {
    const row = node('div', null, 'row');
    row.append(node('span', label), node('b', value), node('small', note));
    summary.append(row);
  }
  const receipt = node('p',
    `제주데이터허브 793 상가(상권) 정보 · ${card.storeDate} 기준. 사업체는 830 통계(원 출처 KOSIS) · 2018~2023년 활동 사업체 기준이며 점포 목록과 집계 범위가 다릅니다.`,
    'receipt');
  receipt.style.cssText = 'margin:5px 0;color:#7c6c56;font-size:12px';
  summary.append(receipt);
  if (card.code === '50130320' && streetAvailable) {
    summary.append(button('표선농협하나로마트 주변 건물', openStreet));
  }
  article.append(summary);
}

function showPlace(code) {
  const area = areas.find(item => item.code === code);
  if (!area) return;
  if (focus !== code) {
    selectedPlace = null;
    currentWalk = null;
    currentBuilding = null;
    walkBadge.hidden = true;
    showSiteRadius(null);
  }
  focus = code;
  $('select').value = code;
  $('area').textContent = `제주특별자치도 / ${area.sgg}`;
  $('place').textContent = area.name;
  $('all').hidden = false;
  history.replaceState(null, '', `/dong/${code}`);
  drawMap();
  if (!frame.hidden) showStreet(code);
  else {
    viewSwitch.hidden = !streetAvailable;
    viewSwitch.textContent = '3D 거리';
  }
}

function clearFocus() {
  focus = null;
  candidateMapActive = false;
  comparison = null;
  selectedPlace = null;
  currentWalk = null;
  currentBuilding = null;
  walkBadge.hidden = true;
  if (profile) { profile.focus = null; profile.focusBuilding = null; }
  $('select').value = '';
  showSiteRadius(null);
  $('place').textContent = '제주 전도';
  $('area').textContent = '제주특별자치도';
  $('all').hidden = true;
  frame.hidden = true;
  frame.removeAttribute('src');
  $('maphead').hidden = false;
  $('mapbar').hidden = false;
  markerLayer.hidden = false;
  mapCredit.hidden = false;
  mapHint.hidden = false;
  viewSwitch.hidden = true;
  updateMapPresentation();
  history.replaceState(null, '', '/');
  drawMap();
}

function showNotebookMap() {
  candidateMapActive = notebookResults.length > 0;
  focus = null;
  currentWalk = null;
  currentBuilding = null;
  walkBadge.hidden = true;
  $('select').value = '';
  showSiteRadius(null);
  $('place').textContent = comparison ? '같은 조건 비교' : '제주 전도';
  $('area').textContent = comparison
    ? `제주특별자치도 · ${comparisonRows().map(row => row.name).join(' · ')}`
    : '제주특별자치도 · 답사 후보';
  $('all').hidden = true;
  if (!frame.hidden) showMapView();
  viewSwitch.hidden = true;
  history.replaceState(null, '', '/');
  drawMap();
}

async function send(text, dong = null) {
  if (busy || !contextReady || (!text && !dong)) return;
  const generation = contextGeneration;
  const requestMode = contextMode;
  const requestContextId = contextId;
  const controller = new AbortController();
  pendingController = controller;
  const area = areas.find(item => item.code === dong);
  busy = true;
  $('submit').disabled = true;
  if (area) showPlace(area.code);
  appendTurn('나', area ? `${area.name} 상권` : text);
  const article = appendTurn('상권하르방', '자료를 살펴보고 있습니다.');
  article.classList.add('busy');
  try {
    const body = {contextMode: requestMode, text};
    if (requestContextId) body.contextId = requestContextId;
    if (profile) body.profile = profile;
    if (dong) body.dong = dong;
    if (selectedPlace) body.selection = selectedPlace;
    const response = await fetch('/api/turn', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
      signal: controller.signal
    });
    if (!response.ok) throw new Error('server');
    const result = await response.json();
    if (generation !== contextGeneration) return;
    if (!['general', 'restricted'].includes(result.contextMode) ||
        result.contextId == null || result.contextId === '') {
      disableKakaoMap();
      contextReady = false;
      throw new Error('context-boundary');
    }
    if (result.contextMode === 'restricted' && contextMode !== 'restricted') {
      if (!enterRestrictedContext()) {
        contextReady = false;
        throw new Error('context-boundary');
      }
    } else if (result.contextMode !== contextMode || result.contextMode !== requestMode) {
      disableKakaoMap();
      contextReady = false;
      throw new Error('context-boundary');
    }
    contextId = String(result.contextId);
    contextReady = true;
    if (profile?.business !== result.profile?.business) showSiteRadius(null);
    profile = result.profile || null;
    void enableKakaoMap();
    if (frame.dataset.ready === 'true') {
      frame.contentWindow.postMessage({source: 'jeju-chat', business: profile?.business ?? null}, location.origin);
    }
    showProfile(result.slots, profile, result.businessName);
    const advice = result.advice;
    const hasNotebook = showNotebook(article, result.notebook);
    const populationInquiry = isPopulationInquiry(text);
    if (!hasNotebook) article.querySelector('p').textContent = result.question?.text || result.agentReply ||
        (area && result.card ? `${area.name}의 점포와 사업체 근거를 이 대화에 정리했습니다.` :
          advice?.explain || result.message);
    if (populationInquiry || Object.hasOwn(result, 'population')) {
      const population = result.card && Object.hasOwn(result.card, 'population')
        ? result.card.population
        : result.population;
      showPopulation(article, population, result.agent?.steps || [],
        result.focus || result.profile?.focus || result.card?.code || area?.code);
    }
    showTrace(article, result.agent?.steps);
    if (!hasNotebook && advice?.candidates?.length) {
      rank = advice.candidates;
      const actions = node('div', null, 'actions');
      for (const candidate of rank) {
        actions.append(button(`${candidate.letter} · ${candidate.name}`, () => choose(candidate.code)));
      }
      article.append(actions);
    }
    const target = result.focus || result.profile?.focus || result.card?.code ||
      (dong ? area?.code : populationInquiry ? focus : null);
    // 직접 입력한 동네 이름과 응답의 장소 의도가 같으면 후보 개요보다 그 동네를 보여 준다.
    const typedArea = dong ? null : typedAreaCode(text);
    const explicitPlace = Boolean(typedArea && target && String(target) === typedArea &&
      result.focusLevel !== 'island');
    const keepPlaceMap = Boolean(dong) || explicitPlace || (populationInquiry && Boolean(target));
    if (keepPlaceMap && target) {
      candidateMapActive = false;
      showPlace(target);
    } else if (hasNotebook && (notebookResults.length || comparison)) showNotebookMap();
    else if (target) showPlace(target);
    else if (!area) drawMap();
    const notebookResult = hasNotebook && result.notebook?.state === 'result';
    if (explicitPlace && notebookResult) {
      // 직접 입력한 동네가 이번 답이면 같은 턴의 후보 순위 목록 대신 그 동네 배경 카드를 보여 준다.
      article.querySelector('.candidate-list')?.remove();
      const placeName = areas.find(item => item.code === focus)?.name;
      if (placeName) {
        article.querySelector('p').textContent = `${placeName}의 읍면동 배경을 이 답사 카드에 정리했습니다. 지도도 같은 동네로 맞췄습니다.`;
      }
    }
    if (!populationInquiry && result.card?.code === focus && (!hasNotebook || explicitPlace)) {
      showPlaceSummary(article, result.card);
    }
    if (result.siteRadius) showSiteRadius(result.siteRadius);
    if (!hasNotebook && result.focusLevel === 'building' && result.focusTarget) {
      currentBuilding = result.focusTarget;
      if (frame.dataset.ready === 'true') {
        frame.contentWindow.postMessage({source: 'jeju-chat', building: currentBuilding}, location.origin);
      }
    }
    if (!hasNotebook && result.walk?.supported) {
      currentWalk = result.walk;
      walkBadge.hidden = false;
      if (frame.contentWindow) frame.contentWindow.postMessage({source: 'jeju-chat', walk: currentWalk}, location.origin);
    }
    const options = hasNotebook && result.notebook.state === 'question'
      ? result.notebook.choices || []
      : result.question?.options || [];
    showOptions(options.filter(option => !option.dong));
  } catch (error) {
    if (generation !== contextGeneration) return;
    if (error instanceof Error && ['context-id', 'context-boundary'].includes(error.message)) {
      contextReady = false;
      article.querySelector('p').textContent = '상담 공간을 확인하지 못했습니다. 새 상담으로 초기화해 주세요.';
      article.append(button('새 상담 재시도', () => startNewConsultation(true)));
    } else {
      article.querySelector('p').textContent = '응답을 받지 못했습니다. 질문은 대화에 남아 있습니다.';
      article.append(button('재시도', () => send(text, dong)));
    }
  } finally {
    if (generation === contextGeneration) {
      article.classList.remove('busy');
      if (pendingController === controller) pendingController = null;
      $('submit').disabled = false;
      busy = false;
      $('feed').scrollTop = $('feed').scrollHeight;
    }
  }
}

function choose(code) { if (code && !busy) send('', code); }
window.addEventListener('message', event => {
  if (frame.hidden || event.origin !== location.origin || event.source !== frame.contentWindow || event.data?.source !== 'jeju-3d') return;
  if (event.data.type === 'focus') {
    if (event.data.code !== focus) focusPlace(event.data.code);
    if (currentWalk?.supported) frame.contentWindow.postMessage({source: 'jeju-chat', walk: currentWalk}, location.origin);
  }
  if (event.data.type === 'selection') {
    selectedPlace = event.data.selection;
    showSiteRadius(event.data.siteRadius || null);
    $('question').placeholder = '이 장소가 내 가게에 왜 중요한가요?';
  }
  if (event.data.type === 'center' && event.data.code === focus) {
    streetCenters.set(focus, event.data.center);
    requestAnimationFrame(drawMarkers);
  }
  if (event.data.type === 'walk') {
    currentWalk = event.data.plan;
    walkBadge.hidden = !currentWalk?.supported;
  }
  if (event.data.type === 'far') {
    showMapView();
  }
});
async function openStreet() {
  if (streetAvailable) await import('./studio-street.js').then(module => module.openStreet());
}
new ResizeObserver(drawMarkers).observe($('mwrap'));

$('newConsultation').addEventListener('click', () => startNewConsultation(true));
$('select').addEventListener('change', event => focusPlace(event.target.value));
$('all').addEventListener('click', clearFocus);
$('form').addEventListener('submit', event => {
  event.preventDefault();
  const text = $('question').value.trim();
  if (!text || busy) return;
  $('question').value = '';
  send(text);
});
$('question').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $('form').requestSubmit();
  }
});
$('question').addEventListener('input', event => {
  event.target.style.height = 'auto';
  event.target.style.height = `${Math.min(event.target.scrollHeight, 150)}px`;
});

try {
  const districtRequest = fetch('/data/dongs.json').then(response => response.json());
  void fetch('studio-footprints.json', {method: 'HEAD'}).then(response => {
    streetAvailable = response.ok;
  }).catch(() => {});
  if (!contextId && !pendingController) {
    const generation = contextGeneration;
    void fetch('/api/turn', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({contextMode: 'general', resetContext: true}),
    }).then(response => response.ok ? response.json() : null).then(result => {
      if (generation !== contextGeneration || contextId || pendingController) return;
      if (result?.contextMode !== 'general' || typeof result.contextId !== 'string' || !result.contextId) return;
      contextId = result.contextId;
      void enableKakaoMap();
    }).catch(() => { /* The local map remains available. */ });
  }
  areas = await districtRequest;
  for (const area of [...areas].sort((a, b) => a.name.localeCompare(b.name, 'ko'))) {
    $('select').append(new Option(`${area.sgg} · ${area.name}`, area.code));
  }
  drawMap();
  const initial = /^\/dong\/(\d{8})$/.exec(location.pathname)?.[1] ||
    new URLSearchParams(location.search).get('dong');
  // 직접 링크도 지도만 옮긴다. 대화는 입력창에서 시작한다.
  if (areas.some(area => area.code === initial)) focusPlace(initial);
  await loadMapBase();
} catch {
  $('facts').hidden = false;
  $('hint').textContent = '행정경계 자료를 불러오지 못했습니다.';
}
