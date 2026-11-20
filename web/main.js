import * as THREE from "three";
import { OrbitControls } from "./vendor/OrbitControls.js";
import { Terrain, buildRoads, buildTrees, buildBuildings, fetchMapAsset } from "./terrain.js";

// ---------- 상수 ----------
const VIEW_HEIGHT = 62000; // 확대 1 일 때 화면 세로가 담는 미터
const STORE_SIZE = 13; // 점포 하나 = 13m 상자
const STORE_LEVEL = 11; // 같은 자리에 쌓인 점포 한 칸의 높이
const STORE_LIFT = 34; // 건물 위로 띄워 가려지지 않게
const STACK_CAP = 30;
const ZOOM_DONG = 4.5; // 이 확대부터 점포·건물을 그린다
const ZOOM_STREET = 16; // 이 확대부터 강조한 점포 이름을 띄운다
const ZOOM_MAX = 140;
const FLY_MS = 2000;
const COLOR = {
  same: "#5e6ad2", // 내 업종과 같은 가게 (강조색 하나)
  anchor: "#2f3238", // 마트·병원·약국·학원
  similar: "#9aa0ab", // 비슷한 업종
  other: "#c9ccd2", // 그 밖의 가게
  dim: "#e6e7ea", // 강조 밖
  selected: "#1b1c1f",
};
const TINT = { cand: ["#aab2ee", 0.22], out: ["#d4d6da", 0.4], focus: ["#5e6ad2", 0.26] };

// ---------- 상태 ----------
const state = {
  profile: null,
  business: null,
  highlight: null, // 후보 동네 코드 Set
  focus: null, // 보고 있는 동네 코드
  advice: null,
  fittedKey: null,
  selection: null, // {type:'store', dong, store} | {type:'building', dong, x, z}
  storeHighlight: null, // 질문으로 강조한 점포 순번 Set
  highlightLabel: "",
  walk: null,
  card: null, // 마지막 동네 카드
  cardMode: null, // 'dong' | 'store' | 'building'
  cardData: null,
  busy: false,
  lastRes: null,
};
const history = []; // 뒤로 가기용 장면 묶음
const debug = new URLSearchParams(location.search).has("debug");
const embedded = new URLSearchParams(location.search).has("embed") && window.parent !== window;
const linked = new URLSearchParams(location.search).get("dong");
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
if (embedded) document.body.classList.add("embedded");
const storeLevel = embedded ? 3 : STORE_LEVEL;
const streetOffset = new THREE.Vector3(0, 20000, 28000);
const metrics = { loadStart: performance.now(), bytes: 0, frames: 0, fps: 0, drawCalls: 0 };
const $ = (id) => document.getElementById(id);

// ---------- 장면 ----------
const stage = $("stage");
let renderer;
try {
  renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
} catch (error) {
  showMapError("이 브라우저에서 3D 지도를 시작하지 못했어요.");
  throw error;
}
renderer.setPixelRatio(1); // GTX 1060 에서도 일정하게
renderer.setSize(innerWidth, innerHeight);
stage.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color("#a8ccd4");
scene.add(new THREE.HemisphereLight("#ffffff", "#9fb3a8", embedded ? 1.45 : 2.0));
const sun = new THREE.DirectionalLight("#fff4e0", embedded ? 2.0 : 1.9);
sun.position.set(-30000, 60000, -35000);
scene.add(sun);

const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 10, 400000);
const HOME = { target: new THREE.Vector3(0, 0, 2000), offset: new THREE.Vector3(0, 52000, 70000), zoom: 1 };
camera.position.copy(HOME.target).add(HOME.offset);
// 섬 전체(동서 약 75km)가 가로에 들어오는 확대. 넓은 화면은 1, 좁은 화면은 그보다 작게
const homeZoom = () => Math.min(1, (VIEW_HEIGHT * (innerWidth / innerHeight)) / 80000);
camera.zoom = homeZoom();
function resizeCamera() {
  const aspect = innerWidth / innerHeight;
  camera.left = (-VIEW_HEIGHT * aspect) / 2;
  camera.right = (VIEW_HEIGHT * aspect) / 2;
  camera.top = VIEW_HEIGHT / 2;
  camera.bottom = -VIEW_HEIGHT / 2;
  camera.updateProjectionMatrix();
}
resizeCamera();

// 서울 3D 아틀라스와 같은 조작감: 휠·트랙패드 확대, 두 손가락 핀치, 끌어서 이동
const controls = new OrbitControls(camera, renderer.domElement);
controls.target.copy(HOME.target);
controls.enableDamping = true;
controls.dampingFactor = 0.07;
controls.maxPolarAngle = Math.PI * 0.465;
controls.minPolarAngle = 0.001;
controls.minZoom = Math.min(0.65, homeZoom() * 0.8);
controls.maxZoom = ZOOM_MAX;
controls.screenSpacePanning = false;
controls.rotateSpeed = 0.6;
controls.zoomSpeed = 0.85;
controls.zoomToCursor = true; // 휠을 굴린 자리로 다가간다
controls.mouseButtons = { LEFT: THREE.MOUSE.PAN, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: THREE.MOUSE.ROTATE };
controls.touches = { ONE: THREE.TOUCH.PAN, TWO: THREE.TOUCH.DOLLY_ROTATE };

const sea = new THREE.Mesh(new THREE.PlaneGeometry(400000, 400000), new THREE.MeshBasicMaterial({ color: "#a8ccd4" }));
sea.rotation.x = -Math.PI / 2;
sea.position.y = -5;
scene.add(sea);

let dongs = [];
let subcats = [];
let meta = null;
let dongInfo = []; // 서버의 동네 목록 (방향·시·읍면)
let terrain = null;
let roads = [];
let trees = null;
let buildings = null;
let buildingsReady;
const storeMeshes = [];
let storeRecords = null;
const labels = new Map();
const storeLabels = [];
const streetCenters = new Map();
let buildingCells;
const details = new Map(); // 동네 코드 -> {순번: [순번, 상호, 소분류, 주소]}

const marker = new THREE.Mesh(
  new THREE.RingGeometry(14, 22, 40),
  new THREE.MeshBasicMaterial({ color: COLOR.selected, side: THREE.DoubleSide, depthTest: false, transparent: true })
);
marker.rotation.x = -Math.PI / 2;
marker.renderOrder = 10;
marker.visible = false;
scene.add(marker);

async function fetchJson(url) {
  const r = await fetchMapAsset(url);
  const buf = await r.arrayBuffer();
  metrics.bytes += buf.byteLength;
  return JSON.parse(new TextDecoder().decode(buf));
}

function showMapError(message) {
  $("loadMessage").textContent = message;
  $("mapRetry").hidden = false;
  $("mapRetry").onclick = () => location.reload();
}

async function load() {
  [dongs, terrain] = await Promise.all([
    fetchJson("data/dongs.json"),
    Terrain.load(metrics),
  ]);
  $("mapCredit").title = `${terrain.info.sources.dem}\n${terrain.info.buildings.source}`;
  scene.add(terrain.buildMesh(embedded));
  buildBoundaries();
  buildLabels();
  if (dongIndex(linked) >= 0) flyToDong(linked, 0);
  metrics.loadMs = Math.round(performance.now() - metrics.loadStart);
  $("loading").classList.add("done");
  $("mapReference").hidden = false;
  applyScene();

  // Give the terrain a frame on its own before decoding or building detail layers.
  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  const note = $("mapLoadNote");
  let incomplete = false;
  const detail = (promise, add) => promise.then(add).catch((error) => {
    incomplete = true;
    console.warn("3D map detail layer:", error);
  });
  buildingsReady = detail(buildBuildings(terrain, metrics, embedded), (layer) => {
    buildings = layer;
    buildingCells = bucketPoints(layer.userData.buildingCenters, STREET_CLUSTER_RADIUS);
    streetCenters.clear();
    buildings.visible = lod === "dong";
    scene.add(buildings);
    if (buildings.userData.streetCenter) $("mapCredit").textContent += " · Overture Maps";
    requestRender();
  });
  const storesReady = detail(Promise.all([
    fetchJson("data/subcats.json"), fetchJson("data/meta.json"), fetchJson("api/dongs"),
    fetchMapAsset("data/stores.bin").then((r) => r.arrayBuffer()),
  ]), ([categories, metadata, list, buf]) => {
    subcats = categories;
    meta = metadata;
    dongInfo = list.dongs;
    buildPicker();
    metrics.bytes += buf.byteLength;
    buildStores(buf);
    storeMeshes.forEach((m) => { m.visible = lod === "dong"; });
    subcats.forEach((s, i) => /슈퍼마켓|의원|병원|약국|입시·교과학원/.test(s.name) && !/동물/.test(s.name) && anchorSubs.add(i));
    applyScene();
  });
  Promise.all([
    buildingsReady, storesReady,
    detail(buildRoads(terrain, metrics), (layers) => {
      roads = layers;
      scene.add(...roads);
      requestRender();
    }),
    detail(buildTrees(terrain, metrics, embedded), (layer) => {
      trees = layer;
      trees.visible = lod === "dong";
      scene.add(trees);
      requestRender();
    }),
  ]).then(() => {
    note.textContent = incomplete ? "일부 지도 정보를 불러오지 못했어요." : "";
    note.hidden = !incomplete;
  });
  return storesReady;
}

