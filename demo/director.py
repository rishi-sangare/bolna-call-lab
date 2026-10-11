"""Demo director: drives the Call Lab in Edge (kiosk, 1920x1080) and screen-records a ~5 minute walkthrough.

    .venv\\Scripts\\python -m demo.director

* The cursor is drawn inside the page (60 fps, eased, slightly curved) - the OS cursor is not recorded,
  so there is no flicker. Clicks are real clicks dispatched at the same point.
* Two calls are placed live against the real Bolna engine; the others are opened from saved runs.
* Every scene start is logged to recordings/<stamp>/scenes.json with its narration line, so the
  transcript and the voice-over studio line up with the video.
Hands off the mouse and keyboard while it runs.
"""
from __future__ import annotations

import json
import math
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import sys

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
LAB = "http://127.0.0.1:7860"
WPS = 2.45                                           # comfortable speaking pace, words per second

CURSOR_JS = r"""
(() => {
  if (window.__cur) return;
  const make = () => {
    const c = document.createElement('div');
    c.id = '__cur';
    c.innerHTML = '<svg width="30" height="30" viewBox="0 0 30 30"><path d="M5 3 L22 13.2 L14.6 14.9 L18.8 22.8 L15.4 24.6 L11.2 16.6 L5.6 21.6 Z" fill="#14213D" stroke="#ffffff" stroke-width="1.7" stroke-linejoin="round"/></svg>';
    Object.assign(c.style, {position: 'fixed', left: '0', top: '0', width: '30px', height: '30px', zIndex: 2147483647,
      pointerEvents: 'none', filter: 'drop-shadow(0 2px 3px rgba(0,0,0,.28))', willChange: 'transform'});
    document.documentElement.appendChild(c);
    return c;
  };
  let c = null;
  let saved = null;
  try { saved = JSON.parse(sessionStorage.getItem('__curpos') || 'null'); } catch (e) {}
  let x = saved ? saved[0] : 1500, y = saved ? saved[1] : 620;
  const apply = () => { if (!c) c = make(); c.style.transform = `translate(${x - 5}px, ${y - 3}px)`; };
  const ease = (t) => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  window.__cur = {
    pos: () => [x, y],
    set(nx, ny) { x = nx; y = ny; apply(); try { sessionStorage.setItem('__curpos', JSON.stringify([x, y])); } catch (e) {} },
    move(tx, ty, ms) {
      const anim = new Promise((res) => {
        const sx = x, sy = y, t0 = performance.now();
        const lift = Math.min(46, Math.hypot(tx - sx, ty - sy) * 0.07);
        const step = (now) => {
          const k = Math.min(1, (now - t0) / ms), e = ease(k);
          this.set(sx + (tx - sx) * e, sy + (ty - sy) * e - Math.sin(Math.PI * k) * lift);
          if (k < 1) requestAnimationFrame(step); else res();
        };
        requestAnimationFrame(step);
      });
      // never hang if the browser throttles animation frames: land on the target after ms + 400
      const guard = new Promise((res) => setTimeout(() => { this.set(tx, ty); res(); }, ms + 400));
      return Promise.race([anim, guard]);
    },
    ripple() {
      const r = document.createElement('div');
      Object.assign(r.style, {position: 'fixed', left: (x - 20) + 'px', top: (y - 20) + 'px', width: '40px', height: '40px',
        borderRadius: '50%', border: '3px solid #0E9F8E', zIndex: 2147483646, pointerEvents: 'none',
        transition: 'transform .5s ease-out, opacity .5s ease-out', transform: 'scale(.25)', opacity: '1'});
      document.documentElement.appendChild(r);
      requestAnimationFrame(() => requestAnimationFrame(() => { r.style.transform = 'scale(1.35)'; r.style.opacity = '0'; }));
      setTimeout(() => r.remove(), 600);
    },
    scrollTo(top, ms) {
      const anim = new Promise((res) => {
        const s0 = window.scrollY, t0 = performance.now();
        const step = (now) => { const k = Math.min(1, (now - t0) / ms); window.scrollTo(0, s0 + (top - s0) * ease(k));
          if (k < 1) requestAnimationFrame(step); else res(); };
        requestAnimationFrame(step);
      });
      const guard = new Promise((res) => setTimeout(() => { window.scrollTo(0, top); res(); }, ms + 400));
      return Promise.race([anim, guard]);
    },
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', apply); else apply();
})();
"""


