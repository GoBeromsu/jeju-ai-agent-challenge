// 지형·도로·건물. 미리 가공한 지도 데이터를 그린다.
import * as THREE from "three";

export const EXAG = 1.5; // 높이 과장

export async function fetchMapAsset(url) {
  const response = await fetch(url, { signal: AbortSignal.timeout(12000) });
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return response;
}

async function fetchGz(url, metrics) {
  const r = await fetchMapAsset(url);
  const raw = await r.arrayBuffer();
  metrics.bytes += raw.byteLength;
  return new Response(new Blob([raw]).stream().pipeThrough(new DecompressionStream("gzip"))).arrayBuffer();
}

function loadImage(url, metrics) {
  return fetchMapAsset(url)
    .then((r) => r.blob())
    .then((b) => {
      metrics.bytes += b.size;
      return createImageBitmap(b, { imageOrientation: "none", colorSpaceConversion: "none" });
    });
}

export class Terrain {
  constructor(info) {
    this.info = info;
    const [x0, z0, x1, z1] = info.bounds;
    Object.assign(this, { x0, z0, x1, z1 });
  }

  static async load(metrics) {
    const r = await fetchMapAsset("data/terrain.json");
    const info = await r.json();
    const t = new Terrain(info);
    const [heights, color, dong] = await Promise.all([
      fetchGz("data/terrain.bin.gz", metrics),
      loadImage("data/terrain_color.jpg", metrics),
      loadImage("data/terrain_dong.png", metrics),
    ]);
    t.heights = new Uint16Array(heights);
    t.colorImage = color;
    t.dongImage = dong;
    // 동네 번호를 CPU 에서도 찾을 수 있게 (클릭, 흐름 배치)
    const cv = new OffscreenCanvas(dong.width, dong.height);
    const ctx = cv.getContext("2d");
    ctx.drawImage(dong, 0, 0);
    const px = ctx.getImageData(0, 0, dong.width, dong.height).data;
    t.dongIds = new Uint8Array(dong.width * dong.height);
    for (let i = 0; i < t.dongIds.length; i++) t.dongIds[i] = px[i * 4];
    return t;
  }

  // 미터 좌표 → 과장된 높이
  heightAt(x, z) {
    const { w, h, step } = this.info.grid;
    const gx = THREE.MathUtils.clamp((x - this.x0) / step, 0, w - 1.001);
    const gz = THREE.MathUtils.clamp((z - this.z0) / step, 0, h - 1.001);
    const ix = Math.floor(gx);
    const iz = Math.floor(gz);
    const fx = gx - ix;
    const fz = gz - iz;
    const H = this.heights;
    const a = H[iz * w + ix];
    const b = H[iz * w + ix + 1];
    const c = H[(iz + 1) * w + ix];
    const d = H[(iz + 1) * w + ix + 1];
    // Interpolate on the same two triangles as buildMesh, not a bilinear patch.
    return (fx + fz <= 1
      ? a + (b - a) * fx + (c - a) * fz
      : d + (c - d) * (1 - fx) + (b - d) * (1 - fz)) * EXAG;
  }

  // 미터 좌표 → 동네 순번 (dongs 배열 index), 바다는 -1
  dongIndexAt(x, z) {
    const { w, h, step } = this.info.texture;
    const ix = Math.round((x - this.x0) / step);
    const iz = Math.round((z - this.z0) / step);
    if (ix < 0 || iz < 0 || ix >= w || iz >= h) return -1;
    return this.dongIds[iz * w + ix] - 1;
  }