// ---------- 동네 고르기 목록 ----------
const GROUPS = [
  ["서귀포시", "읍·면"],
  ["서귀포시", "동"],
  ["제주시", "읍·면"],
  ["제주시", "동"],
];
let pickerGroup = 0;
function buildPicker() {
  const tabs = $("pickerTabs");
  tabs.innerHTML = "";
  GROUPS.forEach(([sgg, kind], i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.role = "tab";
    b.textContent = `${sgg} ${kind}`;
    b.onclick = () => {
      pickerGroup = i;
      buildPicker();
    };
    b.classList.toggle("on", i === pickerGroup);
    tabs.appendChild(b);
  });
  const [sgg, kind] = GROUPS[pickerGroup];
  $("dongList").innerHTML = "";
  for (const d of dongInfo.filter((x) => x.sgg === sgg && x.kind === kind)) {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.type = "button";
    b.className = "dongItem";
    b.dataset.code = d.code;
    b.innerHTML = `<b>${d.name}</b><span>${d.direction} · 점포 ${d.total.toLocaleString()}곳</span>`;
    b.onclick = () => openDong(d.code);
    li.appendChild(b);
    $("dongList").appendChild(li);
  }
}
$("changeDong").onclick = () => {
  $("picker").hidden = false;
  $("profile").hidden = true;
};

// ---------- 지형 위 그림들 ----------
function buildBoundaries() {
  const pts = [];
  for (const d of dongs) {
    for (const ring of d.rings) {
      for (let i = 0; i < ring.length; i++) {
        const [ax, az] = ring[i];
        const [bx, bz] = ring[(i + 1) % ring.length];
        pts.push(ax, terrain.heightAt(ax, az) + 10, az, bx, terrain.heightAt(bx, bz) + 10, bz);
      }
    }
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
  scene.add(new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ color: "#7d7868", transparent: true, opacity: 0.7 })));
}

// 점포: 동네마다 InstancedMesh 하나라 화면 밖 동네는 통째로 그리지 않는다
function buildStores(buf) {
  const view = new DataView(buf);
  const size = meta.storeRecordBytes;
  const n = buf.byteLength / size;
  const perDong = dongs.map(() => []);
  storeRecords = { sub: new Uint16Array(n), x: new Float32Array(n), z: new Float32Array(n), y: new Float32Array(n), perDong, slot: new Int32Array(n) };
  for (let i = 0; i < n; i++) {
    const o = i * size;
    const dIdx = view.getUint8(o + 13);
    storeRecords.slot[i] = perDong[dIdx].length;
    perDong[dIdx].push(i);
    const x = view.getFloat32(o, true);
    const z = view.getFloat32(o + 4, true);
    const stack = Math.min(view.getUint16(o + 8, true), STACK_CAP);
    storeRecords.x[i] = x;
    storeRecords.z[i] = z;
    storeRecords.y[i] = terrain.heightAt(x, z) + (embedded ? 13 : STORE_LIFT) + stack * storeLevel;
    storeRecords.sub[i] = view.getUint16(o + 10, true);
  }
  const sizeXZ = embedded ? 6 : STORE_SIZE;
  const geo = new THREE.BoxGeometry(sizeXZ, storeLevel * 0.86, sizeXZ);
  const mat = new THREE.MeshLambertMaterial();
  perDong.forEach((idxs, dIdx) => {
    const mesh = new THREE.InstancedMesh(geo, mat, idxs.length);
    mesh.userData.dIdx = dIdx;
    mesh.visible = false;
    scene.add(mesh);
    storeMeshes.push(mesh);
  });
  for (let d = 0; d < dongs.length; d++) layoutStores(d);
}

const anchorSubs = new Set();
function relationOf(sub) {
  if (state.business != null && sub === state.business) return "same";
  if (anchorSubs.has(sub)) return "anchor";
  if (state.business != null && subcats[sub].mid === subcats[state.business].mid) return "similar";
  return "other";
}

const _m = new THREE.Matrix4();
const _c = new THREE.Color();
function layoutStores(dIdx) {
  const mesh = storeMeshes[dIdx];
  const idxs = storeRecords.perDong[dIdx];
  const sel = state.selection && state.selection.type === "store" ? state.selection.store : -1;
  const hl = dongs[dIdx].code === state.focus ? state.storeHighlight : null;
  idxs.forEach((rec, k) => {
    let rel = relationOf(storeRecords.sub[rec]);
    let s = 1;
    if (hl) {
      if (hl.has(rec)) s = 1.7;
      else rel = "dim";
    }
    if (rec === sel) {
      rel = "selected";
      s = 2.2;
    }
    _m.makeScale(s, s, s);
    _m.setPosition(storeRecords.x[rec], storeRecords.y[rec] + (s - 1) * storeLevel * 0.43, storeRecords.z[rec]);
    mesh.setMatrixAt(k, _m);
    mesh.setColorAt(k, _c.set(COLOR[rel]));
  });
  mesh.instanceMatrix.needsUpdate = true;
  if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
  mesh.computeBoundingSphere();
}

function candidateOf(code) {
  return state.advice ? state.advice.candidates.find((c) => c.code === code) : null;
}
function buildLabels() {
  const root = $("labels");
  for (const d of dongs) {
    const el = document.createElement("div");
    el.className = "label pin";
    el.textContent = d.name;
    el.setAttribute("role", "button");
    // 2D 지도에서 이름표를 직접 눌러 고른다. 상담 후보면 같은 글자로 카드·채팅과 같은 동네를 연다.
    el.onclick = () => selectCandidate(d.code);
    root.appendChild(el);
    labels.set(d.code, el);
  }
  for (let i = 0; i < 16; i++) {
    const el = document.createElement("div");
    el.className = "label store";
    el.hidden = true;
    root.appendChild(el);
    storeLabels.push(el);
  }
}

// ---------- 확대 단계 ----------
let lod = null;
function updateLod() {
  const next = camera.zoom >= ZOOM_DONG ? "dong" : "island";
  if (next === lod) return;
  if (embedded && lod === "dong" && next === "island") {
    window.parent.postMessage({ source: "jeju-3d", type: "far" }, location.origin);
  }
  lod = next;
  storeMeshes.forEach((m) => (m.visible = lod === "dong"));
  if (trees) trees.visible = lod === "dong";
  if (buildings) buildings.visible = lod === "dong";
}

const tmp = new THREE.Vector3();
function project(x, y, z) {
  tmp.set(x, y, z).project(camera);
  if (tmp.x < -1.05 || tmp.x > 1.05 || tmp.y < -1.05 || tmp.y > 1.05) return null;
  return [((tmp.x + 1) / 2) * innerWidth, ((1 - tmp.y) / 2) * innerHeight];
}
// 이름표는 겹치지 않게 욕심껏 놓는다: 고른 동네 > 후보 > 점포가 많은 동네 순.
// 자리가 없으면 위·아래로 한 칸 비켜 보고, 그래도 겹치면 이 확대 단계에서는 숨긴다(왼쪽 목록에는 늘 있다).
const labelOrder = () =>
  [...dongs].sort((a, b) => {
    const r = (d) => (d.code === state.focus ? 0 : state.highlight && state.highlight.has(d.code) ? 1 : 2);
    return r(a) - r(b) || b.total - a.total;
  });
