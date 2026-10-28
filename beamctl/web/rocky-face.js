// Rocky's face on the robot screen: two eyes on a 128x64 "OLED" grid, scaled up, with twelve emotions, blinks,
// glances, flourishes and a talking mouth.
//
// The drawing code and the face pack are from desk-robot by Casey (https://github.com/cgro00/desk-robot,
// server/brain/dashboard.html and server/faces/rocky.json), used under the MIT License:
//
//   Copyright (c) 2026 Casey (github.com/cgro00)
//   Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
//   documentation files (the "Software"), to deal in the Software without restriction, including without limitation
//   the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and
//   to permit persons to whom the Software is furnished to do so, subject to the following conditions: The above
//   copyright notice and this permission notice shall be included in all copies or substantial portions of the
//   Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED.
//
// Changes here: one built-in pack, no editor, painted in a colour the page chooses.
//
//   const face = RockyFace.mount(canvas);
//   face.setEmotion("happy"); face.setTalking(true); face.setLevel(0.1); face.setColor([57, 196, 119]);
window.RockyFace = (() => {
  const W = 128, H = 64;
  // Must match beambrain/rocky.py EMOTIONS.
  const EMOTIONS = ["neutral", "happy", "sad", "angry", "surprised", "sleepy", "thinking",
                    "curious", "skeptical", "excited", "love", "confused"];
  const PACK = {
    gap: 16, y: 0, cyclops: false, outline: false, glint: true, mouth: "talk",
    asleep: { w: 30, h: 3, rt: 1, rb: 1 },
    emotions: {
      neutral: { w: 34, h: 34, rt: 12, rb: 12 },
      happy: { w: 36, h: 36, rt: 12, rb: 12, cheek: 16, fx: ["blush"] },
      sad: { w: 32, h: 26, rt: 10, rb: 10, st: -9, lid: 4, fx: ["tear"] },
      angry: { w: 34, h: 24, rt: 8, rb: 8, st: 10, fx: ["steam", "shake"] },
      surprised: { w: 38, h: 44, rt: 19, rb: 19, fx: ["pop"], pop: 0.3 },
      sleepy: { w: 34, h: 26, rt: 10, rb: 10, lid: 14 },
      thinking: { w: 30, h: 30, rt: 11, rb: 11, lid: 5, fx: ["dots"], gaze: [7, -5] },
      curious: { w: 30, h: 30, rt: 11, rb: 11, right: { w: 38, h: 40, rt: 15, rb: 15, dy: -2 }, gaze: [-5, -3], pop: 0.15 },
      skeptical: { w: 34, h: 38, rt: 13, rb: 12, st: -3, right: { w: 34, h: 34, rt: 12, rb: 12, lid: 17 }, gaze: [4, 0] },
      excited: { w: 38, h: 40, rt: 16, rb: 16, cheek: 10, fx: ["sparkle", "bounce"], pop: 0.25 },
      love: { w: 36, h: 34, rt: 14, rb: 14, cheek: 12, fx: ["hearts", "blush"], pop: 0.2 },
      confused: { w: 32, h: 36, rt: 13, rb: 12, st: -5, right: { w: 28, h: 24, rt: 10, rb: 10, st: 4, dy: 4 },
                  fx: ["question", "wobble"] },
    },
  };
  const EYE_KEYS = ["w", "h", "rt", "rb", "st", "sb", "lid", "cheek", "dx", "dy"];
  const EYE_DEF = { w: 34, h: 34, rt: 12, rb: 12, st: 0, sb: 0, lid: 0, cheek: 0, dx: 0, dy: 0 };
  const BANG = ["###", "###", "###", "###", "###", "###", "###", "...", "###", "###"];
  const QUESTION = [".#####.", "##...##", "##...##", ".....##", "....##.", "...##..", "..##...", "..##...", ".......", "..##...", "..##..."];
  const HEART = [".##.##.", "#######", "#######", ".#####.", "..###..", "...#..."];
  const clamp = (x, lo, hi) => x < lo ? lo : x > hi ? hi : x;
  const rnd = (a, b) => a + Math.floor(Math.random() * (b - a));
  const approach = (c, t, f) => c + (t - c) * f;
  const fl = Math.floor;

  function readEye(o, base) {
    const e = { ...base };
    if (o) for (const k of EYE_KEYS) if (typeof o[k] === "number") e[k] = o[k];
    return e;
  }
  function resolve(p) {
    const r = { gap: p.gap, y: p.y, cyclops: p.cyclops, outline: p.outline, glint: p.glint,
                mouth: p.mouth === "always" ? 1 : p.mouth === "never" ? 2 : 0,
                asleep: readEye(p.asleep, EYE_DEF), looks: {} };
    for (const name of EMOTIONS) {
      const e = p.emotions[name];
      const l = readEye(e, EYE_DEF);
      r.looks[name] = { l, r: readEye(e.right, l), pop: e.pop ?? 0.12, smile: e.smile ?? 0,
                        gazeX: (e.gaze || [])[0] ?? 0, gazeY: (e.gaze || [])[1] ?? 0, fx: new Set(e.fx || []) };
    }
    return r;
  }
  function morph(c, t, f) { for (const k of EYE_KEYS) c[k] = approach(c[k], t[k], f); }

  function inEye(g, x, y) {
    if (x < g.left || x >= g.right || y < g.top || y >= g.bot) return false;
    const u = (x - g.cx) / (g.w * 0.5) * g.inner;
    const topCut = g.st >= 0 ? g.st * (u + 1) * 0.5 : -g.st * (1 - u) * 0.5;
    const botCut = g.sb >= 0 ? g.sb * (u + 1) * 0.5 : -g.sb * (1 - u) * 0.5;
    const hh = g.bot - g.top;
    const yy = y - topCut * (g.bot - y) / hh + botCut * (y - g.top) / hh;
    if (yy < g.top + g.lid || yy >= g.bot) return false;
    if (yy < g.top + g.rt) {
      const qx = clamp(x, g.left + g.rt, g.right - g.rt), dx = x - qx, dy = yy - (g.top + g.rt);
      if (dx * dx + dy * dy > g.rt * g.rt) return false;
    }
    if (yy > g.bot - g.rb) {
      const qx = clamp(x, g.left + g.rb, g.right - g.rb), dx = x - qx, dy = yy - (g.bot - g.rb);
      if (dx * dx + dy * dy > g.rb * g.rb) return false;
    }
    if (g.cheek > 0.5) {
      const rr = g.w, ccy = g.bot + rr - g.cheek, dx = x - g.cx, dy = y - ccy;
      if (dx * dx + dy * dy < rr * rr) return false;
    }
    return true;
  }

  class Sim {
    constructor(pack, emotion = "neutral", now = performance.now()) {
      this.pack = pack; this.buf = new Uint8Array(W * H);
      this.emotion = null; this.idle = true; this.asleep = false;
      this.talking = false; this.mouthLevel = 0; this.mouth = 0;
      this.blink = 0; this.blinkClosing = false;
      this.gazeX = 0; this.gazeY = 0; this.gtx = 0; this.gty = 0;
      this.pupilX = 0; this.pupilY = 0; this.ptx = 0; this.pty = 0; this.nextPupil = now + rnd(300, 1500);
      this.pop = 0; this.squint = 0; this.tearY = -1; this.thinkDots = 0; this.jitter = 0;
      this.hearts = [0, 1, 2].map(() => ({ alive: false, x: 0, y: 0, age: 0 })); this.heartSide = false;
      this.frame = 0;
      this.nextBlink = now + rnd(800, 4000); this.nextSaccade = now + rnd(500, 2500); this.nextSquint = now + rnd(4000, 12000);
      this.nextTear = 0; this.nextDot = 0; this.nextHeart = 0;
      this.since = now;
      this.setEmotion(emotion, now);
      const look = pack.looks[emotion];
      this.curL = { ...look.l }; this.curR = { ...look.r }; this.smile = look.smile; this.pop = 0;
    }
    setEmotion(e, now = performance.now()) {
      if (!this.pack.looks[e]) e = "neutral";
      if (e !== this.emotion) {
        this.pop = 1; this.since = now; this.tearY = -1; this.nextTear = now + 1500; this.thinkDots = 0;
        for (const h of this.hearts) h.alive = false;
        this.nextHeart = now + 200;
      }
      this.emotion = e;
      const look = this.pack.looks[e];
      this.gtx = look.gazeX; this.gty = look.gazeY;
    }

    step(now) {
      this.frame++;
      const look = this.pack.looks[this.emotion], P = this.pack;
      morph(this.curL, look.l, 0.25);
      morph(this.curR, look.r, 0.25);
      this.smile = approach(this.smile, look.smile, 0.25);
      this.pop *= 0.82; this.squint *= 0.85;

      if (this.blinkClosing) {
        this.blink += 0.45;
        if (this.blink >= 1) { this.blink = 1; this.blinkClosing = false; }
      } else if (this.blink > 0) this.blink = Math.max(0, this.blink - 0.28);

      if (this.idle) {
        if (now >= this.nextBlink) {
          this.blinkClosing = true; this.nextBlink = now + rnd(2200, 6000);
          if (rnd(0, 100) < 20) this.nextBlink = now + 400;
        }
        const looksSomewhere = look.gazeX !== 0 || look.gazeY !== 0;
        if (now >= this.nextSaccade && !looksSomewhere) {
          this.gtx = rnd(-8, 9); this.gty = rnd(-4, 5);
          if (rnd(0, 100) < 40) this.gtx = this.gty = 0;
          this.nextSaccade = now + rnd(1200, 4000);
        }
        if (now >= this.nextPupil) {
          if (rnd(0, 100) < 35) { this.ptx = rnd(-25, 26) / 100; this.pty = rnd(-20, 21) / 100; }
          else { this.ptx = rnd(-90, 91) / 100; this.pty = rnd(-60, 61) / 100; }
          this.nextPupil = now + rnd(600, 2600);
        }
        if (now >= this.nextSquint && this.emotion === "neutral") { this.squint = 1; this.nextSquint = now + rnd(8000, 16000); }
      }

      if (look.fx.has("tear")) {
        if (this.tearY < 0 && now >= this.nextTear) this.tearY = 0;
        if (this.tearY >= 0) {
          this.tearY += 0.7;
          if (this.tearY > H) { this.tearY = -1; this.nextTear = now + rnd(2500, 5000); }
        }
      }
      if (look.fx.has("dots") && now >= this.nextDot) { this.thinkDots = (this.thinkDots + 1) % 4; this.nextDot = now + 420; }
      if (look.fx.has("hearts") && now >= this.nextHeart) {
        for (const h of this.hearts) {
          if (!h.alive) {
            h.alive = true; h.age = 0; this.heartSide = !this.heartSide;
            h.x = this.heartSide ? W / 2 + P.gap / 2 + this.curR.w + 2 : W / 2 - P.gap / 2 - this.curL.w - 9;
            h.y = H / 2 + 6;
            break;
          }
        }
        this.nextHeart = now + rnd(600, 1000);
      }
      for (const h of this.hearts) {
        if (!h.alive) continue;
        h.age += 0.015; h.y -= 0.45;
        if (h.age >= 1 || h.y < -6) h.alive = false;
      }
      this.jitter = (look.fx.has("shake") && this.frame % 3 === 0) ? rnd(-1, 2) : 0;

      const mouthGoal = this.talking ? clamp(0.15 + this.mouthLevel * 4, 0.15, 1) : 0;
      this.mouth = approach(this.mouth, mouthGoal, this.talking ? 0.5 : 0.3);
      this.gazeX = approach(this.gazeX, this.gtx, 0.35); this.gazeY = approach(this.gazeY, this.gty, 0.35);
      this.pupilX = approach(this.pupilX, this.ptx, 0.5); this.pupilY = approach(this.pupilY, this.pty, 0.5);
    }

    dot(x, y, c) { if (x >= 0 && y >= 0 && x < W && y < H) this.buf[y * W + x] = c; }
    rect(x, y, w, h, c) { for (let j = 0; j < h; ++j) for (let i = 0; i < w; ++i) this.dot(x + i, y + j, c); }
    roundBox(x, y, w, h, r, c) {
      r = Math.min(r, Math.min(w, h) * 0.5);
      for (let j = 0; j < h; ++j) for (let i = 0; i < w; ++i) {
        const px = i + 0.5, py = j + 0.5, qx = clamp(px, r, w - r), qy = clamp(py, r, h - r);
        if ((px - qx) * (px - qx) + (py - qy) * (py - qy) <= r * r) this.dot(x + i, y + j, c);
      }
    }
    line(x0, y0, x1, y1) {
      const dx = Math.abs(x1 - x0), sx = x0 < x1 ? 1 : -1, dy = -Math.abs(y1 - y0), sy = y0 < y1 ? 1 : -1;
      let err = dx + dy;
      for (;;) {
        this.dot(x0, y0, 1);
        if (x0 === x1 && y0 === y1) break;
        const e2 = 2 * err;
        if (e2 >= dy) { err += dy; x0 += sx; }
        if (e2 <= dx) { err += dx; y0 += sy; }
      }
    }
    disc(cx, cy, r) { for (let j = -r; j <= r; ++j) for (let i = -r; i <= r; ++i) if (i * i + j * j <= r * r + r) this.dot(cx + i, cy + j, 1); }
    glyph(rows, x, y) { rows.forEach((row, j) => { for (let i = 0; i < row.length; ++i) if (row[i] === "#") this.dot(x + i, y + j, 1); }); }

    drawEye(e, cx, cy, inner, hScale) {
      const P = this.pack, w = e.w;
      let h = e.h * hScale * (1 - this.blink);
      if (h < 2) h = 2;
      const g = { cx: cx - inner * e.dx, w, st: e.st, sb: e.sb, lid: e.lid, cheek: e.cheek, inner };
      const ecy = cy + e.dy;
      g.left = g.cx - w * 0.5; g.right = g.cx + w * 0.5; g.top = ecy - h * 0.5; g.bot = ecy + h * 0.5;
      g.rt = Math.max(0, Math.min(e.rt, Math.min(w, h) * 0.5)); g.rb = Math.max(0, Math.min(e.rb, Math.min(w, h) * 0.5));
      const x0 = fl(g.left), x1 = Math.ceil(g.right), y0 = fl(g.top), y1 = Math.ceil(g.bot);
      for (let y = y0; y < y1; ++y) for (let x = x0; x < x1; ++x) {
        if (inEye(g, x + 0.5, y + 0.5)) this.dot(x, y, 1);
      }
      if (P.glint && h > 12) this.drawPupil(g, x0, x1, y0, y1);
    }
    drawPupil(g, x0, x1, y0, y1) {
      const r = clamp(g.w * 0.14, 3, 6);
      let top = -1, bot = -1;
      for (let y = y0; y < y1; ++y) if (inEye(g, g.cx, y + 0.5)) { if (top < 0) top = y; bot = y; }
      if (top < 0) return;
      const vcy = (top + bot + 1) * 0.5;
      let left = -1, right = -1;
      for (let x = x0; x < x1; ++x) if (inEye(g, x + 0.5, vcy)) { if (left < 0) left = x; right = x; }
      if (left < 0) return;
      const vcx = (left + right + 1) * 0.5;
      const roomX = Math.max(0, (right - left + 1) * 0.5 - r - 2), roomY = Math.max(0, (bot - top + 1) * 0.5 - r - 1.5);
      const px = clamp(this.pupilX + this.gazeX / 12, -1, 1), py = clamp(this.pupilY + this.gazeY / 8, -1, 1);
      const pcx = vcx + px * roomX, pcy = vcy + py * roomY, ri = Math.ceil(r);
      for (let y = fl(pcy) - ri; y <= fl(pcy) + ri; ++y) for (let x = fl(pcx) - ri; x <= fl(pcx) + ri; ++x) {
        const dx = x + 0.5 - pcx, dy = y + 0.5 - pcy;
        if (dx * dx + dy * dy <= r * r && inEye(g, x + 0.5, y + 0.5)) this.dot(x, y, 0);
      }
      if (r >= 4) {
        const hx = fl(pcx - r * 0.5), hy = fl(pcy - r * 0.55);
        if (inEye(g, hx + 0.5, hy + 0.5)) this.rect(hx, hy, 2, 2, 1);
      }
    }
    drawMouth(cx, cy) {
      const P = this.pack, my0 = fl(cy + this.curL.h * 0.5);
      if (this.mouth >= 0.05 && P.mouth !== 2) {
        const mh = 2 + fl(this.mouth * 9), mw = 20 + fl(this.mouth * 8);
        let my = my0 + 4;
        if (my + mh > H - 1) my = H - 1 - mh;
        this.roundBox(cx - Math.trunc(mw / 2), my, mw, mh, Math.min(Math.trunc(mh / 2), 4), 1);
        if (mh > 6) this.roundBox(cx - Math.trunc(mw / 2) + 3, my + 2, mw - 6, mh - 4, 2, 0);
      }
    }
    drawFlourishes(leftOuter, rightOuter, eyeTop, eyeBottom, now) {
      const fx = this.pack.looks[this.emotion].fx, since = now - this.since;
      const lo = fl(leftOuter), ro = fl(rightOuter);
      if (fx.has("pop") && since < 900) this.glyph(BANG, 116, 4);
      if (fx.has("blush")) for (let i = 0; i < 2; ++i) {
        const lx = lo - 8 + i * 3, rx = ro + 3 + i * 3;
        this.line(lx, eyeBottom + 1, lx + 3, eyeBottom - 3); this.line(rx, eyeBottom + 1, rx + 3, eyeBottom - 3);
      }
      if (fx.has("tear") && this.tearY >= 0) {
        const tx = ro - 3, ty = eyeBottom + fl(this.tearY);
        this.disc(tx, ty, 2); this.line(tx, ty - 5, tx, ty - 2);
      }
      if (fx.has("dots")) for (let i = 0; i < this.thinkDots; ++i) this.disc(W - 26 + i * 8, 9, 2);
      if (fx.has("steam") && fl(this.frame / 6) % 2 === 0) {
        this.line(lo - 4, eyeTop - 4, lo - 2, eyeTop - 9); this.line(ro + 4, eyeTop - 4, ro + 2, eyeTop - 9);
      }
      if (fx.has("hearts")) for (const h of this.hearts) {
        if (!h.alive || (h.age > 0.8 && this.frame % 2 === 0)) continue;
        this.glyph(HEART, fl(h.x), fl(h.y));
      }
      if (fx.has("question")) this.glyph(QUESTION, 112, 3 + fl(Math.sin(this.frame * 0.15) * 2));
      if (fx.has("sparkle")) {
        const a = fl(this.frame / 5) % 2 === 0;
        const pts = [a ? lo - 6 : lo - 5, a ? eyeTop + 3 : eyeBottom - 5, a ? ro + 6 : ro + 5, a ? eyeBottom - 4 : eyeTop + 2];
        for (let k = 0; k < 2; ++k) {
          const x = pts[k * 2], y = pts[k * 2 + 1];
          this.line(x - 2, y, x + 2, y); this.line(x, y - 2, x, y + 2);
        }
      }
    }
    render(now = performance.now()) {
      this.buf.fill(0);
      const P = this.pack, look = P.looks[this.emotion];
      let bob = -this.mouth * 2;
      if (look.fx.has("bounce")) bob -= Math.abs(Math.sin(this.frame * 0.18)) * 3;
      const sway = look.fx.has("wobble") ? Math.sin(this.frame * 0.08) * 3 : 0;
      let cy = H / 2 + P.y + this.gazeY + bob;
      if (this.talking) cy -= 2;
      const gx = this.gazeX + this.jitter + sway;
      let hScale = 1 + this.pop * look.pop;
      hScale *= 1 - this.squint * 0.4;
      const lcx = W / 2 - P.gap / 2 - this.curL.w / 2 + gx, rcx = W / 2 + P.gap / 2 + this.curR.w / 2 + gx;
      this.drawEye(this.curL, lcx, cy, 1, hScale); this.drawEye(this.curR, rcx, cy, -1, hScale);
      const leftOuter = lcx - this.curL.dx - this.curL.w / 2, rightOuter = rcx + this.curR.dx + this.curR.w / 2;
      const eyeTop = fl(cy - this.curL.h * hScale / 2), eyeBottom = fl(cy + this.curL.h * hScale / 2);
      this.drawFlourishes(leftOuter, rightOuter, eyeTop, eyeBottom, now);
      this.drawMouth(W / 2 + fl(this.gazeX * 0.5), fl(cy));
      return this.buf;
    }
  }

  // Draws at 30 frames a second (the animation is tuned per frame, as on desk-robot's screen).
  function mount(canvas) {
    canvas.width = W; canvas.height = H;
    const ctx = canvas.getContext("2d"), img = ctx.createImageData(W, H);
    const sim = new Sim(resolve(PACK));
    let color = [233, 237, 242], last = 0;
    function frame(now) {
      requestAnimationFrame(frame);
      if (now - last < 1000 / 30) return;
      last = now;
      sim.step(now);
      const buf = sim.render(now), d = img.data;
      for (let i = 0; i < W * H; ++i) {
        const o = i * 4;
        d[o] = color[0]; d[o + 1] = color[1]; d[o + 2] = color[2]; d[o + 3] = buf[i] ? 255 : 0;
      }
      ctx.putImageData(img, 0, 0);
    }
    requestAnimationFrame(frame);
    return {
      setEmotion: (e) => sim.setEmotion(EMOTIONS.includes(e) ? e : "neutral"),
      setTalking: (on) => { sim.talking = !!on; if (!on) sim.mouthLevel = 0; },
      setLevel: (x) => { sim.mouthLevel = x; },
      setColor: (rgb) => { color = rgb; },
      get emotion() { return sim.emotion; },
    };
  }

  return { W, H, EMOTIONS, mount };
})();
