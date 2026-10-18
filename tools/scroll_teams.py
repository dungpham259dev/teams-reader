"""Scroll up inside the Teams message area via real scroll-wheel events
(Quartz), which act on whatever is under the cursor (no focus needed).

Usage: python3 scroll_teams.py <x> <y> <batches>
"""
import sys
import time

import Quartz

x, y, batches = float(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3])

# Park the cursor over the message list.
move = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (x, y), 0)
Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
time.sleep(0.3)

for _ in range(batches):
    # positive wheel delta = scroll up (toward older messages)
    ev = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, 8)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
    time.sleep(0.25)
print(f"scrolled {batches} batches at ({int(x)},{int(y)})")