function updateLabels() {
  const street = camera.zoom >= ZOOM_STREET;
  const placed = [];
  const hits = (r) => placed.some((q) => r.l < q.r && r.r > q.l && r.t < q.b && r.b > q.t);
  for (const d of labelOrder()) {
    const el = labels.get(d.code);
    const cand = state.highlight && state.highlight.has(d.code);
    const show = !street && (lod === "dong" || cand || !state.highlight || d.code === state.focus);
    const p = show && project(d.center[0], terrain.heightAt(d.center[0], d.center[1]) + (lod === "island" ? 600 : 300), d.center[1]);
    if (!p) {
      el.style.display = "none";
      continue;
    }
    el.style.display = "block";
    el.classList.toggle("cand", !!cand || d.code === state.focus);
    const c = candidateOf(d.code);
    const text = c && !state.focus ? `${c.letter} ${d.name}` : d.name;
    if (el._t !== text) {
      el._t = text;
      el.textContent = text;
      el._w = 0;
    }
    if (!el._w) {
      el._w = el.offsetWidth;
      el._h = el.offsetHeight;
    }
    const w = el._w + 6;
    const h = el._h + 4;
    let spot = null;
    for (const dy of [0, -h, h]) {
      const r = { l: p[0] - w / 2, r: p[0] + w / 2, t: p[1] + dy - h, b: p[1] + dy };
      if (!hits(r)) {
        spot = [p[0], p[1] + dy, r];
        break;
      }
    }
    if (!spot) {
      el.style.display = "none";
      continue;
    }
    placed.push(spot[2]);
    el.style.left = `${spot[0]}px`;
    el.style.top = `${spot[1]}px`;
  }
  // 길 단위: 고른 점포와 강조한 점포 이름
  const names = [];
  if (street && state.focus && details.has(state.focus)) {
    const det = details.get(state.focus);
    const sel = state.selection && state.selection.type === "store" ? state.selection.store : null;
    const pool = sel != null ? [sel] : [];
    if (state.storeHighlight) pool.push(...[...state.storeHighlight].filter((i) => i !== sel));
    else if (state.business != null) pool.push(...storeRecords.perDong[dongIndex(state.focus)].filter((i) => storeRecords.sub[i] === state.business));
    for (const rec of pool) {
      if (names.length >= storeLabels.length * 3) break;
      const p = project(storeRecords.x[rec], storeRecords.y[rec] + 14, storeRecords.z[rec]);
      if (p && det[rec]) names.push([p, det[rec][1], rec === sel]);
    }
  }
  if (state.walk && state.walk.plan.supported) {
    const w = state.walk.plan;
    const r = w.routes.find((x) => x.ok && x.stop.ref === state.walk.active);
    const tags = [[w.dest.x, w.dest.z, `목적지(가정) ${w.dest.name}`]];
    if (r) tags.unshift([r.stop.x, r.stop.z, `정류장 ${r.stop.name}`]);
    for (const [x, z, t] of tags) {
      const p = project(x, terrain.heightAt(x, z) + 34, z);
      if (p) names.unshift([p, t, true]);
    }
  }
  // 점포 이름표도 겹치지 않게: 앞선 것(정류장·목적지·고른 가게)부터 놓고, 자리가 없으면 숨긴다
  let k = 0;
  for (const n of names) {
    if (k >= storeLabels.length) break;
    const el = storeLabels[k];
    el.hidden = false;
    el.textContent = n[1];
    el.classList.toggle("sel", n[2]);
    const w = el.offsetWidth + 4;
    const h = el.offsetHeight + 2;
    const r = { l: n[0][0] - w / 2, r: n[0][0] + w / 2, t: n[0][1] - h, b: n[0][1] };
    if (hits(r)) continue;
    placed.push(r);
    el.style.left = `${n[0][0]}px`;
    el.style.top = `${n[0][1]}px`;
    k++;
  }
  for (; k < storeLabels.length; k++) storeLabels[k].hidden = true;
  metrics.labelsShown = placed.length;
}

function dongIndex(code) {
  return dongs.findIndex((d) => d.code === code);
}

function applyScene() {
  $("mapPlace").textContent = dongs[dongIndex(state.focus || linked)]?.name || "제주";
  dongs.forEach((d, i) => {
    let t = null;
    if (state.highlight) t = state.highlight.has(d.code) ? TINT.cand : TINT.out;
    if (state.focus === d.code) t = TINT.focus;
    terrain.setTint(i, t && t[0], t ? t[1] : 0);
  });
  if (storeRecords) for (let d = 0; d < dongs.length; d++) layoutStores(d);
  const sel = state.selection;
  if (sel && sel.type === "store") {
    marker.visible = true;
    marker.position.set(storeRecords.x[sel.store], terrain.heightAt(storeRecords.x[sel.store], storeRecords.z[sel.store]) + 4, storeRecords.z[sel.store]);
  } else if (sel && sel.type === "building") {
    marker.visible = true;
    marker.position.set(sel.x, terrain.heightAt(sel.x, sel.z) + 4, sel.z);
  } else marker.visible = false;
  updateColorKey();
  requestRender();
}

function updateColorKey() {
  const el = $("colorKey");
  if (!state.focus) {
    el.hidden = true;
    return;
  }
  const biz = state.business != null ? subcats[state.business].name : null;
  const rows = [];
  if (biz) rows.push(["same", `내 업종 (${biz})`], ["similar", "비슷한 업종"]);
  rows.push(["anchor", "마트·병원·약국·학원"], ["other", "그 밖의 가게"]);
  if (state.selection) rows.push(["selected", "고른 곳"]);
  el.innerHTML = `<b>점포 색</b>` + rows.map(([k, t]) => `<span style="--c:${COLOR[k]}">${t}</span>`).join("") + (state.highlightLabel ? `<em>강조: ${state.highlightLabel}</em>` : "");
  el.hidden = false;
}

// ---------- 카메라 ----------
let flight = null;
function flyTo(target, zoom, offset, ms = FLY_MS) {
  if (reducedMotion.matches) ms = 0;
  flight = {
    t0: performance.now(),
    ms,
    fromTarget: controls.target.clone(),
    toTarget: target.clone(),
    fromOffset: camera.position.clone().sub(controls.target),
    toOffset: offset ? offset.clone() : camera.position.clone().sub(controls.target),
    fromZoom: camera.zoom,
    toZoom: zoom,
  };
  // Drain the previous gesture's inertia without changing the flight's endpoints.
  controls.enableDamping = false;
  controls.update();
  controls.enableDamping = true;
  controls.target.copy(flight.fromTarget);
  camera.position.copy(flight.fromTarget).add(flight.fromOffset);
  requestRender();
}
function stepFlight(now) {
  if (!flight) return;
  const t = flight.ms ? THREE.MathUtils.clamp((now - flight.t0) / flight.ms, 0, 1) : 1;
  const e = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  controls.target.lerpVectors(flight.fromTarget, flight.toTarget, e);
  const off = new THREE.Vector3().lerpVectors(flight.fromOffset, flight.toOffset, e);
  camera.position.copy(controls.target).add(off);
  camera.zoom = t === 1 ? flight.toZoom : Math.exp(Math.log(flight.fromZoom) + (Math.log(flight.toZoom) - Math.log(flight.fromZoom)) * e);
  camera.updateProjectionMatrix();
  if (t >= 1) flight = null;
}
// 패널에 가리지 않고 지도가 보이는 화면 영역(px). 카메라는 이 영역의 가운데에 맞춘다.
function openRect() {
  const W = innerWidth;
  const H = innerHeight;
  const box = (id) => {
    const el = $(id);
    if (!el || el.hidden || getComputedStyle(el).display === "none") return null;
    const r = el.getBoundingClientRect();
    return r.width && r.height ? r : null;
  };
  let l = 0, r = W, t = 0, b = H;
  const chat = box("chat");
  const card = box("card");
  if (embedded) {
    // The horizontal controls sit at the bottom, not beside the map.
    if (card) r = card.left - 12;
  } else if (W <= 760) {
    const sheet = [chat, card].filter(Boolean).map((x) => x.top);
    const tools = box("mapTools");
    if (tools) sheet.push(tools.top);
    if (sheet.length) b = Math.min(...sheet) - 8;
    const top = [box("left"), box("colorKey")].filter(Boolean).map((x) => x.bottom);
    if (top.length) t = Math.max(...top) + 8;
  } else {
    const left = box("left");
    if (left) l = left.right + 16;
    if (card) r = card.left - 16;
    else {
      const tools = box("mapTools");
      if (tools) r = tools.left - 16;
    }
    if (chat) b = chat.top - 16;
    const key = box("colorKey");
    if (key) t = key.bottom + 8;
  }
  if (r - l < 120) [l, r] = [0, W];
  if (b - t < 120) [t, b] = [0, H];
  return { l, r, t, b, W, H };
}
// (cx, cz)를 중심으로 spanX × spanZ 미터가 보이는 영역 안에 들어오게 옮긴다
function frame(cx, cz, spanX, spanZ, { offset = null, minZoom = controls.minZoom, maxZoom = ZOOM_MAX, zoom = null, ms = FLY_MS } = {}) {
  const off = offset || (flight ? flight.toOffset : camera.position.clone().sub(controls.target));
  const tilt = off.length() / Math.max(1, off.y); // 비스듬히 보면 땅의 앞뒤 거리가 화면에서 줄어든다
  const azimuth = Math.atan2(off.x, off.z);
  const sin = Math.sin(azimuth), cos = Math.cos(azimuth);
  const width = Math.abs(cos) * spanX + Math.abs(sin) * spanZ;
  const depth = Math.abs(sin) * spanX + Math.abs(cos) * spanZ;
  const R = openRect();
  const fit = Math.min(VIEW_HEIGHT * (R.r - R.l) / (R.H * width), VIEW_HEIGHT * tilt * (R.b - R.t) / (R.H * depth));
  const z = THREE.MathUtils.clamp(zoom ?? fit, minZoom, maxZoom);
  const metresPerPx = VIEW_HEIGHT / (z * R.H);
  const right = (R.W - R.l - R.r) / 2 * metresPerPx;
  const down = (R.H - R.t - R.b) / 2 * metresPerPx * tilt;
  const tx = cx + right * cos + down * sin;
  const tz = cz - right * sin + down * cos;
  flyTo(new THREE.Vector3(tx, terrain.heightAt(cx, cz), tz), z, off, ms);
}
// 후보 동네 2~3곳이 패널에 가리지 않는 영역 안에 함께 들어오게 옮긴다
function flyToCodes(codes) {
  const pts = dongs.filter((d) => codes.includes(d.code)).map((d) => d.center);
  if (!pts.length) return;
  const xs = pts.map((p) => p[0]);
  const zs = pts.map((p) => p[1]);
  const cx = (Math.max(...xs) + Math.min(...xs)) / 2;
  const cz = (Math.max(...zs) + Math.min(...zs)) / 2;
  frame(cx, cz, Math.max(...xs) - Math.min(...xs) + 30000, Math.max(...zs) - Math.min(...zs) + 16000, { offset: HOME.offset, maxZoom: ZOOM_DONG - 0.1 });
}
function seeStores(code) {
  const d = dongs.find((x) => x.code === code);
  if (!d) return;
  const [x, z] = d.storeCenter;
  frame(x, z, 1, 1, { zoom: Math.max(18, flight ? flight.toZoom : camera.zoom), ms: 900 });
}

