"""Human-input gesture state machine, independent of the agent action planner."""
import math


class GestureInput:
    def __init__(self):
        self.point = None
        self.pinched_at = None
        self.dragging = False
        self.scroll_y = None

    def reset(self):
        events = [{'kind': 'up'}] if self.dragging else []
        self.point = self.pinched_at = self.scroll_y = None
        self.dragging = False
        return events

    def update(self, landmarks, gesture, now):
        if not landmarks or len(landmarks) < 21 or gesture == 'Open_Palm':
            return self.reset()
        if not all(math.isfinite(value) for point in landmarks for value in point[:2]):
            return self.reset()
        events = []
        scale = max(math.dist(landmarks[5][:2], landmarks[17][:2]), .015)
        ratio = math.dist(landmarks[4][:2], landmarks[8][:2]) / scale
        pinch = ratio < (.38 if self.pinched_at is not None else .25)
        if gesture == 'Victory' and not pinch:
            if self.dragging: events.append({'kind': 'up'})
            self.dragging = False; self.pinched_at = None
            y = landmarks[9][1]
            if self.scroll_y is not None:
                delta = max(-12, min(12, round((self.scroll_y - y) * 90)))
                if delta: events.append({'kind': 'scroll', 'delta': delta})
            self.scroll_y = y
            return events
        self.scroll_y = None
        if gesture == 'Pointing_Up' or pinch or self.pinched_at is not None:
            raw = [max(0, min(1, (landmarks[8][axis] - .15) / .7)) for axis in (0, 1)]
            self.point = raw if self.point is None else [old * .65 + new * .35 for old, new in zip(self.point, raw)]
            events.append({'kind': 'move', 'x': self.point[0], 'y': self.point[1]})
        if pinch:
            if self.pinched_at is None: self.pinched_at = now
            elif now - self.pinched_at >= .55 and not self.dragging:
                self.dragging = True; events.append({'kind': 'down'})
        elif self.pinched_at is not None:
            if self.dragging: events.append({'kind': 'up'})
            elif now - self.pinched_at < .55: events.append({'kind': 'click'})
            self.pinched_at = None; self.dragging = False
        return events
