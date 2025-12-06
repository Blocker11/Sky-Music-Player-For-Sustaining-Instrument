#!/usr/bin/env python3
"""
Sky Music Player Auto Hold
Full-featured script for playing JSON "song" files with automatic hold calculation.
"""

import os
import sys
import json
import time
import threading
from collections import deque

try:
    import keyboard
except Exception:
    print("Please install the 'keyboard' package: pip install keyboard")
    raise

# ---------------------- CONFIG ----------------------
MUSIC_SHEET_DIR = "./Music Sheets/"
DEFAULT_HOLD_MS = 600
MIN_AUTO_HOLD_MS = 750
SAFETY_MARGIN_MS = 20
ALLOW_OVERLAP_MS = 20
PLAY_START_DELAY = 0.1
SONGS_PER_PAGE = 9

KEY_MAPPING = {
    "1Key0": "Y","1Key1": "U","1Key2": "I","1Key3": "O","1Key4": "P",
    "1Key5": "H","1Key6": "J","1Key7": "K","1Key8": "L","1Key9": ";",
    "1Key10": "N","1Key11": "M","1Key12": ",","1Key13": ".","1Key14": "/",
    "Key0": "Y","Key1": "U","Key2": "I","Key3": "O","Key4": "P",
    "Key5": "H","Key6": "J","Key7": "K","Key8": "L","Key9": ";",
    "Key10": "N","Key11": "M","Key12": ",","Key13": ".","Key14": "/",
}

# ---------------------- GLOBAL STATE ----------------------
music_sheets = []
current_page = 0
page_songs = []
song_hotkeys = {}
selected_index = None
queued_song = None
last_song_index = None
playing = False
paused = False
stop_requested = False
ready_to_play = False
speed_multiplier = 1.0
_speed_up_flag = False
_speed_down_flag = False
play_thread = None
_play_lock = threading.Lock()

# ---------------------- UTIL ----------------------
def load_music_sheets():
    global music_sheets, page_songs, current_page, song_hotkeys
    if not os.path.exists(MUSIC_SHEET_DIR):
        os.makedirs(MUSIC_SHEET_DIR)
    files = [os.path.join(MUSIC_SHEET_DIR, f) for f in os.listdir(MUSIC_SHEET_DIR) if f.lower().endswith('.json')]
    files.sort()
    music_sheets = files
    current_page = 0
    _rebuild_page()

def _rebuild_page():
    global page_songs, song_hotkeys
    start = current_page * SONGS_PER_PAGE
    page_songs = music_sheets[start:start+SONGS_PER_PAGE]
    song_hotkeys = {str(i+1): start + i for i in range(len(page_songs))}

def clear_console():
    os.system('cls' if os.name == 'nt' else 'clear')

def display_ui():
    clear_console()
    print("Sky Music Player — Auto Hold")
    print("-"*60)
    print("Controls: Space=Play/Pause | R=Replay | S=Search | 1-9 Select | -/= Page | Backspace=Stop")
    print(f"Speed: {speed_multiplier*100:.0f}%  | Selected: {os.path.basename(music_sheets[selected_index]) if selected_index is not None else 'None'}")
    print("-"*60)
    for i, path in enumerate(page_songs, start=1):
        idx = song_hotkeys.get(str(i))
        marker = ' [SELECTED]' if idx == selected_index else ''
        print(f"[{i}] {os.path.basename(path)}{marker}")
    print("-"*60)

# ---------------------- NOTE PREPROCESSING ----------------------
def _normalize_song_data(raw):
    if isinstance(raw, dict) and 'songNotes' in raw:
        return raw['songNotes']
    if isinstance(raw, list):
        if len(raw) > 0 and isinstance(raw[0], dict) and 'songNotes' in raw[0]:
            return raw[0]['songNotes']
        return raw
    return []