  buildMesh(street = false) {
    const { w, h, step } = this.info.grid;
    const pos = new Float32Array(w * h * 3);
    const uv = new Float32Array(w * h * 2);
    const H = this.heights;
    for (let iz = 0; iz < h; iz++) {
      for (let ix = 0; ix < w; ix++) {
        const i = iz * w + ix;
        pos[i * 3] = this.x0 + ix * step;
        pos[i * 3 + 1] = H[i] * EXAG;
        pos[i * 3 + 2] = this.z0 + iz * step;
        uv[i * 2] = ix / (w - 1);
        uv[i * 2 + 1] = iz / (h - 1);
      }
    }
    // 땅이 한 꼭짓점이라도 걸친 칸만 그린다 (바다는 그리지 않음)
    const idx = [];
    for (let iz = 0; iz < h - 1; iz++) {
      for (let ix = 0; ix < w - 1; ix++) {
        const a = iz * w + ix;
        const b = a + 1;
        const c = a + w;
        const d = c + 1;
        if (H[a] || H[b] || H[c] || H[d]) idx.push(a, c, b, b, c, d);
      }
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    geo.setAttribute("uv", new THREE.BufferAttribute(uv, 2));
    geo.setIndex(new THREE.Uint32BufferAttribute(idx, 1));
    geo.computeBoundingSphere();

    const colorTex = new THREE.Texture(this.colorImage);
    colorTex.flipY = false;
    colorTex.anisotropy = 4;
    colorTex.needsUpdate = true;
    const dongTex = new THREE.Texture(this.dongImage);
    dongTex.flipY = false;
    dongTex.magFilter = THREE.NearestFilter;
    dongTex.minFilter = THREE.NearestFilter;
    dongTex.generateMipmaps = false;
    dongTex.needsUpdate = true;
    this.palette = new Uint8Array(64 * 4);
    this.paletteTex = new THREE.DataTexture(this.palette, 64, 1, THREE.RGBAFormat);
    this.paletteTex.magFilter = THREE.NearestFilter;
    this.paletteTex.minFilter = THREE.NearestFilter;
    this.paletteTex.needsUpdate = true;

    const mat = new THREE.ShaderMaterial({
      uniforms: {
        colorTex: { value: colorTex }, dongTex: { value: dongTex }, palette: { value: this.paletteTex },
        street: { value: street },
        contourStep: { value: 200 },
      },
      vertexShader: /* glsl */ `
        varying vec2 vUv;
        varying float elevation;
        void main() {
          vUv = uv;
          elevation = position.y / ${EXAG.toFixed(1)};
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }`,
      fragmentShader: /* glsl */ `
        uniform sampler2D colorTex;
        uniform sampler2D dongTex;
        uniform sampler2D palette;
        uniform bool street;
        uniform float contourStep;
        varying vec2 vUv;
        varying float elevation;
        void main() {
          vec3 base = texture2D(colorTex, vUv).rgb;
          float id = floor(texture2D(dongTex, vUv).r * 255.0 + 0.5);
          vec4 tint = texture2D(palette, vec2((id + 0.5) / 64.0, 0.5));
          // 땅의 음영은 살리고 색만 덧입힌다
          float lum = dot(base, vec3(0.299, 0.587, 0.114));
          vec3 tinted = tint.rgb * (0.55 + 0.6 * lum);
          vec3 ground = street ? mix(base, tinted, tint.a * 0.14) : mix(base, tinted, tint.a);
          // Lines follow the DEM in real metres, independent of vertical exaggeration.
          float level = elevation / contourStep;
          float pixel = max(fwidth(level), 0.0001);
          float line = 1.0 - smoothstep(0.0, min(pixel * 1.25, 0.25), abs(fract(level + 0.5) - 0.5));
          float ink = 0.26 * line * step(0.5, id) * step(contourStep * 0.5, elevation)
            * (1.0 - smoothstep(0.18, 0.4, pixel));
          vec3 water = vec3(168.0, 204.0, 212.0) / 255.0;
          gl_FragColor = vec4(id < 0.5 ? water : mix(ground, vec3(0.23, 0.32, 0.25), ink), 1.0);
        }`,
    });
    this.mesh = new THREE.Mesh(geo, mat);
    return this.mesh;
  }

  // index: dongs 배열 순번, color: "#rrggbb" 또는 null(원래 땅색), alpha 0..1
  setTint(index, color, alpha) {
    const o = (index + 1) * 4;
    if (!color) {
      this.palette[o + 3] = 0;
    } else {
      const c = new THREE.Color(color);
      this.palette[o] = Math.round(c.r * 255);
      this.palette[o + 1] = Math.round(c.g * 255);
      this.palette[o + 2] = Math.round(c.b * 255);
      this.palette[o + 3] = Math.round(alpha * 255);
    }
    this.paletteTex.needsUpdate = true;
  }

  dequant(qx, qz) {
    return [this.x0 + (qx / 65535) * (this.x1 - this.x0), this.z0 + (qz / 65535) * (this.z1 - this.z0)];
  }
}

// ---------- 도로: 등급별 폭을 가진 띠 ----------
const ROAD_WIDTH = [40, 32, 21, 12, 5];
const ROAD_COLOR = ["#365f60", "#365f60", "#52746b", "#7b8c7c", "#a7aa94"];

export async function buildRoads(terrain, metrics) {
  const buf = await fetchGz("data/roads.bin.gz", metrics);
  const view = new DataView(buf);
  const layers = ROAD_WIDTH.map(() => ({ pos: [], idx: [] }));
  let o = 0;
  while (o < buf.byteLength) {
    const cls = view.getUint8(o);
    const n = view.getUint16(o + 1, true);
    let qx = view.getUint16(o + 3, true);
    let qz = view.getUint16(o + 5, true);
    o += 7;
    const pts = [terrain.dequant(qx, qz)];
    for (let k = 1; k < n; k++) {
      qx += view.getInt16(o, true);
      qz += view.getInt16(o + 2, true);
      o += 4;
      const p = terrain.dequant(qx, qz);
      const prev = pts[pts.length - 1];
      if (p[0] !== prev[0] || p[1] !== prev[1]) pts.push(p);
    }
    if (pts.length < 2) continue;
    const { pos, idx } = layers[cls];
    const half = ROAD_WIDTH[cls] / 2;
    const sides = pts.slice(1).map(([x, z], k) => {
      const dx = x - pts[k][0], dz = z - pts[k][1];
      const len = Math.hypot(dx, dz);
      return [-dz / len, dx / len];
    });
    const joins = pts.map((_, k) => {
      const a = sides[Math.max(0, k - 1)], b = sides[Math.min(k, sides.length - 1)];
      // Shared, bounded mitres keep the road continuous through bends.
      const m = half / Math.max(0.6, 1 + a[0] * b[0] + a[1] * b[1]);
      return [(a[0] + b[0]) * m, (a[1] + b[1]) * m];
    });
    const first = pos.length / 3;
    const emit = (x, z, nx, nz) => {
      const base = pos.length / 3;
      const lift = 2.5 + (4 - cls) * 0.2;
      pos.push(x + nx, terrain.heightAt(x + nx, z + nz) + lift, z + nz,
        x - nx, terrain.heightAt(x - nx, z - nz) + lift, z - nz);
      if (base > first) idx.push(base - 2, base - 1, base, base, base - 1, base + 1);
    };
    emit(...pts[0], ...joins[0]);
    for (let k = 1; k < pts.length; k++) {
      const [ax, az] = pts[k - 1], [bx, bz] = pts[k];
      const [an, azn] = joins[k - 1], [bn, bzn] = joins[k];
      // OSM segments can span many DEM cells; drape them instead of cutting through hills.
      const steps = Math.ceil(Math.hypot(bx - ax, bz - az) / (terrain.info.grid.step / 5));
      for (let j = 1; j <= steps; j++) {
        const t = j / steps;
        emit(ax + (bx - ax) * t, az + (bz - az) * t, an + (bn - an) * t, azn + (bzn - azn) * t);
      }
    }
  }
  return layers.map(({ pos, idx }, cls) => {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    geo.setIndex(new THREE.Uint32BufferAttribute(idx, 1));
    return new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
      color: ROAD_COLOR[cls],
      side: THREE.DoubleSide,
    }));
  });
}