@dataclass
class Scene:
    id: str
    caption: str
    narration: str
    min_s: float = 0.0

    @property
    def duration(self) -> float:
        return max(self.min_s, len(self.narration.split()) / WPS + 1.0)


SCENES = [
    Scene("intro", "Calls drop after ~12 seconds",
          "Hi! I saw your post about your Bolna calls dropping after about twelve seconds. Before applying, I wanted to "
          "understand exactly why that happens, so I built a small lab around Bolna itself. Let me show you what I found."),
    Scene("why12", "Bolna's silence watchdog: 10 s + a 2 s check",
          "This is the heart of it. Inside Bolna there's a watchdog that checks the call every two seconds. If nobody "
          "has been heard, not the agent and not the caller, for ten seconds, it hangs up. Ten seconds, plus up to two "
          "seconds until the next check. That's your twelve. On a healthy call this never fires, so a clean "
          "twelve-second drop usually means the audio is quietly broken somewhere in the pipeline."),
    Scene("setup", "Real Bolna engine + a Twilio-protocol caller",
          "On the left is the real open-source Bolna engine, running on my machine, with ElevenLabs for speech and voice, "
          "and OpenAI's open gpt-oss model on Groq as the language model. Instead of a phone, I call it with a small client that speaks "
          "Twilio's exact media-stream protocol, so to Bolna it's a normal phone call. Each scenario here is one way a "
          "call can die."),
    Scene("live-silence", "Live call: a healthy call that still gets cut",
          "Let's start with the sneakiest one, a perfectly healthy call. The agent greets the caller, the caller asks "
          "about a delivery, and then says, give me a second, let me find the order number. Now watch the timeline. "
          "Nobody is speaking. The silence builds up, the watchdog keeps checking, and there it is. Bolna ends the call "
          "while the caller is still looking for their order number.", 12),
    Scene("diag-silence", "Diagnosis from Bolna's own logs",
          "The diagnosis reads Bolna's own logs: just over ten seconds of silence, against a ten second limit. "
          "And the are-you-still-there check is also set to ten, so it never gets a chance to play. The fix is two "
          "numbers. Allow twenty-five seconds of silence, and check in at eight."),
    Scene("tts", "Same symptom, different cause: the voice never plays",
          "Here's the same drop with a completely different cause. The ElevenLabs voice ID doesn't exist in the account, "
          "so the caller hears dead air. You can see the voice errors stacking up, and ten seconds later the watchdog "
          "ends the call."),
    Scene("stt", "Speech-to-text account rejected (HTTP 402)",
          "This one actually happened to me while building this. My Deepgram project ran out of credits, Deepgram "
          "rejected the stream with a four-oh-two, and Bolna ended the call after six seconds as a transcriber error. "
          "If billing can silently drop every call, you want an alert on that."),
    Scene("inbound", "Caller audio never reaches Bolna",
          "And here the caller's audio never reaches Bolna at all. The greeting plays fine, but the agent can't hear "
          "anything. The pipeline check stops right at caller audio flowing, which points straight at the telephony "
          "side."),
    Scene("live-fixed", "Live call: the fixed configuration",
          "Now the fixed configuration. Same caller, same pause. The agent answers, the caller goes quiet to look for "
          "the order number, and this time, at eight seconds, the agent gently checks in instead of hanging up. The "
          "caller comes back, moves the delivery to Saturday, and hangs up normally. No early disconnect. While it runs, "
          "keep an eye on the response time panel. Every reply is broken down into speech-to-text, the language model, "
          "and the voice.", 20),
    Scene("latency", "Where the response time goes",
          "The language model and the voice together take around six hundred milliseconds. The biggest share, about a "
          "second, is the speech-to-text deciding the caller has finished speaking, so that's the first setting I'd "
          "tune on your agent."),
    Scene("outro", "Send one execution ID",
          "So here's how I'd work on yours. Send me one or two execution IDs from Bolna, and I'll run them through this "
          "same diagnosis. Usually within an hour I can tell you which of these it is. Then I'll fix it, tune the "
          "latency, and add monitoring so it doesn't come back. Thanks for watching, I'd love to help."),
]


