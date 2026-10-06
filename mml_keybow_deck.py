#!/usr/bin/env python3
"""
MML Keybow Deck - runs on the Pi Zero W fitted to the Pimoroni Keybow.

* Reads buttons.json (extended Presto-Stream-Deck schema)
* Publishes a normalised copy (colours resolved to RGB, actions stripped)
  as a RETAINED message on deck/config so the Presto can render from it
* Lights each Keybow key with that button's bg_color
* Executes actions for presses from either the Keybow keys or the Presto
  (deck/press). Actions: hid, text, consumer, mqtt, page

Run:  sudo python3 mml_keybow_deck.py --config buttons.json
Test without hardware:  python3 mml_keybow_deck.py --dry-run
Find your key numbering:  sudo python3 mml_keybow_deck.py --identify
"""
import argparse
import json
import os
import signal
import threading
import time

import paho.mqtt.client as mqtt

# ---------------------------------------------------------------- settings
BROKER = os.environ.get("DECK_BROKER", "192.168.1.15")
PORT = int(os.environ.get("DECK_PORT", "1883"))
MQTT_USER = os.environ.get("DECK_USER")
MQTT_PASS = os.environ.get("DECK_PASS")

HID_KEYBOARD = "/dev/hidg0"
HID_CONSUMER = "/dev/hidg1"

# Keybow key numbering as laid out on the board, written as rows (top row
# first) x columns. THIS IS AN ASSUMPTION (index 0 bottom-left, counting up
# each column). Run with --identify and edit if your keys differ.
HW_INDEX_GRID = [
    [9, 10, 11],
    [6, 7, 8],
    [3, 4, 5],
    [0, 1, 2],
]

T_CONFIG = "deck/config"      # retained, hub -> presto
T_PAGE = "deck/page"          # retained, hub -> presto
T_EVENT = "deck/event"        # hub -> anyone (feedback / logging)
T_STATUS = "deck/hub/status"  # retained online/offline (LWT)
T_PRESS = "deck/press"        # presto -> hub  {"page":0,"row":1,"column":2}
T_CMD = "deck/cmd/#"          # deck/cmd/page <int|next|prev>, deck/cmd/reload

# ------------------------------------------------------------------ colour
NAMED = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0),
    "green": (0, 255, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0),
    "magenta": (255, 0, 255), "aqua": (0, 255, 255), "cyan": (0, 255, 255),
    "orange": (255, 128, 0), "purple": (128, 0, 255),
    "grey": (128, 128, 128), "gray": (128, 128, 128),
}


def to_rgb(value, default=None):
    """Colour name, '#rrggbb' or [r,g,b] -> [r,g,b]."""
    if value is None:
        return list(default) if default is not None else None
    if isinstance(value, str):
        s = value.strip().lower()
        if s in NAMED:
            return list(NAMED[s])
        if s.startswith("#") and len(s) == 7:
            return [int(s[i:i + 2], 16) for i in (1, 3, 5)]
        raise ValueError("bad colour %r" % value)
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return [max(0, min(255, int(x))) for x in value]
    raise ValueError("bad colour %r" % (value,))


# --------------------------------------------------------------------- HID
MOD = {
    "ctrl": 0x01, "control": 0x01, "shift": 0x02, "alt": 0x04,
    "gui": 0x08, "win": 0x08, "cmd": 0x08, "super": 0x08, "meta": 0x08,
    "rctrl": 0x10, "rshift": 0x20, "ralt": 0x40, "altgr": 0x40, "rgui": 0x80,
}

KEYS = {chr(97 + i): 0x04 + i for i in range(26)}
KEYS.update({str(d): (0x27 if d == 0 else 0x1D + d) for d in range(10)})
KEYS.update({
    "enter": 0x28, "return": 0x28, "esc": 0x29, "escape": 0x29,
    "backspace": 0x2A, "tab": 0x2B, "space": 0x2C, "minus": 0x2D,
    "equal": 0x2E, "lbracket": 0x2F, "rbracket": 0x30, "backslash": 0x31,
    "semicolon": 0x33, "quote": 0x34, "grave": 0x35, "comma": 0x36,
    "period": 0x37, "slash": 0x38, "capslock": 0x39,
    "printscreen": 0x46, "scrolllock": 0x47, "pause": 0x48, "insert": 0x49,
    "home": 0x4A, "pageup": 0x4B, "delete": 0x4C, "end": 0x4D,
    "pagedown": 0x4E, "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52,
})
KEYS.update({"f%d" % n: 0x3A + n - 1 for n in range(1, 13)})
KEYS.update({"f%d" % n: 0x68 + n - 13 for n in range(13, 25)})

