// Procedural 3D model of the Elgato Cam Link 4K board (PD570 rev 2.1), both sides.
// Layout follows public photos of the real board (apertus wiki, Greg Davill, reference only, not in
// the repository): coordinates are in photo pixels (1007 x 3000, origin top-left, USB plug at the top,
// HDMI at the bottom, ~30 px/mm), side B parts mirrored (board flipped around its long axis).
// Passive areas and traces are stylized.
import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';

export const IMG_W = 1007, IMG_H = 3000;
const Y0 = 640, Y1 = 2810;                              // PCB extent (photo px)
export const PCB_LEN = 10.0;                           // world units along the board
const K = PCB_LEN / (Y1 - Y0);                         // world units per photo pixel
export const PCB_W = 840 * K, PCB_H = PCB_LEN, PCB_T = 1.2 * 30 * K;
const MM = 30 * K;                                     // world units per mm
const YC = (Y0 + Y1) / 2;
export const PX = (x, y) => new THREE.Vector3((x - IMG_W / 2) * K, PCB_T / 2, (y - YC) * K);

function rng(seed) { return () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; }; }
function tex(c, srgb = true) { const t = new THREE.CanvasTexture(c); if (srgb) t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 16; return t; }
function canvas(w, h) { const c = document.createElement('canvas'); c.width = w; c.height = h; return c; }

// ------------------------------------------------------------------------------------------ layout
// Board outline (photo px): chamfered corners at both ends.
const OUTLINE = [[180, 640], [826, 640], [924, 740], [924, 2560], [826, 2810], [180, 2810], [84, 2560], [84, 740]];
const SLOTS = [[310, 740, 30, 60], [710, 740, 30, 60], [256, 2480, 30, 50], [744, 2480, 30, 50]];   // plated oval slots
const U4 = [200, 1296, 784, 1880];                     // Lattice ECP5 LFE5U-25F (BGA381)
const TESTPADS = [
  ...Array.from({ length: 8 }, (_, i) => [800, 990 + i * 34]), ...Array.from({ length: 8 }, (_, i) => [846, 990 + i * 34]),
  ...Array.from({ length: 12 }, (_, i) => [812, 1500 + i * 32]), ...Array.from({ length: 12 }, (_, i) => [856, 1500 + i * 32]),
  ...Array.from({ length: 7 }, (_, i) => [150, 1720 + i * 30]),
];
const HDMI_THT = [...Array.from({ length: 10 }, (_, i) => [345 + i * 34, 2475]), ...Array.from({ length: 9 }, (_, i) => [362 + i * 34, 2512])];