const STREET_CLUSTER_RADIUS = 250;
function bucketPoints(points, step) {
  const cells = new Map();
  for (const point of points) {
    const key = `${Math.floor(point[0] / step)},${Math.floor(point[1] / step)}`;
    if (!cells.has(key)) cells.set(key, []);
    cells.get(key).push(point);
  }
  return cells;
}

function streetCenter(d) {
  if (!storeRecords) return d.storeCenter;
  if (streetCenters.has(d.code)) return streetCenters.get(d.code);
  const dIdx = dongIndex(d.code);
  const records = storeRecords.perDong[dIdx];
  const distinct = new Map();
  for (const i of records) {
    const point = [storeRecords.x[i], storeRecords.z[i]];
    distinct.set(point.join(","), point); // Co-located registrations are one place, not a cluster.
  }
  const radius = STREET_CLUSTER_RADIUS;
  const shops = bucketPoints(distinct.values(), radius);
  const countNearby = (cells, x, z) => {
    if (!cells) return 0;
    const ix = Math.floor(x / radius), iz = Math.floor(z / radius);
    let count = 0;
    for (let dx = -1; dx <= 1; dx++) {
      for (let dz = -1; dz <= 1; dz++) {
        for (const [px, pz] of cells.get(`${ix + dx},${iz + dz}`) || []) {
          if ((px - x) ** 2 + (pz - z) ** 2 <= radius ** 2) count++;
        }
      }
    }
    return count;
  };
  let best = d.storeCenter, bestScore = -1, bestDistance = Infinity;
  for (const points of bucketPoints(distinct.values(), 100).values()) {
    const [sx, sz] = points.reduce(([x, z], [px, pz]) => [x + px, z + pz], [0, 0]);
    const x = sx / points.length, z = sz / points.length;
    if (terrain.dongIndexAt(x, z) !== dIdx) continue;
    // Favor commercial activity with visible buildings without letting a large housing block dominate.
    const score = countNearby(shops, x, z) * (1 + Math.sqrt(countNearby(buildingCells, x, z)));
    const distance = (x - d.storeCenter[0]) ** 2 + (z - d.storeCenter[1]) ** 2;
    if (score > bestScore || (score === bestScore && distance < bestDistance)) {
      best = [x, z];
      bestScore = score;
      bestDistance = distance;
    }
  }
  streetCenters.set(d.code, best);
  return best;
}
// 동네를 고르면 그 동네 점포가 모인 곳을 보이는 영역 가운데에 놓고, 동네 크기에 맞춰 확대한다
function flyToDong(code, ms = FLY_MS) {
  const d = dongs.find((x) => x.code === code);
  if (!d) return;
  const span = Math.sqrt(d.area_km2 * 1e6) * 0.8;
  const pilot = embedded && code === "50130320" ? buildings?.userData.streetCenter : null;
  const [x, z] = pilot || (embedded ? streetCenter(d) : d.storeCenter);
  if (embedded) window.parent.postMessage({source: "jeju-3d", type: "center", code, center: [x, z]}, location.origin);
  frame(x, z, span, span, embedded
    ? { offset: streetOffset, zoom: pilot ? 82 : 55, ms }
    : { offset: new THREE.Vector3(0, 38000, 52000), minZoom: ZOOM_DONG + 0.5, maxZoom: 12, ms });
}
function flyToPoint(x, z, zoom) {
  frame(x, z, 1, 1, { zoom, ms: 1200 });
}
function zoomBy(f) {
  const z = THREE.MathUtils.clamp((flight?.toZoom ?? camera.zoom) * f, controls.minZoom, ZOOM_MAX);
  flyTo(flight?.toTarget ?? controls.target, z, flight?.toOffset, 350);
}
$("zoomIn").onclick = () => zoomBy(1.8);
$("zoomOut").onclick = () => zoomBy(1 / 1.8);
$("mapNorth").onclick = () => {
  const off = camera.position.clone().sub(controls.target);
  flyTo(controls.target, camera.zoom, new THREE.Vector3(0, off.y, Math.hypot(off.x, off.z)), 350);
};
$("mapRecenter").onclick = () => flyToDong(state.focus || linked);
$("home").onclick = () => {
  if (!terrain) return;
  actionId++;
  pushHistory();
  // 제주 전체로 나가면 가게 선택을 풀고, 그 가게에 묻던 질문 칩과 보행 경로도 함께 지운다
  clearWalk();
  state.selection = null;
  state.storeHighlight = null;
  state.highlightLabel = "";
  $("card").hidden = true;
  if (state.lastRes) {
    renderResponse(state.lastRes, { quiet: true });
    $("message").textContent = "제주 전체로 나왔어요. 고르셨던 가게 선택은 풀었어요. 후보 카드나 지도에서 다시 골라 주세요.";
  }
  setWorkLog([]);
  applyScene();
  flyTo(HOME.target, homeZoom(), HOME.offset);
};

// ---------- 그리기 (움직일 때만) ----------
let needsRender = true;
function requestRender() {
  needsRender = true;
}
controls.addEventListener("change", requestRender);
controls.addEventListener("start", () => {
  flight = null;
  requestRender();
});
const roadZoom = [0, 0, 0, 3, 10];
function updateMapReference() {
  const pxPerMetre = innerHeight * camera.zoom / VIEW_HEIGHT;
  roads.forEach((road, i) => { road.visible = camera.zoom >= roadZoom[i]; });
  const contourStep = camera.zoom >= ZOOM_STREET ? 20 : camera.zoom >= ZOOM_DONG ? 50 : 200;
  terrain.mesh.material.uniforms.contourStep.value = contourStep;
  $("mapContour").textContent = `${contourStep} m`;
  const max = 120 / pxPerMetre;
  const unit = 10 ** Math.floor(Math.log10(max));
  const distance = (max >= unit * 5 ? 5 : max >= unit * 2 ? 2 : 1) * unit;
  const scale = $("mapScale");
  scale.style.width = `${distance * pxPerMetre}px`;
  scale.textContent = distance >= 1000 ? `${distance / 1000} km` : `${distance} m`;
  // Project grid north using the camera basis; avoid a position-dependent bearing.
  camera.updateMatrixWorld();
  const m = camera.matrixWorld.elements;
  $("mapNeedle").style.transform = `rotate(${Math.atan2(-m[2], -m[6])}rad)`;
  $("mapRecenter").hidden = dongIndex(state.focus || linked) < 0;
  $("mapZoomBand").textContent = camera.zoom >= ZOOM_STREET ? "거리" : lod === "dong" ? "동네" : "섬 전체";
}
let lastFpsT = performance.now();
function loop(now) {
  requestAnimationFrame(loop);
  const wasFlying = flight !== null;
  stepFlight(now);
  const moved = controls.update();
  if (walker.visible && (needsRender || moved || wasFlying || !reducedMotion.matches)) {
    updateWalker(now);
    needsRender = true;
  }
  if (!needsRender && !moved && !wasFlying) return;
  needsRender = false;
  if (terrain) {
    updateLod();
    updateLabels();
    updateMapReference();
    marker.scale.setScalar(Math.max(1, 30 / camera.zoom));
  }
  renderer.render(scene, camera);
  metrics.frames++;
  metrics.renders = (metrics.renders || 0) + 1;
  metrics.drawCalls = renderer.info.render.calls;
  metrics.zoom = camera.zoom;
  if (now - lastFpsT > 1000) {
    metrics.fps = Math.round((metrics.frames * 1000) / (now - lastFpsT));
    metrics.frames = 0;
    lastFpsT = now;
    if (debug) {
      const el = $("metrics");
      el.hidden = false;
      el.textContent = `fps ${metrics.fps} · draw ${metrics.drawCalls} · load ${(metrics.bytes / 1e6).toFixed(2)}MB ${metrics.loadMs}ms · zoom ${camera.zoom.toFixed(1)} · ${lod}`;
    }
  }
}
requestAnimationFrame(loop);
window.__metrics = metrics;
window.__openRect = () => openRect();
if (debug) {
  window.__openDong = (name) => openDong(dongs.find((d) => d.name === name).code);
  window.__state = state;
  // 촬영 도구용: 지금 화면에 보이는 점포 하나의 화면 좌표
  window.__storeScreen = (rel) => {
    const idxs = storeRecords.perDong[dongIndex(state.focus)];
    let best = null;
    const o = openRect();
    for (const i of idxs) {
      if (rel && relationOf(storeRecords.sub[i]) !== rel) continue;
      const p = project(storeRecords.x[i], storeRecords.y[i], storeRecords.z[i]);
      if (!p || p[0] < o.l + 20 || p[0] > o.r - 20 || p[1] < o.t + 20 || p[1] > o.b - 20) continue;
      const d = Math.hypot(p[0] - (o.l + o.r) / 2, p[1] - (o.t + o.b) / 2);
      if (!best || d < best.d) best = { d, x: p[0], y: p[1], idx: i };
    }
    return best;
  };
  window.__orbitFps = (ms) =>
    new Promise((resolve) => {
      const start = performance.now();
      const r0 = metrics.renders || 0;
      const step = (now) => {
        const t = (now - start) / ms;
        const off = camera.position.clone().sub(controls.target);
        off.applyAxisAngle(THREE.Object3D.DEFAULT_UP, 0.004);
        camera.position.copy(controls.target).add(off);
        requestRender();
        if (t < 1) requestAnimationFrame(step);
        else resolve({ fps: Math.round(((metrics.renders - r0) * 1000) / (now - start)), drawCalls: metrics.drawCalls, zoom: camera.zoom, lod });
      };
      requestAnimationFrame(step);
    });
}