// ---------- 나무: 5km 칸마다 InstancedMesh 하나 (화면 밖 칸은 그리지 않음) ----------
export async function buildTrees(terrain, metrics, street = false) {
  const buf = await fetchGz("data/trees.bin.gz", metrics);
  const view = new DataView(buf);
  const n = buf.byteLength / 5;
  const CHUNK = 5000;
  const chunks = new Map();
  for (let i = 0; i < n; i++) {
    const [x, z] = terrain.dequant(view.getUint16(i * 5, true), view.getUint16(i * 5 + 2, true));
    const s = view.getUint8(i * 5 + 4) / 100;
    const key = `${Math.floor(x / CHUNK)},${Math.floor(z / CHUNK)}`;
    if (!chunks.has(key)) chunks.set(key, []);
    chunks.get(key).push(x, z, s);
  }
  // 둥근 수관 + 짧은 줄기를 한 기하로
  const crown = new THREE.IcosahedronGeometry(1, 0);
  crown.scale(22, 26, 22);
  crown.translate(0, 40, 0);
  const trunk = new THREE.CylinderGeometry(3, 4, 18, 5);
  trunk.translate(0, 9, 0);
  const geo = mergeColored([
    [crown, "#5f8f55"],
    [trunk, "#8a6a4a"],
  ]);
  const mat = new THREE.MeshLambertMaterial({ vertexColors: true });
  const group = new THREE.Group();
  const m = new THREE.Matrix4();
  const q = new THREE.Quaternion();
  const sc = new THREE.Vector3();
  const p = new THREE.Vector3();
  const tint = new THREE.Color();
  let seed = 1;
  const rand = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
  for (const arr of chunks.values()) {
    const count = arr.length / 3;
    const mesh = new THREE.InstancedMesh(geo, mat, count);
    for (let k = 0; k < count; k++) {
      const x = arr[k * 3];
      const z = arr[k * 3 + 1];
      const s = arr[k * 3 + 2];
      p.set(x, terrain.heightAt(x, z), z);
      q.setFromAxisAngle(THREE.Object3D.DEFAULT_UP, rand() * Math.PI * 2);
      sc.setScalar(s * (street ? 0.33 : 1));
      m.compose(p, q, sc);
      mesh.setMatrixAt(k, m);
      tint.setHSL(0.27 + rand() * 0.06, 0.25 + rand() * 0.15, 0.82 + rand() * 0.25);
      mesh.setColorAt(k, tint);
    }
    mesh.computeBoundingSphere();
    group.add(mesh);
  }
  return group;
}