// Components: [kind, x0, y0, x1, y1, h(mm), opts]; side B parts in side B photo coordinates.
const PARTS_A = [
  ['bga', 420, 916, 776, 1280, 1.1, { text: ['CYUSB3014-BZX', 'C 1913', 'A 33 THA', 'CYP 612595'], label: 'Cypress FX3 · USB 3.0', lh: 2.4, ref: 'U2' }],
  ['bga', 200, 2030, 680, 2340, 1.1, { text: ['9DJ17', 'D9SFT'], micron: true, label: 'DDR3L 128MB · LiteDRAM', lh: 2.0, ref: 'U5' }],
  ['sop', 170, 864, 326, 1070, 0.8, { pins: 'tb', n: 4, text: ['winbond', '25Q32JVIQ'], label: 'SPI flash', lh: 1.4, ref: 'U3' }],
  ['qfn', 766, 2070, 860, 2190, 0.6, { text: ['APL', '5336'], ref: 'U8' }],
  ['xtal', 116, 1166, 206, 1256, 0.8, { text: ['27.0'], label: '27 MHz', lh: 0.9, ref: 'Y1' }],
  ['sot', 468, 790, 516, 850, 0.6, { ref: 'U9' }], ['sot', 736, 790, 840, 850, 0.6, { ref: 'Q1' }],
  ['usba', 300, 140, 700, 700, 4.5, {}],
  ['hdmi', 240, 2550, 770, 2890, 5.0, {}],
];
const PARTS_B = [
  ['qfn', 476, 1866, 796, 2190, 0.9, { text: ['ITE', 'IT6802E', 'IT6801FN'], ite: true, label: 'ITE IT6802 · HDMI Rx', lh: 1.9, ref: 'U1' }],
  ['led', 424, 2214, 556, 2324, 0.7, { ref: 'D1' }],
  ['xtal', 286, 2136, 380, 2244, 0.8, { text: ['HS180'], ref: 'Y2' }],
  ['xtal', 730, 860, 820, 950, 0.8, { text: ['H.000'], ref: 'Y3' }],
  ['sop', 116, 2220, 210, 2370, 0.8, { pins: 'lr', n: 4, ref: 'U10' }],
  ['sop', 196, 910, 270, 1000, 0.8, { pins: 'lr', n: 4, ref: 'U11' }],
  ['sot', 196, 1010, 270, 1080, 0.6, { ref: 'U12' }], ['sot', 126, 1860, 190, 1930, 0.7, { ref: 'X1' }],
  ['ind', 300, 1210, 380, 1290, 1.2, { ref: 'L1' }], ['ind', 790, 1360, 880, 1430, 1.2, { ref: 'L2' }],
  ['ind', 210, 1736, 296, 1800, 1.2, { ref: 'L3' }],
];
const BIGCAPS_A = [[170, 2230, 205, 2300], [766, 2210, 800, 2270], [812, 2210, 846, 2270], [200, 1130, 250, 1160]];
// Zones filled with 0402/0603 passives (photo px rects).
const ZONES_A = [[340, 1080, 410, 1160], [520, 800, 620, 890], [620, 810, 720, 880], [220, 1905, 800, 1975], [110, 760, 300, 850],
  [330, 860, 420, 1080], [110, 1280, 190, 1560], [800, 1880, 900, 2050], [700, 2360, 900, 2440], [120, 1960, 200, 2180], [600, 1080, 780, 1110],
  [120, 1560, 190, 1700], [700, 2000, 760, 2200], [230, 2360, 700, 2400], [100, 2200, 160, 2300], [220, 1100, 330, 1150]];
const ZONES_B = [[300, 900, 700, 1150], [150, 1100, 420, 1350], [420, 1150, 900, 1330], [220, 1330, 780, 1560],
  [120, 1560, 460, 1740], [480, 1600, 900, 1840], [120, 1950, 460, 2130], [800, 1880, 900, 2200], [560, 2200, 780, 2320]];

// ------------------------------------------------------------------------------------------ helpers
function outlinePath(g, s = 1) {
  g.beginPath(); OUTLINE.forEach(([x, y], i) => (i ? g.lineTo(x * s, y * s) : g.moveTo(x * s, y * s))); g.closePath();
}
function route(g, s, a, b) {  // 45-degree router
  const dx = b[0] - a[0], dy = b[1] - a[1];
  g.beginPath(); g.moveTo(a[0] * s, a[1] * s);
  if (Math.abs(dy) > Math.abs(dx)) g.lineTo(a[0] * s, (b[1] - Math.sign(dy) * Math.abs(dx)) * s);
  else g.lineTo((b[0] - Math.sign(dx) * Math.abs(dy)) * s, a[1] * s);
  g.lineTo(b[0] * s, b[1] * s); g.stroke();
}
function inRect(x, y, r, m = 0) { return x > r[0] - m && x < r[2] + m && y > r[1] - m && y < r[3] + m; }
const mirX = x => IMG_W - x;

