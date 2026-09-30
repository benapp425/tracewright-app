// Dictation for the message box: in the Mac app, Apple's speech recognizer (on this Mac when it can); in a
// browser, its Web Speech API. Either way the callbacks get the whole text heard so far (onText(text, final)),
// the microphone's loudness (onLevel, 0..1, the app only) and the end (onEnd(message|null)).
import { isNative, native, events } from "./native.js";

const SR = typeof window !== "undefined" && (window.SpeechRecognition || window.webkitSpeechRecognition);

export function dictationAvailable() { return isNative || !!SR; }

let current = null;                      // one at a time, like the microphone
events.on("dictation", (m) => { if (current && current.native) current.nativeEvent(m); });

export class Dictation {
  constructor({ onText, onLevel, onStart, onEnd }) {
    this.onText = onText || (() => {}); this.onLevel = onLevel || (() => {});
    this.onStart = onStart || (() => {}); this.onEnd = onEnd || (() => {});
    this.native = isNative; this.on = false;
  }

  start() {
    if (current && current !== this) current.cancel();
    current = this; this.on = true; this.ended = false;
    const lang = navigator.language || "en-US";
    if (this.native) { native.dictate("start", { locale: lang }); return; }
    if (!SR) { this.end("Dictation isn't available in this browser."); return; }
    const r = this.rec = new SR();
    r.continuous = true; r.interimResults = true; r.lang = lang;
    r.onstart = () => this.onStart({ onDevice: false });
    r.onresult = (e) => {
      let text = "", final = true;
      for (let i = 0; i < e.results.length; i++) { text += e.results[i][0].transcript; if (!e.results[i].isFinal) final = false; }
      this.onText(text.replace(/\s+/g, " ").trim(), final);
    };
    r.onerror = (e) => {
      this.err = e.error === "not-allowed" || e.error === "service-not-allowed" ? "The browser blocked the microphone. Allow it for this page and try again."
        : e.error === "no-speech" || e.error === "aborted" ? null
        : e.error === "audio-capture" ? "No microphone found."
        : e.error === "network" ? "Dictation needs the speech service, which could not be reached."
        : e.error === "language-not-supported" ? "Dictation does not support this language here."
        : `Dictation stopped (${e.error}).`;
    };
    r.onend = () => this.end(this.err || null);
    try { r.start(); } catch (e) { this.end(e.message); }
  }

  // settle what was heard and stop listening
  stop() {
    if (!this.on) return;
    if (this.native) native.dictate("stop");
    else if (this.rec) this.rec.stop();
  }

  // stop and drop what was heard
  cancel() {
    if (!this.on) return;
    this.cancelled = true;
    if (this.native) native.dictate("cancel");
    else if (this.rec) this.rec.abort();
    this.end(null);
  }

  nativeEvent(m) {
    if (m.phase === "start") this.onStart({ onDevice: !!m.onDevice });
    else if (m.phase === "level") this.onLevel(m.level || 0);
    else if ((m.phase === "partial" || m.phase === "final") && !this.cancelled) this.onText(m.text || "", m.phase === "final");
    else if (m.phase === "end") this.end(m.message || null);
  }

  end(message) {
    if (this.ended) return;
    this.ended = true; this.on = false;
    if (current === this) current = null;
    this.onEnd(message);
  }
}