function mergeColored(parts) {
  const pos = [];
  const nor = [];
  const col = [];
  const c = new THREE.Color();
  for (const [g0, hex] of parts) {
    const g = g0.index ? g0.toNonIndexed() : g0;
    g.computeVertexNormals();
    c.set(hex);
    const P = g.attributes.position.array;
    const N = g.attributes.normal.array;
    for (let i = 0; i < P.length; i++) {
      pos.push(P[i]);
      nor.push(N[i]);
    }
    for (let i = 0; i < P.length / 3; i++) col.push(c.r, c.g, c.b);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  geo.setAttribute("normal", new THREE.Float32BufferAttribute(nor, 3));
  geo.setAttribute("color", new THREE.Float32BufferAttribute(col, 3));
  return geo;
}

// ---------- 건물: OSM 윤곽을 방향 있는 상자로, 5km 칸마다 InstancedMesh ----------
export const BUILDING_EXAG = 2.0;
const WALL_COLORS = ["#d5c7ae", "#d9cfc1", "#bbc2be", "#d0bb9e", "#c7c9ba"];
const ROOF_COLORS = ["#777e7a", "#9a7661", "#637e83", "#8b8d7a", "#a69a8a"];

// Same local metre projection as build/build_data.py; the pilot stores WGS84 rings.
function projectFootprint([lon, lat]) {
  return [(lon - 126.55) * 111320 * Math.cos(33.38 * Math.PI / 180), -(lat - 33.38) * 111320];
}

async function loadStreetFootprints(metrics) {
  try {
    const res = await fetchMapAsset("studio-footprints.json");
    const raw = await res.text();
    metrics.bytes += new TextEncoder().encode(raw).byteLength;
    return JSON.parse(raw);
  } catch {
    // Packaged builds without the optional pilot still have the island-wide boxes.
    return null;
  }
}

function addStreetFootprints(group, terrain, pack, boxes) {
  const replaced = new Set();
  const oldCenters = pack.old.map(([ring]) => {
    const pts = ring.slice(0, -1).map(projectFootprint);
    return pts.reduce(([x, z], [a, b]) => [x + a / pts.length, z + b / pts.length], [0, 0]);
  });
  for (const rec of boxes) {
    const [x, z] = rec;
    if (oldCenters.some(([a, b]) => Math.hypot(x - a, z - b) < 2)) replaced.add(rec);
  }
  const vertices = [], normals = [], colors = [];
  const roofPoints = [];
  pack.features.forEach((rings, i) => {
    const [outline, ...holes] = rings.map((ring) => ring.slice(0, -1).map(projectFootprint));
    if (outline.length < 3) return;
    const ground = outline.map(([x, z]) => terrain.heightAt(x, z));
    const base = Math.min(...ground) - 1;
    // No measured heights or roof profiles in the pilot; use a uniform low-rise extrusion.
    const top = Math.max(...ground) + 9;
    const pathPoints = (ring) => ring.map(([x, z]) => new THREE.Vector2(x, -z));
    const shape = new THREE.Shape(pathPoints(outline));
    shape.holes = holes.map((ring) => new THREE.Path(pathPoints(ring)));
    const geo = new THREE.ExtrudeGeometry(shape, { depth: top - base, bevelEnabled: false });
    geo.rotateX(-Math.PI / 2);
    geo.translate(0, base, 0);
    const pos = geo.attributes.position, normal = geo.attributes.normal;
    const wall = new THREE.Color(WALL_COLORS[i % WALL_COLORS.length]);
    const roof = new THREE.Color(ROOF_COLORS[i % ROOF_COLORS.length]);
    let largest = 0;
    for (let j = 0; j < pos.count; j++) {
      const y = pos.getY(j), ny = normal.getY(j);
      vertices.push(pos.getX(j), y, pos.getZ(j));
      normals.push(normal.getX(j), ny, normal.getZ(j));
      const c = ny > 0.5 ? roof : wall;
      const shade = ny > 0.5 ? 1 : 0.63 + 0.37 * (y - base) / (top - base);
      colors.push(c.r * shade, c.g * shade, c.b * shade);
      if (ny > 0.5 && j % 3 === 0) {
        const ax = pos.getX(j), bx = pos.getX(j + 1), cx = pos.getX(j + 2);
        const az = pos.getZ(j), bz = pos.getZ(j + 1), cz = pos.getZ(j + 2);
        const area = Math.abs((bx - ax) * (cz - az) - (cx - ax) * (bz - az));
        if (area > largest) {
          largest = area;
          roofPoints[i] = [(ax + bx + cx) / 3, (az + bz + cz) / 3];
        }
      }
    }
    geo.dispose();
  });
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.Float32BufferAttribute(vertices, 3));
  geo.setAttribute("normal", new THREE.Float32BufferAttribute(normals, 3));
  geo.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
  group.add(new THREE.Mesh(geo, new THREE.MeshLambertMaterial({ vertexColors: true })));
  group.userData.streetCenter = projectFootprint(pack.center);
  group.userData.roofPoints = roofPoints;
  return replaced;
}