// Solder mask/copper/silk textures for one side.
function sideTextures(side, R) {
  const S = 1.5, CW = Math.round(IMG_W * S), CH = Math.round(IMG_H * S);
  const col = canvas(CW, CH), rough = canvas(CW, CH), metal = canvas(CW, CH), bump = canvas(CW, CH);
  const gc = col.getContext('2d'), gr = rough.getContext('2d'), gm = metal.getContext('2d'), gb = bump.getContext('2d');
  gc.fillStyle = '#0e2a17'; gc.fillRect(0, 0, CW, CH);                 // green solder mask
  gr.fillStyle = '#505050'; gr.fillRect(0, 0, CW, CH);
  gm.fillStyle = '#000'; gm.fillRect(0, 0, CW, CH);
  gb.fillStyle = '#000'; gb.fillRect(0, 0, CW, CH);
  for (let i = 0; i < 6000; i++) {
    const x = R() * CW, y = R() * CH; gc.fillStyle = `rgba(30,70,40,${0.05 + R() * 0.07})`; gc.fillRect(x, y, 2 + R() * 3, 2 + R() * 3);
  }
  const trace = (a, b, w = 3) => {
    gc.strokeStyle = '#17452a'; gc.lineWidth = w * S; gc.lineCap = gc.lineJoin = 'round'; route(gc, S, a, b);
    gb.strokeStyle = '#fff'; gb.lineWidth = w * S; gb.lineCap = gb.lineJoin = 'round'; route(gb, S, a, b);
  };
  const via = (x, y) => {
    gc.fillStyle = '#a58b4c'; gc.beginPath(); gc.arc(x * S, y * S, 5 * S, 0, 7); gc.fill();
    gc.fillStyle = '#050805'; gc.beginPath(); gc.arc(x * S, y * S, 2.2 * S, 0, 7); gc.fill();
    gm.fillStyle = '#fff'; gm.beginPath(); gm.arc(x * S, y * S, 5 * S, 0, 7); gm.fill();
  };
  const bundle = (a, b, n, sp = 9, w = 3) => {
    const dx = b[0] - a[0], dy = b[1] - a[1], l = Math.hypot(dx, dy), px = -dy / l, py = dx / l;
    for (let i = 0; i < n; i++) {
      const o = (i - (n - 1) / 2) * sp;
      trace([a[0] + px * o, a[1] + py * o], [b[0] + px * o, b[1] + py * o], w); if (R() < 0.4) via(b[0] + px * o, b[1] + py * o);
    }
  };
  const gold = (draw) => { for (const [g, c] of [[gc, '#d9b263'], [gm, '#fff'], [gr, '#3a3a3a']]) { g.fillStyle = c; g.strokeStyle = c; draw(g); } };
  const silk = '#e6ebe4';
  if (side === 'A') {
    const cx4 = (U4[0] + U4[2]) / 2;
    bundle([cx4 + 120, U4[1]], [560, 1280], 12, 9);          // ECP5 -> FX3 (GPIF-II)
    bundle([U4[2], 1400], [800, 1300], 6, 10);
    bundle([cx4 - 60, U4[3]], [400, 2030], 14, 9);           // ECP5 -> DDR3
    bundle([U4[0] + 40, U4[3]], [260, 2030], 6, 10);
    bundle([U4[0], 1400], [206, 1256], 3, 12);               // -> 27 MHz
    bundle([560, 916], [540, 700], 8, 10, 2.5);              // FX3 -> USB 3.0
    bundle([420, 1000], [326, 1000], 4, 12, 2.5);            // FX3 -> SPI flash
    bundle([cx4 + 200, U4[3]], [780, 2070], 4, 10);          // -> APL5336 (DDR VTT)
    bundle([500, 2340], [500, 2470], 10, 11, 2.5);           // HDMI -> vias (IT6802 on side B)
    for (let i = 0; i < 90; i++) {
      const z = ZONES_A[Math.floor(R() * ZONES_A.length)];
      const a = [z[0] + R() * (z[2] - z[0]), z[1] + R() * (z[3] - z[1])], b = [a[0] + (R() - 0.5) * 160, a[1] + (R() - 0.5) * 160];
      trace(a, b, 2 + R() * 1.5); if (R() < 0.6) via(b[0], b[1]);
    }
    // BGA landing pads under the FPGA (visible once the chip is lifted)
    gc.fillStyle = '#0b1a10'; gc.fillRect(U4[0] * S, U4[1] * S, (U4[2] - U4[0]) * S, (U4[3] - U4[1]) * S);
    const n = 19;
    for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) gold(g => {
      const x = U4[0] + 18 + i * ((U4[2] - U4[0] - 36) / (n - 1)), y = U4[1] + 18 + j * ((U4[3] - U4[1] - 36) / (n - 1));
      g.beginPath(); g.arc(x * S, y * S, 6 * S, 0, 7); g.fill();
    });
    for (const [x, y] of TESTPADS) gold(g => { g.fillRect((x - 12) * S, (y - 10) * S, 24 * S, 20 * S); });
    for (const [x, y] of HDMI_THT) gold(g => { g.lineWidth = 5 * S; g.beginPath(); g.arc(x * S, y * S, 10 * S, 0, 7); g.stroke(); });
    gold(g => { g.beginPath(); g.arc(830 * S, 2390 * S, 50 * S, 0, 7); g.fill(); });
    gc.fillStyle = '#6b5a2a'; gc.beginPath(); gc.arc(830 * S, 2390 * S, 22 * S, 0, 7); gc.fill();
    gold(g => { g.fillRect(456 * S, 664 * S, 124 * S, 40 * S); });               // USB shield tab pad
    // silkscreen
    gc.strokeStyle = gc.fillStyle = silk; gc.lineWidth = 2.5 * S;
    gc.strokeRect((U4[0] - 10) * S, (U4[1] - 10) * S, (U4[2] - U4[0] + 20) * S, (U4[3] - U4[1] + 20) * S);
    gc.save(); gc.translate(890 * S, 1010 * S); gc.rotate(Math.PI / 2);
    gc.font = `500 ${40 * S}px Ubuntu Mono`; gc.fillText('PD570  REV:2.1   MADE IN TAIWAN', 0, 0); gc.restore();
    gc.save(); gc.translate(120 * S, 1330 * S); gc.rotate(Math.PI / 2);
    gc.font = `500 ${26 * S}px Ubuntu Mono`; gc.fillText('94V-0  20006-T1-8243', 0, 0); gc.restore();
  } else {
    bundle([mirX(640), 1866], [mirX(640), 1600], 10, 10);    // IT6802 -> vias to the FPGA
    bundle([mirX(560), 2190], [mirX(520), 2440], 12, 10, 2.5);   // HDMI -> IT6802
    for (let i = 0; i < 110; i++) {
      const z = ZONES_B[Math.floor(R() * ZONES_B.length)];
      const a = [mirX(z[0] + R() * (z[2] - z[0])), z[1] + R() * (z[3] - z[1])], b = [a[0] + (R() - 0.5) * 180, a[1] + (R() - 0.5) * 180];
      trace(a, b, 2 + R() * 1.5); if (R() < 0.6) via(b[0], b[1]);
    }
    for (let i = 0; i < 70; i++) { const x = 110 + R() * 790, y = 760 + R() * 1750; via(x, y); }
    for (let i = 0; i < 9; i++) gold(g => { g.fillRect((mirX(350) - 16 + (i - 4) * 34) * S, 770 * S, 18 * S, 70 * S); });   // USB 3.0 pins
    gold(g => { g.lineWidth = 12 * S; g.beginPath(); g.arc(mirX(200) * S, 830 * S, 58 * S, 0, 7); g.stroke(); });
    gc.fillStyle = '#050805'; gc.beginPath(); gc.arc(mirX(200) * S, 830 * S, 40 * S, 0, 7); gc.fill();
    gold(g => { g.beginPath(); g.arc(mirX(830) * S, 2390 * S, 50 * S, 0, 7); g.fill(); });
    gc.fillStyle = gc.strokeStyle = silk; gc.lineWidth = 2.5 * S;
  }
  // plated slots and edge
  for (const [x, y, w, h] of SLOTS) gold(g => {
    const X = side === 'A' ? x : mirX(x);
    g.lineWidth = 10 * S; g.beginPath(); g.ellipse(X * S, y * S, (w + 6) * S, (h + 6) * S, 0, 0, 7); g.stroke();
  });
  gc.save(); outlinePath(gc, S); gc.clip(); gc.strokeStyle = '#0a2012'; gc.lineWidth = 10 * S; outlinePath(gc, S); gc.stroke(); gc.restore();
  return { col, rough, metal, bump };
}

