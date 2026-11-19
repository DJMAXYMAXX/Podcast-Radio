# Podcast Radio

A radio where every station is always on air. Each of the 10 channels plays a
random, endless stream of podcast episodes pulled from every genre, on a shared
clock. Turn the dial and you land wherever that station is right now, usually
halfway through something. Between stations you get static. A Restart button
takes the current episode back to the beginning, and "Back to live" rejoins the
broadcast.

## Try it on your computer

You need Python 3 (already on a Mac).

```
cd podcast-radio
python3 server.py
```

Then open http://localhost:8088 and press **Turn on**.

The first start takes a minute or two: it looks up shows for about 80 topics
(history, cycling, true crime, jazz, kids stories, woodworking, and so on) in
Apple's public podcast directory, then reads their feeds. The result is cached
in `library.json` and refreshed once a day.

Controls: drag the knob, scroll, click or drag on the dial, or use the arrow
keys (Shift + arrow jumps straight to the next station). `R` restarts the
episode.

## How "always on" works

The station went on air on 1 January 2026. Each channel has its own seeded
shuffle of the episode pool, and the server walks it forward from that date
using each episode's length. So at any moment it knows exactly what every
channel is playing and how far in, even channels nobody is listening to. When
the real audio turns out shorter or longer than the feed claimed, the radio
corrects the schedule as it goes. Episodes that won't play are skipped.

## Changing things

Everything lives in `config.json`:

- `channels`: how many stations (spread evenly from 88.1 to 107.9 FM)
- `discovery_terms`: the topics used to find shows. Add anything.
- `curated_feeds`: RSS feeds that are always included
- `shows_per_term`, `episodes_per_show`: how big the pool gets
- `discover`: set to `false` to use only your curated feeds

Delete `library.json` to force a fresh pull.

## Putting it in a real radio

Plan: a Raspberry Pi runs `server.py` plus `radio_pi.py`, which plays audio
with no screen and reads a real knob and button.

Parts (roughly $80 to $120 plus the radio):

- Raspberry Pi 4 or Pi Zero 2 W, power supply, microSD card
- Rotary encoder (KY-040 style) as the tuning knob, ideally on the radio's
  original tuning shaft
- Momentary push button for Restart (could reuse an old band switch)
- Audio: an I2S amp board such as the Adafruit MAX98357A or a HiFiBerry
  MiniAmp, driving the radio's original speaker
- Optional: a potentiometer or second encoder for volume, an LED behind the
  dial glass

Wiring (BCM pins, editable at the top of `radio_pi.py`): encoder A to GPIO17,
B to GPIO27, button to GPIO22, all against ground.

On the Pi:

```
sudo apt install mpv python3-gpiozero
python3 server.py &
python3 radio_pi.py
```

`radio_pi.py` also runs on a laptop for testing (keys `a`/`d` tune, `A`/`D`
jump stations, `r` restarts). Set both to start at boot with systemd once it
works. The Pi needs Wi-Fi; podcasts stream, nothing is stored.

`radio_pi.py` hasn't been tested on Pi hardware yet, so expect some tuning.

## Ideas for later

- A physical needle driven by a small servo, synced to the frequency
- Station "IDs" or a chime between episodes
- One channel that only plays shows you add by hand
- Skip ads by starting a few minutes past dynamic ad slots (hard to do reliably)