def preprocess_notes(song_path):
    try:
        with open(song_path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except Exception as e:
        raise RuntimeError(f"Failed to load JSON: {e}")

    notes_list = _normalize_song_data(raw)
    notes = [{'time': int(n.get('time',0)), 'key': n.get('key'), 'hold': n.get('hold')} for n in notes_list]

    times = sorted(set(n['time'] for n in notes))
    notes_by_time = {t: [] for t in times}
    for n in notes:
        notes_by_time[n['time']].append(n)

    sorted_times = sorted(notes_by_time.keys())
    for idx, t in enumerate(sorted_times):
        group = notes_by_time[t]
        next_t = sorted_times[idx+1] if idx+1 < len(sorted_times) else None
        if next_t is None:
            for note in group:
                note_hold = note.get('hold') if note.get('hold') is not None else DEFAULT_HOLD_MS
                note['hold'] = max(int(note_hold), MIN_AUTO_HOLD_MS)
        else:
            gap = next_t - t
            base = max(int(gap * 0.9), MIN_AUTO_HOLD_MS)
            use_hold = max(base, MIN_AUTO_HOLD_MS) if len(group)>=3 else base
            max_allowed_hold = gap + ALLOW_OVERLAP_MS
            if use_hold > max_allowed_hold:
                use_hold = max_allowed_hold
            for note in group:
                explicit = note.get('hold')
                note['hold'] = max(int(explicit), use_hold) if explicit is not None else int(use_hold)

    return notes_by_time, sorted_times

# ---------------------- PLAYBACK CORE ----------------------
def play_song_core(notes_by_time, time_keys_sorted):
    global playing, paused, stop_requested, speed_multiplier
    events = []
    for t in time_keys_sorted:
        for note in notes_by_time[t]:
            keyname = note.get('key')
            mapped = KEY_MAPPING.get(keyname)
            if not mapped: continue
            hold = int(note.get('hold', DEFAULT_HOLD_MS))
            events.append((t, 'press', mapped))
            events.append((t + hold, 'release', mapped))
    events.sort(key=lambda e: (e[0], 0 if e[1]=='press' else 1))
    release_queue = deque()
    pressed_keys = set()
    perf_start = time.perf_counter() + PLAY_START_DELAY
    while time.perf_counter() < perf_start:
        if stop_requested: return
        time.sleep(0.005)

    ev_idx = 0
    total_events = len(events)
    last_realtime = time.perf_counter()
    virtual_ms = 0.0

    def release_due(now_perf):
        while release_queue and release_queue[0][0] <= now_perf:
            _, k = release_queue.popleft()
            try: keyboard.release(k)
            except Exception: pass
            pressed_keys.discard(k)

    while ev_idx < total_events and not stop_requested:
        if paused:
            for k in list(pressed_keys):
                try: keyboard.release(k)
                except Exception: pass
            pressed_keys.clear()
            remaining_releases = []
            now_perf = time.perf_counter()
            while release_queue:
                rt, k = release_queue.popleft()
                remaining_releases.append((max(0.0, rt - now_perf), k))
            while paused and not stop_requested: time.sleep(0.02)
            if stop_requested: break
            now_perf = time.perf_counter()
            for rem, k in remaining_releases: release_queue.append((now_perf + rem, k))
            release_queue = deque(sorted(release_queue, key=lambda x: x[0]))
            last_realtime = time.perf_counter()
            continue

        now = time.perf_counter()
        elapsed = now - last_realtime
        virtual_ms += elapsed * 1000.0 * speed_multiplier
        last_realtime = now

        while ev_idx < total_events and events[ev_idx][0] <= virtual_ms + 1e-6:
            ev_time_ms, action, key = events[ev_idx]
            if action == 'press':
                try: keyboard.press(key)
                except Exception: pass
                pressed_keys.add(key)
                for j in range(ev_idx+1, total_events):
                    if events[j][1]=='release' and events[j][2]==key:
                        release_event_ms = events[j][0]; break
                else: release_event_ms = ev_time_ms + DEFAULT_HOLD_MS
                time_until_release_sec = max(0.0, (release_event_ms - virtual_ms) / (1000.0 * speed_multiplier))
                release_perf = time.perf_counter() + time_until_release_sec
                release_queue.append((release_perf, key))
                if len(release_queue) > 1:
                    i = len(release_queue)-1
                    while i>0 and release_queue[i][0]<release_queue[i-1][0]:
                        release_queue[i], release_queue[i-1] = release_queue[i-1], release_queue[i]
                        i -= 1
            ev_idx += 1

        release_due(time.perf_counter())
        time.sleep(0.0009)

    while release_queue:
        now = time.perf_counter()
        if release_queue[0][0] <= now:
            _, k = release_queue.popleft()
            try: keyboard.release(k)
            except Exception: pass
        else: time.sleep(0.001)
    for k in list(pressed_keys):
        try: keyboard.release(k)
        except Exception: pass
    pressed_keys.clear()

# ---------------------- HIGH LEVEL CONTROLS ----------------------
def select_song_by_hotkey(hk):
    global selected_index, queued_song, ready_to_play
    if hk not in song_hotkeys: return
    idx = song_hotkeys[hk]
    if idx<0 or idx>=len(music_sheets): return
    selected_index = idx
    queued_song = music_sheets[selected_index]
    ready_to_play = True
    print(f"Selected: {os.path.basename(queued_song)} — press Space to start")

def start_selected_song():
    global play_thread, playing, stop_requested, last_song_index, ready_to_play
    if not ready_to_play or queued_song is None:
        print("No song queued.")
        return
    stop_playback()
    try: notes_by_time, times = preprocess_notes(queued_song)
    except Exception as e:
        print("Failed to prepare song:", e)
        return
    stop_requested = False
    playing = True
    ready_to_play = False
    last_song_index = selected_index
    def worker():
        try: play_song_core(notes_by_time, times)
        except Exception as e: print("Playback error:", e)
        finally: global playing; playing=False
    play_thread = threading.Thread(target=worker, daemon=True)
    play_thread.start()
    print(f"▶ Started: {os.path.basename(queued_song)}")

def stop_playback():
    global stop_requested, playing, ready_to_play
    stop_requested = True
    playing = False
    ready_to_play = False
    for v in KEY_MAPPING.values():
        try: keyboard.release(v)
        except Exception: pass

def replay_last():
    global last_song_index, selected_index, queued_song, ready_to_play
    if last_song_index is None: print("No last song to replay."); return
    selected_index = last_song_index
    queued_song = music_sheets[selected_index]
    ready_to_play = True
    print(f"Requeued last song: {os.path.basename(queued_song)} — press Space to start")

def live_search():
    global music_sheets, current_page
    q = input("Search (empty to cancel): ").strip()
    if q=="": _rebuild_page(); display_ui(); return
    results = [p for p in music_sheets if q.lower() in os.path.basename(p).lower()]
    if results:
        music_sheets[:] = results + [p for p in music_sheets if p not in results]
        current_page = 0
        _rebuild_page()
        display_ui()
    else: print("No matches")

# ---------------------- HOTKEY REGISTRATION ----------------------
def register_hotkeys():
    for i in range(1, SONGS_PER_PAGE+1):
        hk=str(i)
        keyboard.add_hotkey(hk, lambda hk=hk: select_song_by_hotkey(hk))
    keyboard.add_hotkey('space', on_space_key)
    keyboard.add_hotkey('backspace', lambda: (stop_playback(), print('Stopped')))
    keyboard.add_hotkey('-', page_prev)
    keyboard.add_hotkey('=', page_next)
    keyboard.add_hotkey('s', live_search)
    keyboard.add_hotkey('r', replay_last)
    keyboard.on_press_key('up', lambda e: set_speed_flag(True, False))
    keyboard.on_release_key('up', lambda e: set_speed_flag(False, False))
    keyboard.on_press_key('down', lambda e: set_speed_flag(False, True))
    keyboard.on_release_key('down', lambda e: set_speed_flag(False, False))

def set_speed_flag(up, down):
    global _speed_up_flag, _speed_down_flag
    _speed_up_flag = up
    _speed_down_flag = down

def page_next():
    global current_page
    max_page = max(0,(len(music_sheets)-1)//SONGS_PER_PAGE)
    if current_page<max_page: current_page+=1; _rebuild_page(); display_ui()

def page_prev():
    global current_page
    if current_page>0: current_page-=1; _rebuild_page(); display_ui()

def on_space_key():
    global playing, paused, ready_to_play
    if ready_to_play and not playing: start_selected_song(); return
    if playing:
        paused = not paused
        print('Paused' if paused else 'Resumed')
        if paused:
            for k in list(KEY_MAPPING.values()):
                try: keyboard.release(k)
                except Exception: pass
        return
    print('No song queued. Select with 1-9')

# ---------------------- SPEED ADJUSTER THREAD ----------------------
def _speed_adjust_loop():
    global speed_multiplier, _speed_up_flag, _speed_down_flag
    while True:
        changed=False
        if _speed_up_flag: speed_multiplier*=1.01; changed=True
        if _speed_down_flag: speed_multiplier/=1.01; changed=True
        if changed:
            speed_multiplier = max(0.1,min(5.0,speed_multiplier))
            print(f"Speed: {speed_multiplier*100:.0f}%")
        time.sleep(0.06)

# ---------------------- MAIN ----------------------
def main():
    load_music_sheets()
    if not music_sheets:
        print(f"No JSON files found in {MUSIC_SHEET_DIR}.")
        return
    _rebuild_page()
    display_ui()
    register_hotkeys()
    threading.Thread(target=_speed_adjust_loop, daemon=True).start()
    print('Hotkeys registered. Press F5 to exit.')
    try: keyboard.wait('F5')
    except KeyboardInterrupt: pass
    finally: stop_playback(); print('Exiting...')

if __name__ == '__main__':
    main()