// ------------------------------------------------------------------------------------------ build
export function buildBoard() {
  const group = new THREE.Group();
  const R = rng(4321);
  const sp = ([x, y]) => [(x - IMG_W / 2) * K, (YC - y) * K];
  const shape = new THREE.Shape();
  OUTLINE.forEach((p, i) => { const [x, y] = sp(p); i ? shape.lineTo(x, y) : shape.moveTo(x, y); });
  for (const [x, y, w, h] of SLOTS) { const p = new THREE.Path(); const [cx, cy] = sp([x, y]); p.absellipse(cx, cy, w * K, h * K, 0, Math.PI * 2, true); shape.holes.push(p); }
  const geo = new THREE.ExtrudeGeometry(shape, { depth: PCB_T, bevelEnabled: false, curveSegments: 24 });
  const W = IMG_W * K, H = IMG_H * K;
  const mk = (c, srgb, flip = false) => {
    const t = tex(c, srgb); t.repeat.set((flip ? -1 : 1) / W, 1 / H); t.offset.set(0.5, 1 - YC / IMG_H); return t;
  };
  const mat = (T, flip) => new THREE.MeshPhysicalMaterial({
    map: mk(T.col, true, flip), roughnessMap: mk(T.rough, false, flip), metalnessMap: mk(T.metal, false, flip), bumpMap: mk(T.bump, false, flip),
    bumpScale: 0.5, roughness: 1, metalness: 1, clearcoat: 0.45, clearcoatRoughness: 0.3, envMapIntensity: 0.8,
  });
  const TA = sideTextures('A', R), TB = sideTextures('B', R);
  const top = mat(TA, false), edge = new THREE.MeshStandardMaterial({ color: 0x26301f, roughness: 0.7 });
  const board = new THREE.Mesh(geo, [top, edge]);
  board.rotation.x = -Math.PI / 2; board.position.y = -PCB_T / 2;
  board.castShadow = board.receiveShadow = true;
  group.add(board);
  // side B face (separate cap, textured with the side B layout)
  const bottomMat = mat(TB, false);
  const bottom = new THREE.Mesh(new THREE.ShapeGeometry(shape, 24), bottomMat);
  bottom.rotation.x = Math.PI / 2; bottom.position.y = -PCB_T / 2 - 0.001;
  bottom.scale.y = -1;
  group.add(bottom);

  // ------------------------------------------------------------------------------------------ parts
  const M = {
    black: new THREE.MeshStandardMaterial({ color: 0x151518, roughness: 0.55, metalness: 0.1 }),
    tin: new THREE.MeshStandardMaterial({ color: 0xc9ccd2, roughness: 0.3, metalness: 1 }),
    steel: new THREE.MeshStandardMaterial({ color: 0x9aa0a8, roughness: 0.38, metalness: 1, envMapIntensity: 0.8 }),
    gold: new THREE.MeshStandardMaterial({ color: 0xd8b25a, roughness: 0.3, metalness: 1 }),
    cap: new THREE.MeshStandardMaterial({ color: 0xa88963, roughness: 0.55 }),
    res: new THREE.MeshStandardMaterial({ color: 0x141414, roughness: 0.5 }),
    ind: new THREE.MeshStandardMaterial({ color: 0x3a3c40, roughness: 0.6, metalness: 0.2 }),
    sub: new THREE.MeshStandardMaterial({ color: 0x2a3a2c, roughness: 0.45 }),
    white: new THREE.MeshStandardMaterial({ color: 0xf1f0e6, roughness: 0.4, emissive: 0xffffff, emissiveIntensity: 0 }),
  };
  const comps = [], pins = [];
  const topLabel = (w, d, lines, o = {}) => {
    const c = canvas(512, Math.max(128, Math.round(512 * d / w))), g = c.getContext('2d');
    if (o.mirror) { g.translate(c.width, 0); g.scale(-1, 1); }
    g.fillStyle = '#17171a'; g.fillRect(0, 0, c.width, c.height);
    g.fillStyle = '#80858f'; g.textAlign = 'center';
    let fs = Math.min(96, c.height / (lines.length + 1.2));
    g.font = `500 ${fs}px Ubuntu Mono`;
    while (lines.some(l => g.measureText(l).width > c.width * 0.9) && fs > 10) { fs -= 2; g.font = `500 ${fs}px Ubuntu Mono`; }
    if (o.ite) {   // ITE logo block
      g.fillStyle = '#6b6f78'; g.font = `700 ${c.height * 0.3}px Ubuntu`; g.fillText('ITE', c.width / 2, c.height * 0.42);
      g.fillStyle = '#80858f'; g.font = `500 ${c.height * 0.1}px Ubuntu Mono`;
      lines.slice(1).forEach((l, i) => g.fillText(l, c.width / 2, c.height * (0.66 + i * 0.13)));
    } else {
      lines.forEach((l, i) => g.fillText(l, c.width / 2, c.height / 2 + (i - (lines.length - 1) / 2) * fs * 1.1 + fs * 0.35));
      if (o.micron) { g.strokeStyle = '#80858f'; g.lineWidth = 6; g.beginPath(); g.arc(c.width * 0.9, c.height * 0.17, c.height * 0.09, 0, 7); g.stroke(); }
    }
    g.fillStyle = '#2a2b30'; g.beginPath(); g.arc(30, 30, 12, 0, 7); g.fill();
    const m = new THREE.Mesh(new THREE.PlaneGeometry(w * 0.96, d * 0.96), new THREE.MeshStandardMaterial({ map: tex(c), roughness: 0.55 }));
    m.rotation.x = -Math.PI / 2; return m;
  };
  const box = (w, h, d, mat, r = 0) => new THREE.Mesh(r ? new RoundedBoxGeometry(w, h, d, 2, r) : new THREE.BoxGeometry(w, h, d), mat);
  const cx4 = (U4[0] + U4[2]) / 2, cy4 = (U4[1] + U4[3]) / 2, u4c = PX(cx4, cy4);
  const anchors = {};

  const addPart = ([kind, x0, y0, x1, y1, hmm, o], side) => {
    if (side === 'B') { [x0, x1] = [mirX(x1), mirX(x0)]; o = { ...o, mirror: true }; }
    const h = hmm * MM;
    const c = PX((x0 + x1) / 2, (y0 + y1) / 2), w = (x1 - x0) * K, d = (y1 - y0) * K;
    const g = new THREE.Group(); const wrap = new THREE.Group(); wrap.add(g);
    g.position.copy(c);
    const add = (m, y) => { m.position.y = y; m.castShadow = true; m.receiveShadow = true; g.add(m); return m; };
    if (kind === 'bga') {
      add(box(w, h * 0.25, d, M.sub, 0.01), h * 0.125);
      add(box(w * 0.97, h * 0.75, d * 0.97, M.black, 0.01), h * 0.25 + h * 0.375);
      add(topLabel(w * 0.97, d * 0.97, o.text || [], o), h + 0.001);
    } else if (kind === 'sop' || kind === 'qfn') {
      const inset = kind === 'sop' ? 0.035 : 0;
      const bw = o.pins === 'lr' ? w - 2 * inset : w, bd = o.pins === 'tb' ? d - 2 * inset : d;
      add(box(bw, h, bd, M.black, 0.008), h / 2);
      add(topLabel(bw, bd, o.text || [], o), h + 0.001);
      const n = kind === 'sop' ? o.n : 12;
      for (let i = 0; i < n; i++) for (const s of [-1, 1]) {
        const t = (i + 0.5) / n - 0.5, m4 = new THREE.Matrix4();
        if (kind === 'sop' && o.pins === 'lr') m4.compose(new THREE.Vector3(s * (w / 2 - inset / 2), 0.012, t * bd * 0.9), new THREE.Quaternion(), new THREE.Vector3(inset + 0.01, 0.02, bd * 0.9 / n * 0.45));
        else if (kind === 'sop') m4.compose(new THREE.Vector3(t * bw * 0.9, 0.012, s * (d / 2 - inset / 2)), new THREE.Quaternion(), new THREE.Vector3(bw * 0.9 / n * 0.45, 0.02, inset + 0.01));
        else {
          m4.compose(new THREE.Vector3(s * w / 2, 0.006, t * d * 0.85), new THREE.Quaternion(), new THREE.Vector3(0.012, 0.012, d * 0.85 / n * 0.5)); pins.push([m4, g]);
          m4.compose(new THREE.Vector3(t * w * 0.85, 0.006, s * d / 2), new THREE.Quaternion(), new THREE.Vector3(w * 0.85 / n * 0.5, 0.012, 0.012));
        }
        pins.push([m4.clone(), g]);
      }
    } else if (kind === 'xtal') {
      add(box(w, h, d, M.steel, 0.02), h / 2);
      if (o.text) add(topLabel(w * 0.8, d * 0.6, o.text, o), h + 0.001);
    } else if (kind === 'sot') {
      add(box(w * 0.7, h, d * 0.55, M.black, 0.004), h / 2);
      for (const [px, pz] of [[-0.3, -0.42], [0.3, -0.42], [0, 0.42]]) { const t = add(box(w * 0.14, 0.02, d * 0.2, M.tin), 0.01); t.position.set(px * w, 0.01, pz * d); }
    } else if (kind === 'ind') {
      add(box(w, h, d, M.ind, 0.02), h / 2);
      for (const s of [-1, 1]) { const t = add(box(w * 0.22, h * 0.5, d * 1.02, M.tin), h * 0.25); t.position.x = s * w * 0.4; }
    } else if (kind === 'led') {
      add(box(w, h * 0.6, d, M.white, 0.01), h * 0.3);
      const lens = add(box(w * 0.7, h * 0.4, d * 0.7, M.white, 0.02), h * 0.8);
      anchors.led = g; anchors.ledMat = M.white;
    } else if (kind === 'usba') {       // USB 3.0 Type-A plug (metal shell, blue tongue) along -Z
      const len = (y1 - y0) * K, sw = w, sh = 4.5 * MM;
      const shell = add(box(sw, sh, len, M.steel, 0.03), -PCB_T / 2);
      const tongue = new THREE.Mesh(new THREE.BoxGeometry(sw * 0.85, sh * 0.35, len * 0.7), new THREE.MeshStandardMaterial({ color: 0x1e5bd8, roughness: 0.5 }));
      tongue.position.set(0, sh * 0.05 - PCB_T / 2, -len * 0.14); g.add(tongue);
      for (const s of [-1, 1]) { const win = add(box(sw * 0.13, 0.01, len * 0.08, M.black), 0); win.position.set(s * sw * 0.22, sh / 2 - PCB_T / 2 + 0.004, -len * 0.3); }
      anchors.usb = new THREE.Vector3(c.x, 0, c.z - len / 2);
    } else if (kind === 'hdmi') {       // HDMI Type-A receptacle, mid-mount (straddles both sides), opening along +Z
      // Mid-mount in a cutout of the board end (teardown photos: the shell is visible on both
      // sides), body centred on the board mid-plane; trapezoidal mouth, narrow (chamfered) edge
      // toward side A: the wide edge faces side B, the top of the case.
      const len = (y1 - y0) * K, sh = hmm * MM;
      const body = add(box(w, sh, len, M.gold, 0.03), 0);
      const mw = w * 0.80, mh = sh * 0.55, ch = mh * 0.32, mouthShape = new THREE.Shape();
      mouthShape.moveTo(-mw / 2, -mh / 2); mouthShape.lineTo(mw / 2, -mh / 2);
      mouthShape.lineTo(mw / 2, mh / 2 - ch); mouthShape.lineTo(mw / 2 - ch, mh / 2);
      mouthShape.lineTo(-mw / 2 + ch, mh / 2); mouthShape.lineTo(-mw / 2, mh / 2 - ch); mouthShape.closePath();
      const mouth = new THREE.Mesh(new THREE.ExtrudeGeometry(mouthShape, { depth: 0.03, bevelEnabled: false }), M.black);
      mouth.position.set(0, 0, len / 2 - 0.028); g.add(mouth);
      const tongue = add(box(mw * 0.82, mh * 0.18, len * 0.5, M.black, 0.005), 0);   // contact tongue
      tongue.position.set(0, -mh * 0.12, len * 0.24);
      anchors.hdmi = new THREE.Vector3(c.x, 0, c.z + len / 2);
    }
    if (side === 'B') wrap.scale.y = -1;                  // mounted under the board
    group.add(wrap);
    const comp = { g, wrap, side, dist: Math.hypot(c.x - u4c.x, c.z - u4c.z), label: o.label, lh: (o.lh || 1), h, kind: kind === 'usba' || kind === 'hdmi' ? 'connector' : 'part' };
    comps.push(comp);
    if (o.ref) anchors[o.ref] = comp;
    return comp;
  };
  PARTS_A.forEach(p => addPart(p, 'A'));
  PARTS_B.forEach(p => addPart(p, 'B'));

  const inst = (geo, mat, arr) => { const m = new THREE.InstancedMesh(geo, mat, arr.length); arr.forEach((a, i) => m.setMatrixAt(i, a)); m.castShadow = m.receiveShadow = true; return m; };
  const unit = new THREE.BoxGeometry(1, 1, 1);
  const byPart = new Map();
  for (const [m4, g] of pins) { if (!byPart.has(g)) byPart.set(g, []); byPart.get(g).push(m4); }
  for (const [g, arr] of byPart) g.add(inst(unit, M.tin, arr));

  // passives (instanced), one group per area so they can be placed in waves
  const passive = (zones, big, side) => {
    const occupiedA = (x, y) => PARTS_A.some(p => inRect(x, y, [p[1], p[2], p[3], p[4]], 14)) || inRect(x, y, U4, 14) ||
      TESTPADS.some(([tx, ty]) => Math.abs(x - tx) < 20 && Math.abs(y - ty) < 18);
    const occupiedB = (x, y) => PARTS_B.some(p => inRect(x, y, [p[1], p[2], p[3], p[4]], 14));
    const placed = [];
    const mk = (cx, cy) => ({ cx, cy, cap: [], res: [], term: [] });
    const sets = [];
    const addPassive = (set, x, y, l, wdt, h, type, rot) => {
      const X = side === 'B' ? mirX(x) : x;
      const c = PX(X, y), q = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), rot);
      set[type].push(new THREE.Matrix4().compose(new THREE.Vector3(c.x, c.y + h / 2, c.z), q, new THREE.Vector3(l * 0.62, h, wdt)));
      for (const s of [-1, 1]) {
        const off = new THREE.Vector3(s * l * 0.4, 0, 0).applyQuaternion(q);
        set.term.push(new THREE.Matrix4().compose(new THREE.Vector3(c.x + off.x, c.y + h / 2, c.z + off.z), q, new THREE.Vector3(l * 0.2, h * 1.02, wdt * 1.02)));
      }
    };
    if (big.length) {
      const s = mk(500, 1700); sets.push(s);
      for (const [x0, y0, x1, y1] of big) {
        const w = (x1 - x0) * K, d = (y1 - y0) * K;
        addPassive(s, (x0 + x1) / 2, (y0 + y1) / 2, Math.max(w, d), Math.min(w, d), 1.0 * MM, 'cap', w > d ? 0 : Math.PI / 2);
      }
    }
    for (const z of zones) {
      const s = mk((z[0] + z[2]) / 2, (z[1] + z[3]) / 2); sets.push(s);
      const n = Math.round((z[2] - z[0]) * (z[3] - z[1]) / (side === 'A' ? 1100 : 1500));
      for (let i = 0, tries = 0; i < n && tries < n * 8; tries++) {
        const x = z[0] + R() * (z[2] - z[0]), y = z[1] + R() * (z[3] - z[1]);
        if ((side === 'A' ? occupiedA(x, y) : occupiedB(x, y)) || placed.some(([px, py]) => Math.abs(x - px) < 34 && Math.abs(y - py) < 24)) continue;
        placed.push([x, y]); i++;
        const bg = R() < 0.35, l = (bg ? 48 : 30) * K, wdt = (bg ? 24 : 15) * K;
        addPassive(s, x, y, l, wdt, (bg ? 0.8 : 0.5) * MM, R() < 0.55 ? 'cap' : 'res', R() < 0.5 ? 0 : Math.PI / 2);
      }
    }
    for (const set of sets) {
      const g = new THREE.Group(), wrap = new THREE.Group(); wrap.add(g);
      for (const [arr, mat] of [[set.cap, M.cap], [set.res, M.res], [set.term, M.tin]]) if (arr.length) g.add(inst(unit, mat, arr));
      if (side === 'B') wrap.scale.y = -1;
      group.add(wrap);
      const c = PX(side === 'B' ? mirX(set.cx) : set.cx, set.cy);
      comps.push({ g, wrap, side, dist: Math.hypot(c.x - u4c.x, c.z - u4c.z), h: 0.05, kind: 'passive' });
    }
  };
  passive(ZONES_A, BIGCAPS_A, 'A');
  passive(ZONES_B, [], 'B');

  const pos = ref => anchors[ref].g.position.clone();
  return {
    group, comps, bare: [board, bottom], bareMats: [top, edge, bottomMat],
    u4pos: u4c, U4_SIZE: (U4[2] - U4[0]) * K, MM,
    usbPos: anchors.usb, hdmiPos: anchors.hdmi, fx3Pos: pos('U2'), ddrPos: pos('U5'), flashPos: pos('U3'),
    itePos: pos('U1').setY(-(PCB_T / 2 + 0.9 * MM)), led: anchors.led, ledMat: anchors.ledMat,
  };
}
