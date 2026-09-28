// Tracewright's brand in one place: the name, the line that says what it is, the mark (a T drawn as
// two copper traces between pads) and the links. Every screen that shows the brand takes it from here.
import { h } from "./util.js";

export const BRAND = {
  name: "Tracewright",
  tagline: "KiCad boards, designed with Claude",
  pitch: "Design, check and order circuit boards with Claude, right inside KiCad.",
  repo: "",                                            // filled from /api/info (the release's GitHub repository)
};

let seq = 0;
// The mark as an SVG element. anim: the pads land, then the traces draw between them.
export function mark(size = 36, anim = false) {
  const id = `m${++seq}`;
  const el = h("span.brand-mark" + (anim ? ".anim" : ""), { html: `<svg width="${size}" height="${size}" viewBox="100 100 824 824" aria-hidden="true"><defs>
    <linearGradient id="${id}b" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2a3530"/><stop offset="1" stop-color="#121815"/></linearGradient>
    <linearGradient id="${id}c" x1="230" y1="270" x2="700" y2="840" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#ffc590"/><stop offset=".55" stop-color="#e8864a"/><stop offset="1" stop-color="#b95a26"/></linearGradient></defs>
    <rect class="bm-tile" x="100" y="100" width="824" height="824" rx="190" fill="url(#${id}b)"/>
    <g fill="none" stroke="url(#${id}c)" stroke-linecap="round" stroke-linejoin="round" stroke-width="84"><path class="bm-tr bm-t1" pathLength="1" d="M300 352H724"/><path class="bm-tr bm-t2" pathLength="1" d="M512 352V560L600 648V716"/></g>
    <g fill="url(#${id}c)"><circle class="bm-pad bm-p1" cx="276" cy="352" r="78"/><circle class="bm-pad bm-p2" cx="748" cy="352" r="78"/><circle class="bm-pad bm-p3" cx="600" cy="742" r="86"/></g>
    <circle class="bm-pad bm-p3" cx="600" cy="742" r="36" fill="#121815"/></svg>` });
  return el;
}

export function wordmark(size = 22) {
  return h("span.wordmark", mark(size), h("span.wm-name", BRAND.name));
}

// A person's initials, or their Google picture, in a circle.
export function avatar(user, size = 26) {
  const name = (user && (user.name || user.email)) || "?";
  const initials = name.split(/[\s@._-]+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "?";
  const hue = [...(user && user.email || name)].reduce((a, c) => (a * 31 + c.charCodeAt(0)) % 360, 7);
  const el = h("span.avatar", { style: { width: size + "px", height: size + "px", fontSize: Math.round(size * 0.42) + "px", background: `hsl(${hue} 42% 38%)` } }, initials);
  if (user && user.picture) {
    const img = h("img", { src: user.picture, alt: "", referrerpolicy: "no-referrer", onerror: () => img.remove() });
    el.appendChild(img);
  }
  return el;
}