CONSUMER = {
    "play_pause": 0x00CD, "next": 0x00B5, "prev": 0x00B6, "stop": 0x00B7,
    "mute": 0x00E2, "vol_up": 0x00E9, "vol_down": 0x00EA,
}

_PLAIN = "-=[]\\;'`,./"
_SHIFTED = "_+{}|:\"~<>?"
_PLAIN_CODES = [0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38]
_NUM_SHIFTED = ")!@#$%^&*("


# HID codes are physical key positions; the laptop's layout decides what
# character appears. These tables are the per-layout differences.
# UK: Shift+2 = ", Shift+3 = £, Shift+' = @, # has its own key (0x32),
# \ and | are on the extra key beside left Shift (0x64), Shift+` = ¬.
_UK_OVERRIDES = {
    '"': (0x02, 0x1F),   # Shift+2
    "£": (0x02, 0x20),   # Shift+3
    "@": (0x02, 0x34),   # Shift+'
    "#": (0x00, 0x32),
    "~": (0x02, 0x32),
    "\\": (0x00, 0x64),
    "|": (0x02, 0x64),
    "¬": (0x02, 0x35),
    "€": (0x40, 0x21),   # AltGr+4
}


def char_to_key(ch, layout="uk"):
    """char -> (modifier, keycode) for the laptop's layout ('uk' or 'us').
    Returns None if unsupported."""
    if layout == "uk" and ch in _UK_OVERRIDES:
        return _UK_OVERRIDES[ch]
    if ch == "\n":
        return 0, KEYS["enter"]
    if ch == "\t":
        return 0, KEYS["tab"]
    if ch == " ":
        return 0, KEYS["space"]
    if ch.isascii() and ch.isalpha():
        return (0x02 if ch.isupper() else 0), KEYS[ch.lower()]
    if ch.isascii() and ch.isdigit():
        return 0, KEYS[ch]
    if ch in _NUM_SHIFTED:
        return 0x02, KEYS[str(_NUM_SHIFTED.index(ch))]
    if ch in _PLAIN:
        return 0, _PLAIN_CODES[_PLAIN.index(ch)]
    if ch in _SHIFTED:
        return 0x02, _PLAIN_CODES[_SHIFTED.index(ch)]
    return None


class HID:
    def __init__(self, dry_run=False):
        self.dry = dry_run
        self.layout = os.environ.get("DECK_LAYOUT", "uk").lower()
        self.lock = threading.Lock()

    def _write(self, path, data):
        if self.dry:
            print("[hid] %s <- %s" % (path, data.hex()))
            return
        try:
            with open(path, "wb", buffering=0) as f:
                f.write(data)
        except OSError as e:
            # Typically: laptop asleep / not enumerated yet
            print("[hid] write to %s failed: %s" % (path, e))

    def _kbd(self, mods, codes):
        codes = (list(codes) + [0] * 6)[:6]
        self._write(HID_KEYBOARD, bytes([mods, 0] + codes))

    def combo(self, spec):
        mods, codes = 0, []
        for part in spec.lower().split("+"):
            part = part.strip()
            if part in MOD:
                mods |= MOD[part]
            elif part in KEYS:
                codes.append(KEYS[part])
            else:
                raise ValueError("unknown key %r in %r" % (part, spec))
        with self.lock:
            self._kbd(mods, codes)
            time.sleep(0.02)
            self._kbd(0, [])

    def text(self, s, delay=0.004):
        with self.lock:
            for ch in s:
                k = char_to_key(ch, self.layout)
                if k is None:
                    print("[hid] cannot type %r (layout %s)" % (ch, self.layout))
                    continue
                self._kbd(k[0], [k[1]])
                time.sleep(delay)
                self._kbd(0, [])
                time.sleep(delay)

    def consumer(self, name):
        usage = CONSUMER[name]
        with self.lock:
            self._write(HID_CONSUMER, usage.to_bytes(2, "little"))
            time.sleep(0.02)
            self._write(HID_CONSUMER, b"\x00\x00")