class Director:
    def __init__(self):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.out = ROOT / "recordings" / stamp
        self.out.mkdir(parents=True, exist_ok=True)
        self.marks: list[dict] = []
        self.live_runs: list[str] = []
        self.ff = None
        self.t0 = 0.0
        self.scene_end = 0.0

    # ------------------------------------------------------------------ recording
    def start_recording(self) -> None:
        import imageio_ffmpeg
        cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
               "-i", "ddagrab=output_idx=0:framerate=30:draw_mouse=0",
               "-vf", "hwdownload,format=bgra,format=yuv420p",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(self.out / "raw.mp4")]
        self.ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, creationflags=0x08000000)
        self.t0 = time.time() + 0.45
        (self.out / "rec.json").write_text(json.dumps({"rec_t0": self.t0}), encoding="utf-8")

    def stop_recording(self) -> None:
        if self.ff and self.ff.poll() is None:
            self.ff.stdin.write(b"q")
            self.ff.stdin.flush()
            self.ff.wait(30)
        (self.out / "scenes.json").write_text(json.dumps({
            "total": round(time.time() - self.t0, 2), "rec_t0": self.t0, "scenes": self.marks,
            "live_runs": self.live_runs}, indent=2), encoding="utf-8")

    @staticmethod
    def fullscreen(ctx, page) -> None:
        """True fullscreen through the DevTools protocol (command-line kiosk flags are ignored here)."""
        cdp = ctx.new_cdp_session(page)
        wid = cdp.send("Browser.getWindowForTarget")["windowId"]
        cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
        cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "fullscreen"}})
        time.sleep(1.2)

    @staticmethod
    def foreground() -> None:
        """Make sure the kiosk window is the active, top-most window before recording."""
        import ctypes
        import win32gui
        u = ctypes.windll.user32
        hits = []
        win32gui.EnumWindows(lambda h, _: hits.append(h) if win32gui.IsWindowVisible(h) and
                             "Call Lab" in win32gui.GetWindowText(h) else None, None)
        for h in hits[:1]:
            u.keybd_event(0x12, 0, 0, 0)                     # Alt tap releases the foreground lock
            u.keybd_event(0x12, 0, 2, 0)
            win32gui.ShowWindow(h, 3)
            win32gui.SetForegroundWindow(h)
        u.SetCursorPos(1919, 1079)                           # park the real cursor (it is not recorded)

    # ------------------------------------------------------------------ helpers
    def scene(self, sc: Scene) -> None:
        t = time.time() - self.t0
        self.marks.append({"id": sc.id, "t": round(t, 2), "caption": sc.caption, "narration": sc.narration,
                           "duration": round(sc.duration, 2)})
        print(f"[{t:6.1f}s] {sc.id}", flush=True)
        self.scene_end = time.time() + sc.duration

    def hold(self, extra: float = 0.0) -> None:
        remaining = self.scene_end - time.time() + extra
        if remaining > 0:
            time.sleep(remaining)

    def move(self, page, x: float, y: float, ms: int = 750) -> None:
        page.evaluate(f"window.__cur.move({x}, {y}, {ms})")

    def move_to(self, page, selector: str, fx: float = 0.5, fy: float = 0.5, ms: int = 750) -> tuple[float, float]:
        box = page.locator(selector).first.bounding_box()
        x, y = box["x"] + box["width"] * fx, box["y"] + box["height"] * fy
        self.move(page, x, y, ms)
        return x, y

    def click(self, page, selector: str, fx: float = 0.5, fy: float = 0.5, ms: int = 750) -> None:
        x, y = self.move_to(page, selector, fx, fy, ms)
        time.sleep(0.12)
        page.evaluate("window.__cur.ripple()")
        page.mouse.click(x, y)
        time.sleep(0.25)

    def wait_call_done(self, page, timeout: float = 120) -> str | None:
        end = time.time() + timeout
        while time.time() < end:
            st = httpx.get(f"{LAB}/api/state", timeout=5).json()
            done = page.evaluate("typeof S !== 'undefined' && !S.live && !!S.shown")
            if not st["busy"] and done:
                return page.evaluate("S.shown && S.shown.run_id")
            time.sleep(0.4)
        return None

    def latest_run(self, scenario: str) -> str:
        runs = httpx.get(f"{LAB}/api/state", timeout=5).json()["runs"]
        return next(r["id"] for r in runs if r["scenario"] == scenario)

    # ------------------------------------------------------------------ the walkthrough
    def run(self) -> None:
        S = {s.id: s for s in SCENES}
        tts_run, stt_run, inb_run = (self.latest_run(x) for x in ("tts-broken", "stt-rejected", "inbound-dead"))
        sil_prev = self.latest_run("silence-tight")
        with sync_playwright() as p:
            browser = p.chromium.launch(
                channel="msedge", headless=False,
                args=["--start-fullscreen", "--kiosk", "--force-device-scale-factor=1",
                      "--no-first-run", "--disable-infobars", "--autoplay-policy=no-user-gesture-required",
                      "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
                      "--disable-background-timer-throttling"],
                ignore_default_args=["--enable-automation"])
            ctx = browser.new_context(no_viewport=True)
            ctx.add_init_script(CURSOR_JS)
            page = ctx.new_page()
            page.goto(LAB)
            page.bring_to_front()
            self.fullscreen(ctx, page)
            self.foreground()
            page.wait_for_function("typeof S !== 'undefined' && S.state && S.state.scenarios")
            page.evaluate(f"openRun('{sil_prev}')")
            time.sleep(2.5)
            self.start_recording()
            time.sleep(0.8)

            # 1. intro on a finished report: the red cut is the hook
            self.scene(S["intro"])
            time.sleep(2.0)
            self.move_to(page, "#tape", 0.83, 0.18, 1200)
            time.sleep(1.5)
            self.move_to(page, "#diag .diag-title", 0.3, 0.5, 1000)
            self.hold()

            # 2. why 12 seconds - the architecture page
            self.scene(S["why12"])
            page.goto(f"{LAB}/plan/index.html")
            page.wait_for_load_state("networkidle")
            page.evaluate("document.documentElement.style.scrollBehavior='auto'")
            top = page.evaluate("document.getElementById('why12').getBoundingClientRect().top + window.scrollY - 30")
            page.evaluate(f"window.__cur.scrollTo({top}, 1600)")
            time.sleep(1.8)
            self.move_to(page, "#why12 .diagram", 0.23, 0.36, 1100)
            time.sleep(4)
            self.move_to(page, "#why12 .diagram", 0.6, 0.6, 1600)
            time.sleep(4)
            self.move_to(page, "#why12 .diagram", 0.9, 0.2, 1400)
            self.hold()

            # 3. setup - back in the lab
            self.scene(S["setup"])
            page.goto(LAB)
            page.wait_for_function("typeof S !== 'undefined' && S.state && S.state.scenarios")
            self.click(page, ".scn[data-id='silence-tight']", 0.4, 0.5, 900)
            self.move_to(page, "#status", 0.5, 0.25, 900)
            time.sleep(3.5)
            self.move_to(page, "#status", 0.5, 0.85, 900)
            time.sleep(4)
            self.move_to(page, "#scenarios", 0.4, 0.15, 900)
            time.sleep(2.5)
            self.move_to(page, "#scenarios", 0.4, 0.85, 1600)
            self.hold()

            # 4. live call: healthy call, caller pauses
            self.scene(S["live-silence"])
            self.click(page, "#placeCall", 0.5, 0.5, 900)
            time.sleep(3)
            self.move_to(page, "#convo", 0.5, 0.4, 1200)
            time.sleep(9)
            self.move_to(page, "#tape", 0.55, 0.45, 1400)
            run = self.wait_call_done(page, 90)
            if run:
                self.live_runs.append(run)
            self.hold()

            # 5. diagnosis
            self.scene(S["diag-silence"])
            self.move_to(page, "#diag .diag-title", 0.35, 0.5, 900)
            time.sleep(4)
            self.move_to(page, "#diag .ev-list", 0.3, 0.3, 900)
            time.sleep(5)
            self.move_to(page, "#diag .fix", 0.4, 0.5, 900)
            self.hold()

            # 6-8. saved runs
            for sid, run_id, targets in (("tts", tts_run, ["#tape|0.62|0.62", "#diag .diag-title|0.3|0.5"]),
                                          ("stt", stt_run, ["#tape|0.2|0.55", "#diag .diag-title|0.3|0.5"]),
                                          ("inbound", inb_run, ["#chain|0.3|0.2", "#diag .diag-title|0.3|0.5"])):
                self.scene(S[sid])
                hist = f"#history li[data-id='{run_id}']"
                self.click(page, hist, 0.35, 0.5, 1000)
                time.sleep(1.2)
                for tgt in targets:
                    sel, fx, fy = tgt.split("|")
                    self.move_to(page, sel, float(fx), float(fy), 1000)
                    time.sleep(max(2.5, (S[sid].duration - 4) / len(targets) - 1))
                self.hold()

            # 9. live call: fixed configuration
            self.scene(S["live-fixed"])
            self.click(page, ".scn[data-id='fixed']", 0.4, 0.5, 1000)
            time.sleep(0.6)
            self.click(page, "#placeCall", 0.5, 0.5, 900)
            time.sleep(4)
            self.move_to(page, "#convo", 0.5, 0.5, 1200)
            time.sleep(14)
            self.move_to(page, "#tape", 0.5, 0.45, 1400)
            run = self.wait_call_done(page, 120)
            if run:
                self.live_runs.append(run)
            self.hold()

            # 10. latency
            self.scene(S["latency"])
            self.move_to(page, "#latencyPanel", 0.5, 0.15, 900)
            page.evaluate("document.querySelector('#latencyPanel').scrollIntoView({behavior:'smooth', block:'center'})")
            time.sleep(1.5)
            self.move_to(page, "#latency .lat-legend", 0.3, 0.5, 900)
            time.sleep(3.5)
            self.move_to(page, "#latency .lat-note", 0.4, 0.5, 900)
            self.hold()

            # 11. outro
            self.scene(S["outro"])
            page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
            time.sleep(1.2)
            self.move_to(page, "#history", 0.4, 0.3, 1200)
            self.hold(0.8)
            self.stop_recording()
            browser.close()
        print("recorded to", self.out)


def smoke() -> None:
    """15-second check: kiosk window up, cursor animates, nothing hangs."""
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=False, args=[
            "--start-fullscreen", "--kiosk", "--force-device-scale-factor=1", "--no-first-run",
            "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling"], ignore_default_args=["--enable-automation"])
        ctx = b.new_context(no_viewport=True)
        ctx.add_init_script(CURSOR_JS)
        page = ctx.new_page()
        page.goto(LAB)
        page.bring_to_front()
        Director.fullscreen(ctx, page)
        Director.foreground()
        t = time.time()
        page.evaluate("window.__cur.move(400, 300, 900)")
        page.evaluate("window.__cur.move(1500, 800, 900)")
        print("two moves took", round(time.time() - t, 2), "s; viewport", page.evaluate("[innerWidth, innerHeight]"),
              "visible", page.evaluate("document.visibilityState"), flush=True)
        page.screenshot(path=str(ROOT / "recordings" / "smoke.png"))
        b.close()


if __name__ == "__main__":
    smoke() if "--smoke" in sys.argv else Director().run()