addEventListener("resize", () => {
  renderer.setSize(innerWidth, innerHeight);
  resizeCamera();
  requestRender();
});

// ---------- 누르기: 점포 > 건물 > 동네 ----------
const ray = new THREE.Raycaster();
let downAt = null;
renderer.domElement.addEventListener("pointerdown", (e) => (downAt = [e.clientX, e.clientY]));
renderer.domElement.addEventListener("pointerup", (e) => {
  if (!initialized || e.button !== 0 || !downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 6) return;
  const p = new THREE.Vector2((e.clientX / innerWidth) * 2 - 1, -(e.clientY / innerHeight) * 2 + 1);
  ray.setFromCamera(p, camera);
  if (lod === "dong") {
    const visible = storeMeshes.filter((m) => m.visible);
    const hit = ray.intersectObjects(visible, false)[0];
    if (hit && hit.instanceId != null) {
      const rec = storeRecords.perDong[hit.object.userData.dIdx][hit.instanceId];
      openStore(dongs[hit.object.userData.dIdx].code, rec);
      return;
    }
    const bHit = buildings && ray.intersectObject(buildings, true)[0];
    if (bHit) {
      const i = terrain.dongIndexAt(bHit.point.x, bHit.point.z);
      if (i >= 0) {
        openBuilding(dongs[i].code, bHit.point.x, bHit.point.z);
        return;
      }
    }
  }
  const hit = ray.intersectObject(terrain.mesh, false)[0];
  if (!hit) return;
  const i = terrain.dongIndexAt(hit.point.x, hit.point.z);
  if (i >= 0 && dongs[i].code !== state.focus) openDong(dongs[i].code);
});

// ---------- 뒤로 가기 ----------
function snapshot() {
  return {
    res: state.lastRes,
    focus: state.focus,
    selection: state.selection,
    storeHighlight: state.storeHighlight,
    highlightLabel: state.highlightLabel,
    cardMode: state.cardMode,
    cardData: state.cardData,
    cardHidden: $("card").hidden,
    message: $("message").innerHTML,
    target: controls.target.clone(),
    offset: camera.position.clone().sub(controls.target),
    zoom: camera.zoom,
  };
}
function pushHistory() {
  if (!state.lastRes) return;
  history.push(snapshot());
  if (history.length > 40) history.shift();
  $("back").disabled = false;
}
function goBack() {
  const s = history.pop();
  $("back").disabled = history.length === 0;
  if (!s) return;
  renderResponse(s.res, { quiet: true });
  state.focus = s.focus;
  state.selection = s.selection;
  state.storeHighlight = s.storeHighlight;
  state.highlightLabel = s.highlightLabel;
  $("message").innerHTML = s.message;
  if (s.cardHidden) $("card").hidden = true;
  else showCardMode(s.cardMode, s.cardData);
  applyScene();
  flyTo(s.target, s.zoom, s.offset, 900);
}
$("back").onclick = goBack;

// ---------- 서버와 주고받기 ----------
let status = { agent: { configured: false }, population: { live: false } };
let actionId = 0;
async function post(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(String(r.status));
  return r.json();
}

async function send({ text = "", dong = null, chip = false, ask = false }) {
  if (!storeRecords || state.busy) return;
  state.busy = true;
  pushHistory();
  const requestId = ++actionId;
  $("message").textContent = "살펴보는 중…";
  setWorkLog([]);
  try {
    const res = await post("api/turn", { profile: state.profile, text, dong, chip: chip && !ask, selection: state.selection });
    if (requestId !== actionId) return;
    const prevFocus = state.focus;
    const nextFocus = res.profile.focus || null;
    if (nextFocus !== prevFocus) {
      clearWalk();
      state.selection = null;
      state.storeHighlight = null;
      state.highlightLabel = "";
    }
    renderResponse(res, {});
    if (res.highlightStores) {
      state.storeHighlight = new Set(res.highlightStores);
      state.highlightLabel = res.highlightLabel;
      if (res.store) showCardMode("store", res.store);
      // 강조한 점포가 화면 밖이면 그쪽으로 옮긴다 (선택은 그대로)
      if (!state.selection && res.highlightStores.length) {
        const first = res.highlightStores[0];
        flyToPoint(storeRecords.x[first], storeRecords.z[first], Math.max(camera.zoom, 12));
      }
    } else if (res.card) {
      const moved = res.card.code !== prevFocus;
      if (moved) {
        state.selection = null;
        state.storeHighlight = null;
        state.highlightLabel = "";
      }
      if (moved || (state.cardMode !== "store" && state.cardMode !== "building")) showCardMode("dong", res.card);
      else state.card = res.card;
      if (moved) {
        updateColorKey();
        flyToDong(res.card.code);
      }
    }
    applyScene();
    if (embedded && state.focus) {
      window.parent.postMessage({ source: "jeju-3d", type: "focus", code: state.focus }, location.origin);
    }
  } catch (err) {
    if (requestId === actionId) {
      $("message").textContent = "서버와 연결이 끊겼어요. 실행 창이 켜져 있는지 확인해 주세요.";
      setWorkLog([]);
    }
  } finally {
    state.busy = false;
  }
}

function renderResponse(res, { quiet }) {
  const previousFocus = state.focus;
  state.lastRes = res;
  const focusCode = res.profile.focus;
  $("studioLink").href = focusCode ? `studio.html?dong=${focusCode}` : "studio.html";
  state.profile = res.profile;
  state.business = res.scene.business;
  state.focus = res.profile.focus || null;
  const anyCondition = res.profile.business != null || res.profile.region || res.profile.area;
  state.highlight = anyCondition && !state.focus && Array.isArray(res.candidates) ? new Set(res.candidates) : null;
  if (!quiet) {
    const lines = [];
    if (res.agentReply) lines.push(`<span class="reply">${escapeHtml(res.agentReply)}</span>`);
    lines.push(escapeHtml(res.message));
    $("message").innerHTML = lines.join(" ");
    setWorkLog(workItems(res));
  }
  renderProfile(res);
  state.advice = res.advice && Array.isArray(res.advice.candidates) && res.advice.candidates.length ? res.advice : null;
  if (!state.advice && state.cardMode === "advice") {
    $("card").hidden = true;
    state.cardMode = null;
    state.cardData = null;
  }
  renderCandBar();
  $("chips").innerHTML = "";
  for (const c of res.chips) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = c.label;
    if (c.dong) b.className = "dong";
    b.onclick = () => (c.code ? selectCandidate(c.code) : send({ text: c.text, chip: true, ask: !!c.ask }));
    $("chips").appendChild(b);
  }
  if (state.focus) {
    loadDetails(state.focus);
    $("picker").hidden = true;
  } else if (state.advice) {
    showCardMode("advice", state.advice);
    const codes = state.advice.candidates.map((c) => c.code);
    const key = codes.join(",");
    if (!quiet && (previousFocus || key !== state.fittedKey)) {
      state.fittedKey = key;
      flyToCodes(codes);
    }
  }
}

// 이번 차례에 서버가 실제로 한 일만 적는다 (응답에 담긴 사실에서만 만든다)
function workItems(res) {
  const items = [];
  const calls = res.agent && Array.isArray(res.agent.calls) ? res.agent.calls : [];
  if (calls.length) items.push("상담 도구를 사용했어요.");
  const candidates = res.advice && Array.isArray(res.advice.candidates) ? res.advice.candidates : [];
  if (candidates.length && !res.card) {
    const date = candidates[0].storeDate;
    items.push(`후보 동네 ${candidates.length}곳을 제안했어요${date ? ` (${date} 상가 정보 기준)` : ""}.`);
  }
  if (res.card) items.push(`${res.card.name} 동네 정보를 확인했어요.`);
  if (Array.isArray(res.highlightStores)) items.push(`점포 ${res.highlightStores.length}곳을 지도에 표시했어요.`);
  return items;
}
// 좁은 화면에서 카드가 대화 위에 앉도록 대화 높이를 CSS 변수로 알린다
new ResizeObserver(() => document.documentElement.style.setProperty("--chatH", `${$("chat").offsetHeight}px`)).observe(document.getElementById("chat"));