# --------------------------------------------------------------------- hub
class Hub:
    def __init__(self, args):
        self.args = args
        self.cfg_path = args.config
        self.lock = threading.RLock()
        self.hid = HID(args.dry_run)
        self.kb = None  # keybow module when LEDs are available
        self.page = 0
        self.settings = {}
        self.buttons = {}   # (page,row,col) -> normalised dict (for LEDs)
        self.actions = {}   # (page,row,col) -> raw action
        self.pages = [0]
        self.published = {}
        self.client = self._make_client()
        self.hw_to_pos = {HW_INDEX_GRID[r][c]: (r, c)
                          for r in range(4) for c in range(3)}
        self.load_config()

    # ---- config
    def load_config(self):
        with open(self.cfg_path) as f:
            raw = json.load(f)
        screen_bg = to_rgb(raw.get("background_color"), (0, 0, 0))
        dflt = to_rgb(raw.get("default_color"), (255, 255, 255))
        rows, cols = int(raw.get("rows", 4)), int(raw.get("columns", 3))
        if rows > 4 or cols > 3:
            raise ValueError("Keybow has 4 rows x 3 columns; got %dx%d" % (rows, cols))

        buttons, actions = {}, {}
        for b in raw.get("buttons_defs", []):
            key = (int(b["page"]), int(b["row"]), int(b["column"]))
            if key in buttons:
                print("[cfg] duplicate button at %s (%s)" % (key, b.get("name")))
            if key[1] >= rows or key[2] >= cols:
                raise ValueError("button %s outside grid" % (b.get("name"),))
            base = to_rgb(b.get("color"), dflt)
            buttons[key] = {
                "name": b.get("name", "%d-%d-%d" % key),
                "page": key[0], "row": key[1], "column": key[2],
                "label": b.get("label", ""),
                "label_font": b.get("label_font"),
                "symbol": b.get("symbol"),
                "label_color": to_rgb(b.get("label_color"), base),
                "outline_color": to_rgb(b.get("outline_color"), base),
                "bg_color": to_rgb(b.get("bg_color"), screen_bg),
                "active": bool(b.get("action")),
            }
            if b.get("action"):
                actions[key] = b["action"]

        settings = {
            "rows": rows, "columns": cols,
            "margin_ratio": raw.get("margin_ratio", 0.1),
            "corner_radius": raw.get("corner_radius"),
            "default_font": raw.get("default_font"),
            "background_color": screen_bg,
            "default_color": dflt,
            "led_brightness": float(raw.get("led_brightness", 0.4)),
        }
        if "DECK_LAYOUT" not in os.environ:
            self.hid.layout = str(raw.get("keyboard_layout", "uk")).lower()
        with self.lock:
            self.settings, self.buttons, self.actions = settings, buttons, actions
            self.pages = sorted({k[0] for k in buttons}) or [0]
            if self.page not in self.pages:
                self.page = int(raw.get("start_page", self.pages[0]))
            self.published = {"settings": settings, "pages": self.pages,
                              "buttons": list(buttons.values())}
        print("[cfg] loaded %d buttons on pages %s" % (len(buttons), self.pages))

    # ---- mqtt
    def _make_client(self):
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="deck-hub")
        except AttributeError:  # paho-mqtt 1.x
            c = mqtt.Client(client_id="deck-hub")
        if MQTT_USER:
            c.username_pw_set(MQTT_USER, MQTT_PASS)
        c.will_set(T_STATUS, "offline", retain=True)
        c.reconnect_delay_set(1, 30)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        return c

    def _on_connect(self, client, userdata, flags, rc, *extra):
        print("[mqtt] connected (rc=%s)" % (rc,))
        client.subscribe([(T_PRESS, 0), (T_CMD, 0)])
        client.publish(T_STATUS, "online", retain=True)
        self.publish_state()

    def publish_state(self):
        with self.lock:
            cfg, page = json.dumps(self.published), self.page
        self.client.publish(T_CONFIG, cfg, retain=True)
        self.client.publish(T_PAGE, str(page), retain=True)

    def _on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode()
            if msg.topic == T_PRESS:
                d = json.loads(payload)
                self.press(int(d["page"]), int(d["row"]), int(d["column"]), "presto")
            elif msg.topic == "deck/cmd/page":
                self.goto(payload.strip())
            elif msg.topic == "deck/cmd/reload":
                self.reload()
        except Exception as e:  # never let a bad message kill the loop
            print("[mqtt] bad message on %s: %s" % (msg.topic, e))

    # ---- leds
    def setup_leds(self):
        try:
            import keybow
            keybow.setup(keybow.FULL)  # 12-key Keybow; check library README if this errors
            self.kb = keybow
        except Exception as e:
            print("[led] disabled: %s" % e)
            return

        @keybow.on()
        def _handler(index, state):
            if state and index in self.hw_to_pos:
                r, c = self.hw_to_pos[index]
                with self.lock:
                    page = self.page
                self.press(page, r, c, "keybow")

        self.refresh_leds()

    def _scaled(self, rgb, factor=None):
        f = self.settings["led_brightness"] if factor is None else factor
        return [int(v * f) for v in rgb]

    def refresh_leds(self):
        if not self.kb:
            return
        with self.lock:
            self.kb.clear()
            for r in range(4):
                for c in range(3):
                    b = self.buttons.get((self.page, r, c))
                    if b:
                        self.kb.set_led(HW_INDEX_GRID[r][c], *self._scaled(b["bg_color"]))
            self.kb.show()

    def flash(self, r, c):
        if not self.kb:
            return
        with self.lock:
            b = self.buttons.get((self.page, r, c))
            if not b:
                return
            idx = HW_INDEX_GRID[r][c]
            self.kb.set_led(idx, *self._scaled(b["bg_color"], 1.0))
            self.kb.show()

        def restore():
            with self.lock:
                self.kb.set_led(idx, *self._scaled(b["bg_color"]))
                self.kb.show()
        threading.Timer(0.15, restore).start()

    # ---- actions
    def press(self, page, row, col, src):
        with self.lock:
            if page != self.page:
                print("[press] ignoring press for page %d (current %d)" % (page, self.page))
                return
            key = (page, row, col)
            name = self.buttons.get(key, {}).get("name", "?")
            action = self.actions.get(key)
        print("[press] %s (%d,%d,%d) from %s" % (name, page, row, col, src))
        self.flash(row, col)
        self.client.publish(T_EVENT, json.dumps({"name": name, "src": src}))
        if action:
            threading.Thread(target=self.run, args=(action,), daemon=True).start()

    def run(self, action):
        for a in action if isinstance(action, list) else [action]:
            try:
                t = a["type"]
                if t == "hid":
                    self.hid.combo(a["keys"])
                elif t == "text":
                    self.hid.text(a["text"])
                elif t == "consumer":
                    self.hid.consumer(a["key"])
                elif t == "mqtt":
                    p = a.get("payload", "")
                    if isinstance(p, (dict, list)):
                        p = json.dumps(p)
                    self.client.publish(a["topic"], p, retain=bool(a.get("retain")))
                elif t == "page":
                    self.goto(a["to"])
                elif t == "delay":
                    time.sleep(a.get("ms", 100) / 1000)
                else:
                    print("[action] unknown type %r" % t)
            except Exception as e:
                print("[action] failed %r: %s" % (a, e))

    def goto(self, to):
        with self.lock:
            i = self.pages.index(self.page)
            if to == "next":
                new = self.pages[(i + 1) % len(self.pages)]
            elif to == "prev":
                new = self.pages[(i - 1) % len(self.pages)]
            else:
                new = int(to)
                if new not in self.pages:
                    print("[page] no such page %s" % to)
                    return
            self.page = new
        self.client.publish(T_PAGE, str(new), retain=True)
        self.refresh_leds()

    def reload(self):
        try:
            self.load_config()
            self.refresh_leds()
            self.publish_state()
        except Exception as e:
            print("[cfg] reload failed, keeping old config: %s" % e)

    # ---- run
    def run_forever(self):
        if not self.args.dry_run:
            self.setup_leds()
        self.client.connect_async(BROKER, PORT, 60)
        self.client.loop_forever()

    def shutdown(self, *_):
        try:
            self.client.publish(T_STATUS, "offline", retain=True)
            if self.kb:
                self.kb.clear()
                self.kb.show()
        finally:
            os._exit(0)


def identify():
    """Light keys dim; pressing one prints its index and lights it green."""
    import keybow
    keybow.setup(keybow.FULL)
    for i in range(12):
        keybow.set_led(i, 20, 20, 20)
    keybow.show()

    @keybow.on()
    def _h(index, state):
        print("key index %d %s" % (index, "down" if state else "up"))
        keybow.set_led(index, 0, 255 if state else 20, 0 if state else 20)
        keybow.show()
    print("Press keys; Ctrl+C to quit. Edit HW_INDEX_GRID to match.")
    signal.pause()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "buttons.json"))
    ap.add_argument("--dry-run", action="store_true", help="no Keybow/HID hardware; print instead")
    ap.add_argument("--identify", action="store_true", help="show key index numbering")
    args = ap.parse_args()
    if args.identify:
        identify()
        return
    hub = Hub(args)
    signal.signal(signal.SIGHUP, lambda *_: hub.reload())
    signal.signal(signal.SIGTERM, hub.shutdown)
    signal.signal(signal.SIGINT, hub.shutdown)
    hub.run_forever()


if __name__ == "__main__":
    main()
