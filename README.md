# Presto + Keybow deck

Keybow (Pi Zero W) is the hub: it owns `button_defs.json`, lights its keys,
types into the laptop over USB HID, and talks MQTT. The Presto is a touchscreen
front end that renders from the config and reports touches.

Based on the schema of goodeb/Presto-Stream-Deck (page/row/column, label,
color, outline_color, label_color, symbol, label_font, margin_ratio,
default_font, default_color, background_color, corner_radius, `buttons_defs`).

## Schema changes vs. the original

| Field | Status | Meaning |
|---|---|---|
| `bg_color` | **new** | Button fill on the Presto **and** the Keybow LED colour |
| `label_color` | kept | Text colour (falls back to `color`) |
| `outline_color` | kept | Outline colour (falls back to `color`) |
| `color` | kept | Shorthand for text + outline |
| `action` | **new, replaces `fn_name`/`arg`** | Object or list of objects, see below |
| `rows`, `columns` | **new** | Grid size (max 4 x 3 for the Keybow) |
| `led_brightness` | **new** | 0-1, idle LED level (pressed key flashes at 1.0) |
| `start_page` | **new** | Page shown at boot |

Colours: a name (`red`, `aqua`...), `"#rrggbb"`, or `[r,g,b]`. The hub resolves
all of them to `[r,g,b]` before publishing, so the Presto never parses names.
Missing buttons are blank (LED off). A button with no `action` is a
label/spacer.

### Actions

```json
{"type":"hid","keys":"ctrl+shift+m"}          // combo; f1-f24, arrows, etc.
{"type":"text","text":"hello\n"}              // typed text (US layout)
{"type":"consumer","key":"vol_up"}            // play_pause next prev stop mute vol_up vol_down
{"type":"mqtt","topic":"a/b","payload":"on","retain":false}   // payload may be an object
{"type":"page","to":"next"}                   // next | prev | page number
{"type":"delay","ms":200}
```
A list runs in order, e.g. `[{"type":"hid","keys":"ctrl+l"},{"type":"text","text":"example.com\n"}]`.
Use `both` behaviour by listing an `hid` and an `mqtt` action together.

## MQTT topics (broker 192.168.1.15)

| Topic | Direction | Notes |
|---|---|---|
| `deck/config` | hub -> Presto | **retained** normalised config (no actions) |
| `deck/page` | hub -> Presto | **retained** current page number |
| `deck/press` | Presto -> hub | `{"page":0,"row":1,"column":2}` |
| `deck/event` | hub -> any | `{"name":"Mute","src":"keybow"}` for feedback |
| `deck/hub/status` | hub | retained `online`/`offline` (last will) |
| `deck/cmd/page` | any -> hub | `next`, `prev` or a number |
| `deck/cmd/reload` | any -> hub | re-read `button_defs.json` |

Presto firmware needs to: subscribe to `deck/config` and `deck/page`, draw the
current page (fill with `bg_color`, outline, label), and on touch publish
`deck/press`. Pages change only via the hub, so both devices stay in sync.

## Install on the Pi Zero W

1. Raspberry Pi OS Lite. Add `dtoverlay=dwc2` to `/boot/firmware/config.txt`
   (older images: `/boot/config.txt`) and `modules-load=dwc2` after
   `rootwait` in `cmdline.txt`. Reboot.
2. `sudo apt install python3-pip python3-paho-mqtt` then `sudo pip3 install keybow --break-system-packages`
3. Copy this folder to `/home/pi/deck`, `chmod +x setup_hid_gadget.sh`.
4. Plug the Zero's **data** USB port into the laptop.
5. Check key numbering: `sudo python3 deck_hub.py --identify`, press keys, edit
   `HW_INDEX_GRID` if needed.
6. Try it: `sudo python3 deck_hub.py` (or `--dry-run` off-device to see actions printed).
7. Service: `sudo cp deck-hub.service /etc/systemd/system/ && sudo systemctl enable --now deck-hub`.
   After editing the JSON: `sudo systemctl reload deck-hub`.

## Things to verify

* **Key numbering and `keybow.setup(keybow.HAT)`** are from the library docs
  and snippets, not tested on your hardware. `--identify` exists for this.
* `text` actions follow the `keyboard_layout` setting in `button_defs.json`
  (`"uk"` default, or `"us"`; the `DECK_LAYOUT` env var overrides it). It must
  match the laptop's keyboard layout. UK mode covers `" @ # ~ \ | £ ¬ €`.
  Key combos with letters are layout-independent.
* Pi Zero USB power: if it brown-outs on laptop boot, try a different port or a
  powered hub.
* Pimoroni's stock Keybow OS (Lua) must not be running; this replaces it.