function setWorkLog(items) {
  const el = $("workLog");
  el.hidden = !items.length;
  el.innerHTML = items.length ? `<b>한 일</b>${items.map((x) => `<span>${escapeHtml(x)}</span>`).join("")}` : "";
}

// 동네 안에 들어가도 상담에서 받은 후보 A/B/C를 대화창 위에 남겨, 상담·후보·지도를 한 화면에서 오간다
function renderCandBar() {
  const bar = $("candBar");
  const cands = state.focus && state.advice ? state.advice.candidates : [];
  bar.hidden = !cands.length;
  bar.innerHTML = cands.length
    ? `<span class="t">후보</span>` +
      cands
        .map((c) => `<button type="button" data-code="${c.code}" class="${c.code === state.focus ? "on" : ""}"><span class="letter">${c.letter}</span>${escapeHtml(c.name)}</button>`)
        .join("")
    : "";
  bar.querySelectorAll("button").forEach((b) => (b.onclick = () => selectCandidate(b.dataset.code)));
}

function selectCandidate(code) {
  if (code) openDong(code);
}

function renderProfile(res) {
  const rows = [];
  if (state.focus) {
    const info = dongInfo.find((d) => d.code === state.focus);
    rows.push(["보는 동네", `${info.name} (${info.sgg}, ${info.direction})`]);
  }
  for (const s of res.slots) if (s.filled) rows.push([s.title, s.value]);
  $("profile").hidden = rows.length === 0;
  $("slots").innerHTML = rows.map(([t, v]) => `<li><span class="t">${t}</span><span class="v">${escapeHtml(v)}</span></li>`).join("");
}

async function loadDetails(code) {
  if (details.has(code)) return;
  details.set(code, {});
  const rows = await fetchJson(`data/stores/${code}.json`);
  const map = {};
  for (const r of rows) map[r[0]] = r;
  details.set(code, map);
  requestRender();
}

function openDong(code) {
  return send({ dong: code });
}

async function openStore(code, rec) {
  pushHistory();
  const requestId = ++actionId;
  clearWalk();
  state.storeHighlight = null;
  state.highlightLabel = "";
  state.selection = { type: "store", dong: code, store: rec };
  $("message").textContent = "점포 정보를 살펴보는 중…";
  setWorkLog([]);
  try {
    const s = await post("api/store", { profile: state.profile, dong: code, idx: rec });
    if (requestId !== actionId) return;
    showCardMode("store", s);
    setWorkLog([`점포 정보: ${s.name}을 확인했어요.`]);
    $("message").textContent = `${s.name}(${s.sub})을(를) 골랐어요. "여기 왜 표시됐어요?", "근처 약국 어디 있어요?"처럼 물어보세요.`;
    setFollowupChips();
  } catch {
    if (requestId === actionId) {
      $("message").textContent = "이 점포 정보를 찾지 못했어요.";
      setWorkLog([]);
    }
  }
  applyScene();
}

async function openBuilding(code, x, z) {
  pushHistory();
  const requestId = ++actionId;
  clearWalk();
  state.storeHighlight = null;
  state.highlightLabel = "";
  state.selection = { type: "building", dong: code, x, z };
  $("message").textContent = "건물 주변 점포를 살펴보는 중…";
  setWorkLog([]);
  try {
    const nb = await post("api/nearby", { profile: state.profile, dong: code, x, z });
    if (requestId !== actionId) return;
    showCardMode("building", nb);
    $("message").textContent = nb.count ? `이 건물 주변 점포 ${nb.count}곳을 찾았어요. 목록에서 눌러 보세요.` : "이 건물 주변에는 등록된 점포가 없어요.";
    setWorkLog([`건물 주변 점포 ${nb.count}곳을 확인했어요.`]);
  } catch {
    if (requestId === actionId) {
      $("message").textContent = "건물 주변 점포를 찾지 못했어요.";
      setWorkLog([]);
    }
  }
  applyScene();
}

function setFollowupChips() {
  $("chips").innerHTML = "";
  const qs = ["여기 왜 표시됐어요?", "근처 같은 업종 가게", "근처 마트·병원·약국"];
  const biz = state.business != null ? subcats[state.business].name : null;
  for (const q of qs) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = q;
    const text = q === "근처 같은 업종 가게" ? (biz ? `근처 ${biz} 가게 어디` : "여기 근처 같은 업종 가게") : q === "근처 마트·병원·약국" ? "근처 마트 병원 약국" : q;
    b.onclick = () => send({ text, chip: true, ask: true });
    $("chips").appendChild(b);
  }
}