export async function buildBuildings(terrain, metrics, street = false) {
  const [buf, pack] = await Promise.all([
    fetchGz("data/buildings.bin.gz", metrics),
    street ? loadStreetFootprints(metrics) : null,
  ]);
  const view = new DataView(buf);
  const n = buf.byteLength / 8;
  const CHUNK = 5000;
  const chunks = new Map();
  for (let i = 0; i < n; i++) {
    const o = i * 8;
    const [x, z] = terrain.dequant(view.getUint16(o, true), view.getUint16(o + 2, true));
    const rec = [x, z, view.getUint8(o + 4) / 2, view.getUint8(o + 5) / 2, (view.getUint8(o + 6) / 255) * Math.PI, view.getUint8(o + 7)];
    const key = `${Math.floor(x / CHUNK)},${Math.floor(z / CHUNK)}`;
    if (!chunks.has(key)) chunks.set(key, []);
    chunks.get(key).push(rec);
  }
  const geo = new THREE.BoxGeometry(1, 1, 1);
  geo.translate(0, 0.5, 0);
  // 지붕은 밝게, 벽은 아래로 갈수록 살짝 어둡게 (구운 그늘)
  const shade = new Float32Array(geo.attributes.position.count * 3);
  for (let i = 0; i < geo.attributes.position.count; i++) {
    const y = geo.attributes.position.getY(i);
    const top = geo.attributes.normal.getY(i) > 0.5;
    const v = top ? 1.0 : 0.78 + 0.18 * y;
    shade[i * 3] = shade[i * 3 + 1] = shade[i * 3 + 2] = v;
  }
  geo.setAttribute("color", new THREE.BufferAttribute(shade, 3));
  const mat = new THREE.MeshLambertMaterial({ vertexColors: true });
  const group = new THREE.Group();
  group.userData.buildingCenters = [];
  const [cx, cz] = pack ? projectFootprint(pack.center) : [0, 0];
  const nearbyBoxes = pack ? [...chunks.values()].flat().filter(([x, z]) =>
    Math.abs(x - cx) < 550 && Math.abs(z - cz) < 550) : [];
  const replaced = pack ? addStreetFootprints(group, terrain, pack, nearbyBoxes) : new Set();
  const m = new THREE.Matrix4();
  const q = new THREE.Quaternion();
  const sc = new THREE.Vector3();
  const p = new THREE.Vector3();
  const c = new THREE.Color();
  for (const records of chunks.values()) {
    const list = replaced.size ? records.filter((rec) => !replaced.has(rec)) : records;
    if (!list.length) continue;
    const mesh = new THREE.InstancedMesh(geo, mat, list.length);
    list.forEach(([x, z, w, d, a, h], k) => {
      group.userData.buildingCenters.push([x, z]);
      p.set(x, terrain.heightAt(x, z) - 2, z);
      q.setFromAxisAngle(THREE.Object3D.DEFAULT_UP, -a);
      sc.set(w, h * BUILDING_EXAG + 2, d);
      m.compose(p, q, sc);
      mesh.setMatrixAt(k, m);
      // 낮은 집은 따뜻한 흰색, 높은 건물은 푸른 회색
      const t = Math.min(1, (h - 3) / 40);
      if (street) c.set(WALL_COLORS[Math.abs(Math.round(x * 13 + z * 7)) % WALL_COLORS.length]);
      else c.setRGB(0.96 - 0.2 * t, 0.94 - 0.14 * t, 0.89 - 0.04 * t);
      mesh.setColorAt(k, c);
    });
    mesh.computeBoundingSphere();
    group.add(mesh);
  }
  return group;
}