// ---------- 오른쪽 정보 창 ----------
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[ch]);
}
function fmtDate(d) {
  return d ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}` : "";
}
const REL_TEXT = { same: "내 업종과 같은 가게", anchor: "손님을 끌어오는 이웃", similar: "비슷한 업종", other: "그 밖의 가게" };

function showCardMode(mode, data) {
  state.cardMode = mode;
  state.cardData = data;
  if (mode === "dong") {
    state.card = data;
    $("cardBody").innerHTML = dongCardHtml(data);
  } else if (mode === "advice") {
    $("cardBody").innerHTML = adviceCardHtml(data);
    $("cardBody").querySelectorAll("[data-letter]").forEach((b) => (b.onclick = () => send({ text: `${b.dataset.letter} 후보 보기`, chip: true })));
  } else if (mode === "store") {
    $("cardBody").innerHTML = storeCardHtml(data);
  } else {
    $("cardBody").innerHTML = buildingCardHtml(data);
    $("cardBody").querySelectorAll("[data-idx]").forEach((b) => (b.onclick = () => openStore(data.dong, Number(b.dataset.idx))));
  }
  const wb = $("cardBody").querySelector("#walkBtn");
  if (wb) wb.onclick = () => openWalk();
  const zb = $("cardBody").querySelector("#seeStores");
  // 카메라가 아직 날아가는 중이어도 목적 좌표(이 동네 점포 중심)를 명시해 확대한다
  if (zb) zb.onclick = () => seeStores(data.code);
  $("cardBack").hidden = mode === "dong" || mode === "advice" || !state.card;
  $("card").hidden = embedded && (mode === "dong" || mode === "advice");
  if (embedded && (mode === "store" || mode === "building")) {
    window.parent.postMessage({
      source: "jeju-3d", type: "selection", selection: state.selection, siteRadius: data.siteRadius,
    }, location.origin);
  }
}
let pendingWalk = null;
let pendingBuilding = null;
let pendingCode = null;
let initialized = false;
let pendingBusiness;
async function focusEmbeddedBuilding(target) {
  if (typeof target !== "string") return;
  const match = /^(\d{8}):(store|new):(0|[1-9]\d{0,7})$/.exec(target);
  if (!match) return;
  const [, code, layer, suffix] = match;
  const idx = Number(suffix);
  const dIdx = dongIndex(code);
  if (dIdx < 0) return;
  if (layer === "new") await buildingsReady;
  const point = layer === "store" && storeRecords.perDong[dIdx].includes(idx)
    ? [storeRecords.x[idx], storeRecords.z[idx]]
    : layer === "new" && code === "50130320" ? buildings?.userData.roofPoints?.[idx] : null;
  if (!point) return;
  if (state.focus !== code) await openDong(code);
  if (state.focus !== code) return;
  const [x, z] = point;
  if (layer === "store") await openStore(code, idx);
  else await openBuilding(code, x, z);
  frame(x, z, 1, 1, { offset: streetOffset, zoom: 120, ms: 900 });
}
window.addEventListener("message", async (event) => {
  if (!embedded || event.origin !== location.origin || event.source !== window.parent) return;
  if (event.data?.source !== "jeju-chat") return;
  const { code, building, walk, business } = event.data;
  if (business !== undefined) pendingBusiness = business;
  if (!initialized) {
    if (code) pendingCode = code;
    if (building) pendingBuilding = building;
    if (walk?.supported) pendingWalk = walk;
    return;
  }
  if (business !== undefined) {
    state.profile = {...state.profile, business};
    state.business = business;
    applyScene();
  }
  if (code && code !== state.focus) await openDong(code);
  if (building) await focusEmbeddedBuilding(building);
  if (walk?.supported) showEmbeddedWalk(walk);
});
$("cardBack").onclick = () => {
  pushHistory();
  clearWalk();
  state.selection = null;
  state.storeHighlight = null;
  state.highlightLabel = "";
  showCardMode("dong", state.card);
  applyScene();
};
$("cardClose").onclick = () => {
  pushHistory();
  $("card").hidden = true;
  state.selection = null;
  applyScene();
};

function adviceCardHtml(a) {
  const cards = a.candidates
    .map(
      (c) => `<article class="cand">
      <h3><span class="letter">${c.letter}</span>${escapeHtml(c.name)} <small>${escapeHtml(c.sgg)} · ${c.kind}</small></h3>
      <p class="style">${escapeHtml(c.style)}</p>
      <h4>이 기준에서 앞선 이유</h4><ul>${c.why.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>
      <h4>주의할 점</h4><ul>${c.cautions.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul>
      <button type="button" class="wide" data-letter="${c.letter}">${escapeHtml(c.name)} 들어가 보기</button>
    </article>`,
    )
    .join("");
  const date = a.candidates[0] ? a.candidates[0].storeDate : "";
  return `<h2>${escapeHtml(a.basisTitle)}</h2>
    <p class="sub">${escapeHtml(a.explain)}</p>
    ${cards}
    <p class="src">상가(상권)정보 ${date} 기준, 43개 읍면동 비교. 비중의 분모는 같은 동네 음식점 수예요. 유동인구·임대료·매출은 셈하지 않았어요.</p>`;
}

function populationHtml(p) {
  if (!p) return `<p>오가는 사람 자료는 아직 연결 전이에요. 평일과 주말 낮에 직접 서 있어 보세요.</p>`;
  return `<dl class="stat">
      <dt>주민</dt><dd>${p.residentShare}%</dd>
      <dt>방문객</dt><dd>${p.visitShare}%</dd>
      <dt>40~60대</dt><dd>${p.midAgeShare}%</dd>
    </dl>
    <p class="src">제주데이터허브 읍면동 유동인구 ${fmtDate(p.date)} 하루치 (실시간 아님)</p>`;
}

function dongCardHtml(c) {
  const b = c.business;
  const info = dongInfo.find((d) => d.code === c.code);
  const tops = c.topSubs.map((t) => `<li>${t.name} <b>${t.n}곳</b> <span class="muted">(${((100 * t.n) / c.total).toFixed(1)}%)</span></li>`).join("");
  const checks = c.fieldChecks.map((x) => `<li>${x}</li>`).join("");
  const reasons = c.reasons.map((x) => `<li>${x}</li>`).join("");
  const anchors = Object.entries(c.anchors).map(([k, v]) => `${k} ${v}`).join(", ") || "없음";
  return `
    <h2>${c.name}</h2>
    <p class="sub">${c.sgg} · ${info ? info.direction : ""} · 면적 ${c.areaKm2}㎢</p>
    <div class="big-number"><span>전체 점포</span><b>${c.total.toLocaleString()}곳</b><small>${c.storeDate} 기준</small></div>
    ${
      b
        ? `<div class="big-number mine"><span>${b.name}</span><b>${b.count}곳</b><small>${c.total.toLocaleString()}곳 중 ${b.share}% · 섬 평균 ${b.islandShare}%</small></div>
           ${b.saturated ? `<p class="warn">같은 가게가 섬 평균의 1.5배가 넘어요.</p>` : ""}`
        : `<h3>많은 업종 (${c.total.toLocaleString()}곳 중)</h3><ul class="tops">${tops}</ul>`
    }
    <button id="seeStores" type="button" class="wide">점포 하나하나 보기 (확대)</button>
    <h3>손님을 끌어오는 이웃</h3>
    <p>마트·병원·약국·학원 <b>${c.anchorTotal}곳</b> <span class="muted">(동네 중간값 ${c.anchorMedian}곳)</span></p>
    <p class="muted">${anchors}</p>
    <h3>사업체가 늘었나</h3>
    <dl class="stat">
      <dt>2018 → 2023</dt><dd>${c.bizTrend.active2018.toLocaleString()} → ${c.bizTrend.active2023.toLocaleString()}곳</dd>
      <dt>늘어난 비율</dt><dd>${c.bizTrend.growth}% <span class="muted">(섬 ${c.bizTrend.islandGrowth}%)</span></dd>
    </dl>
    <h3>누가 오가나</h3>
    ${populationHtml(c.population)}
    ${reasons ? `<h3>이 동네를 볼 이유</h3><ul>${reasons}</ul>` : ""}
    <h3>현장에서 확인할 것</h3><ul>${checks}</ul>
    <p class="src">출처: 제주데이터허브 상가(상권) 정보 ${c.storeDate} 기준, 읍면동별 사업체 생멸 통계(KOSIS). 숫자는 계산 도구가 직접 셌어요.</p>`;
}

function storeCardHtml(s) {
  return `
    <p class="kicker" style="--c:${COLOR[s.relation]}">${REL_TEXT[s.relation]}</p>
    <h2>${escapeHtml(s.name)}</h2>
    <p class="sub">${escapeHtml(s.sub)} · ${escapeHtml(s.cat)} · ${escapeHtml(s.address)}</p>
    <h3>내 가게와의 관계</h3><ul>${s.why.map((x) => `<li>${x}</li>`).join("")}</ul>
    <h3>현장에서 확인할 것</h3><ul>${s.checks.map((x) => `<li>${x}</li>`).join("")}</ul>
    <p class="note">${s.buildingNote}</p>
    <button id="walkBtn" type="button" class="wide">정류장에서 걸어 보기 (가정 경로)</button>
    <p class="src">출처: ${s.source}</p>`;
}

// ---------- 가정 보행 미리보기: 실제 정류장 → 가정한 목적지, 고른 가게 옆을 지나는가 ----------
const walkGroup = new THREE.Group();
walkGroup.renderOrder = 9;
scene.add(walkGroup);
const WALK_COLOR = { active: "#5e6ad2", other: "#8a8f98", stop: "#1b1c1f", dest: "#5e6ad2" };
const walker = new THREE.Group();
walker.renderOrder = 11;
walker.visible = false;
scene.add(walker);
const walkerMaterial = new THREE.MeshBasicMaterial({ color: "#193e63", depthTest: false, depthWrite: false });
const head = new THREE.Mesh(new THREE.SphereGeometry(5, 10, 8), walkerMaterial);
head.position.y = 29;
const body = new THREE.Mesh(new THREE.CylinderGeometry(3, 4, 17, 8), walkerMaterial);
body.position.y = 17;
walker.add(head, body);
const limbs = [[-4, 9, 15], [4, 9, 15], [-5, 22, 12], [5, 22, 12]].map(([x, y, length]) => {
  const pivot = new THREE.Group();
  pivot.position.set(x, y, 0);
  const part = new THREE.Mesh(new THREE.CylinderGeometry(2, 2, length, 6), walkerMaterial);
  part.position.y = -length / 2;
  pivot.add(part);
  walker.add(pivot);
  return pivot;
});
let walkTrack = null;
reducedMotion.addEventListener("change", requestRender);

function updateWalker(now) {
  const { points, length, start } = walkTrack;
  const distance = reducedMotion.matches ? length / 2 : (Math.max(0, now - start) / 18000 % 1) * length;
  let i = 1;
  while (i < points.length - 1 && distance > points[i].distance) i++;
  const a = points[i - 1], b = points[i];
  const t = (distance - a.distance) / (b.distance - a.distance);
  const x = THREE.MathUtils.lerp(a.x, b.x, t), z = THREE.MathUtils.lerp(a.z, b.z, t);
  walker.position.set(x, terrain.heightAt(x, z) + 5, z);
  walker.rotation.y = Math.atan2(b.x - a.x, b.z - a.z);
  walker.scale.setScalar(Math.max(1, 24 * VIEW_HEIGHT / (34 * innerHeight * camera.zoom)));
  const swing = reducedMotion.matches ? 0 : Math.sin(now / 230) * 0.45;
  limbs.forEach((limb, j) => { limb.rotation.x = swing * (j % 2 ? -1 : 1); });
}

function resetWalkDrawing() {
  for (const c of [...walkGroup.children]) {
    walkGroup.remove(c);
    c.geometry.dispose();
    c.material.dispose();
  }
  walkTrack = null;
  walker.visible = false;
  $("mapWalkNote").hidden = true;
  requestRender();
}

function clearWalk() {
  resetWalkDrawing();
  state.walk = null;
}

function walkTube(points, color, radius) {
  const curve = new THREE.CurvePath();
  const v = points.map(([x, z]) => new THREE.Vector3(x, terrain.heightAt(x, z) + 3, z));
  for (let i = 0; i < v.length - 1; i++) curve.add(new THREE.LineCurve3(v[i], v[i + 1]));
  const geo = new THREE.TubeGeometry(curve, Math.max(8, v.length * 2), radius, 6, false);
  const mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ color, depthTest: false, transparent: true, opacity: 0.95 }));
  mesh.renderOrder = 9;
  return mesh;
}
function walkPin(x, z, color) {
  const m = new THREE.Mesh(new THREE.CylinderGeometry(7, 7, 30, 20), new THREE.MeshBasicMaterial({ color, depthTest: false }));
  m.position.set(x, terrain.heightAt(x, z) + 15, z);
  m.renderOrder = 10;
  return m;
}

function drawWalk() {
  resetWalkDrawing();
  const w = state.walk;
  if (!w || !w.plan.supported) return;
  const ok = w.plan.routes.filter((r) => r.ok);
  for (const r of ok) if (r.stop.ref !== w.active) walkGroup.add(walkTube(r.path, WALK_COLOR.other, 2.5));
  const act = ok.find((r) => r.stop.ref === w.active);
  if (act) {
    walkGroup.add(walkTube(act.path, WALK_COLOR.active, 4.5));
    walkGroup.add(walkPin(act.stop.x, act.stop.z, WALK_COLOR.stop));
    const points = [];
    let length = 0;
    for (const [x, z] of act.path) {
      const last = points[points.length - 1];
      if (last && last.x === x && last.z === z) continue;
      if (last) length += Math.hypot(x - last.x, z - last.z);
      points.push({ x, z, distance: length });
    }
    if (length > 0) {
      walkTrack = { points, length, start: performance.now() };
      walker.visible = true;
      $("mapWalkNote").hidden = false;
    }
  }
  walkGroup.add(walkPin(w.plan.dest.x, w.plan.dest.z, WALK_COLOR.dest));
  requestRender();
}

function frameWalk(route) {
  const xs = route.path.map(([x]) => x);
  const zs = route.path.map(([, z]) => z);
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2;
  const cz = (Math.min(...zs) + Math.max(...zs)) / 2;
  const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...zs) - Math.min(...zs), 200);
  frame(cx, cz, span * 1.3, span * 1.3, { offset: embedded ? streetOffset : null, minZoom: 12, ms: 900 });
}

function showEmbeddedWalk(plan) {
  const route = plan.routes.find((r) => r.ok);
  state.walk = { plan, active: route?.stop.ref };
  drawWalk();
  if (route) frameWalk(route);
}

function storeVerdict(st) {
  if (!st) return "";
  if (st.passes === null) return `판단 못 함: 가게 표시점(주소 좌표)이 OSM 길에서 ${st.snapM}m 떨어져 있어요. 경로와의 거리는 ${st.distanceM}m예요.`;
  if (st.passes) return `가게 표시점에서 ${st.distanceM}m 옆을 지나요. 출입구가 그쪽인지는 확인하지 못했어요.`;
  return `가게 표시점에서 ${st.distanceM}m 떨어져 지나가요.` + (st.detourM != null ? ` 가게에 들렀다 가면 ${st.detourM}m 더 걸어요.` : "");
}

function walkCardHtml(p, active) {
  if (!p.supported) return `<h2>정류장에서 걸어 보기</h2><p>${escapeHtml(p.why)}</p>`;
  const rows = p.routes
    .map((r) => {
      if (!r.ok) return `<li><b>${escapeHtml(r.stop.name)}</b> (${r.stop.ref}) ${escapeHtml(r.why)}</li>`;
      const kinds = Object.entries(r.kinds).map(([k, m]) => `${k} ${Math.round(m)}m`).join(", ");
      const same = r.sameAsRef ? `<p class="muted">길 건너 정류장(${r.sameAsRef})과 같은 길 지점에 붙어 경로가 같아요. 길을 건너는지는 확인하지 못했어요.</p>` : "";
      return `<li class="walkRoute${r.stop.ref === active ? " on" : ""}"><button type="button" data-ref="${r.stop.ref}">
        <b>${escapeHtml(r.stop.name)} 정류장 <span class="muted">${r.stop.ref}</span></b>
        <span>${r.lengthM.toLocaleString()}m · 약 ${r.minutes}분 · 횡단보도 ${r.crossings}곳</span></button>
        <p>${storeVerdict(r.store)}</p><p class="muted">길 구성: ${kinds}. 정류장 표시점에서 길까지 ${r.snapM.stop}m, 목적지까지 ${r.snapM.dest}m.</p>${same}</li>`;
    })
    .join("");
  const dests = p.destOptions
    .map((d) => `<button type="button" class="opt${d.idx === p.dest.idx ? " on" : ""}" data-dest="${d.idx}">${escapeHtml(d.name)}</button>`)
    .join("");
  const stopNames = [...new Set(p.stopOptions.map((s) => s.name))];
  const stops = stopNames.map((n) => `<button type="button" class="opt" data-stop="${escapeHtml(n)}">${escapeHtml(n)}</button>`).join("");
  return `<p class="kicker" style="--c:${WALK_COLOR.active}">가정 보행 미리보기</p>
    <h2>${escapeHtml(p.store.name)} 앞을 지나갈까요?</h2>
    <p class="sub">정류장 → <b>${escapeHtml(p.dest.name)}</b>(가정한 목적지)까지 걸어갈 때</p>
    <p class="note">${escapeHtml(p.assumption)} ${escapeHtml(p.speed)}.</p>
    <ul class="nearby walk">${rows}</ul>
    <h3>목적지 바꾸기</h3><div class="opts">${dests}</div>
    <h3>정류장 바꾸기</h3><div class="opts">${stops}</div>
    <p class="src">길·정류장: ${escapeHtml(p.source.source)}, ${escapeHtml(p.source.osmBase || "")} 기준. 가게·목적지 위치: 제주데이터허브 상가(상권) 정보. 유동인구 관측 아님.</p>`;
}

async function openWalk(opts = {}) {
  const sel = state.selection;
  if (!sel || sel.type !== "store") return;
  const prev = state.walk && state.walk.plan;
  const body = { dong: sel.dong, store: sel.store, dest: opts.dest ?? (prev && prev.dest ? prev.dest.idx : null), stops: opts.stops ?? null };
  const p = await post("api/walk", body);
  const first = p.supported ? p.routes.find((r) => r.ok) : null;
  state.walk = { plan: p, active: opts.active || (first && first.stop.ref) };
  if (embedded) window.parent.postMessage({ source: "jeju-3d", type: "walk", plan: p }, location.origin);
  renderWalkCard();
  drawWalk();
  if (first) {
    const r = p.routes.find((x) => x.stop.ref === state.walk.active) || first;
    frameWalk(r);
    $("message").textContent = `${r.stop.name}${r.stop.name.includes("정류장") ? "" : " 정류장"}에서 ${p.dest.name}까지 약 ${r.lengthM.toLocaleString()}m(가정 경로)예요. ${storeVerdict(r.store)}`;
    setWorkLog([`가정 경로: OSM 길 위 최단 경로, 정류장 ${p.routes.length}곳 비교`, "관측한 사람 흐름 아님"]);
  } else {
    $("message").textContent = p.supported ? "정류장과 목적지를 잇는 길을 찾지 못했어요." : p.why;
  }
}

function renderWalkCard() {
  const p = state.walk.plan;
  state.cardMode = "walk";
  $("cardBody").innerHTML = walkCardHtml(p, state.walk.active);
  $("cardBack").hidden = !state.card;
  $("card").hidden = false;
  $("cardBody").querySelectorAll("[data-ref]").forEach((b) => (b.onclick = () => { state.walk.active = b.dataset.ref; renderWalkCard(); drawWalk(); }));
  $("cardBody").querySelectorAll("[data-dest]").forEach((b) => (b.onclick = () => openWalk({ dest: Number(b.dataset.dest) })));
  $("cardBody").querySelectorAll("[data-stop]").forEach((b) => (b.onclick = () => openWalk({ stops: p.stopOptions.filter((s) => s.name === b.dataset.stop).map((s) => s.ref) })));
}

function buildingCardHtml(nb) {
  const items = nb.items
    .map((it) => `<li><button type="button" data-idx="${it.idx}" style="--c:${COLOR[it.relation]}"><b>${escapeHtml(it.name)}</b><span>${escapeHtml(it.sub)} · ${it.m}m</span></button></li>`)
    .join("");
  return `
    <p class="kicker" style="--c:${COLOR.selected}">주변 점포</p>
    <h2>건물 주변 ${nb.count}곳</h2>
    <p class="note">${nb.note}</p>
    ${items ? `<ul class="nearby">${items}</ul>` : "<p>등록된 점포가 없어요.</p>"}`;
}

$("ask").addEventListener("submit", (e) => {
  e.preventDefault();
  const t = $("text").value.trim();
  if (!t) return;
  $("text").value = "";
  send({ text: t });
});

fetch("api/status")
  .then((r) => r.json())
  .then((s) => (status = s))
  .catch(() => {});
load().then(async () => {
  const code = pendingCode || linked;
  if (storeRecords) {
    if (embedded) await buildingsReady;
    if (dongIndex(code) >= 0) await openDong(code);
    else await send({});
  }
  if (pendingBuilding) {
    await focusEmbeddedBuilding(pendingBuilding);
    pendingBuilding = null;
  }
  initialized = true;
  if (pendingBusiness !== undefined) {
    state.profile = {...state.profile, business: pendingBusiness};
    state.business = pendingBusiness;
    applyScene();
  }
  if (pendingWalk) {
    showEmbeddedWalk(pendingWalk);
    pendingWalk = null;
  }
}).catch((error) => {
  console.warn("3D map startup:", error);
  showMapError("지도 자료를 불러오지 못했어요.");
});
