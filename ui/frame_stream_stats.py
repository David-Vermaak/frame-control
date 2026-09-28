"""The desktop stream's timing records, in the Mac viewer/bench schema.

Times are host monotonic microseconds. Zero means unknown, never a fabricated
capture/display timestamp. Storage is bounded to the same 4096/512 records as
Stats.swift. The existing macview-bench.py grades these records unchanged.
"""
from collections import OrderedDict
import threading


def distribution(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return {}
    return {key: round(values[min(len(values)-1, int((len(values)-1)*p + .5))], 1)
            for key, p in (('p50', .5), ('p95', .95))}


class Stats:
    def __init__(self, now):
        self.now, self.lock = now, threading.RLock()
        self.frames, self.inputs = OrderedDict(), OrderedDict()
        self.captured = self.skipped = self.dropped = 0
        self.rtt, self.decoder, self.synced = 0, '', False
        self.sequence = 0

    def add(self, **values):
        with self.lock:
            self.sequence = (self.sequence + 1) & 0xffffffff
            record = dict.fromkeys(('s', 'k', 'b', 'w', 'h', 'cap', 'arr', 'e0', 'e1', 'snd',
                                    'wire', 'rx', 'dec', 'drw', 'vs', 'echo', 'tier', 'br'), 0)
            record.update(values, s=self.sequence)
            self.frames[self.sequence] = record
            while len(self.frames) > 4096:
                self.frames.popitem(last=False)
            for event in self.inputs.values():
                if not event['frame'] and event['inj'] <= record['cap']:
                    event.update(frame=self.sequence, cap=record['cap'])
                    record['echo'] = event['id']
            return record

    def report(self, message):
        with self.lock:
            if message['t'] == 'rx':
                frame = self.frames.get(message.get('s'))
                if frame and isinstance(message.get('r'), (int, float)):
                    frame['rx'] = int(message['r'])
            elif message['t'] == 'fd':
                for item in message.get('f', [])[:4096]:
                    if isinstance(item, list) and len(item) >= 4:
                        frame = self.frames.get(item[0])
                        if frame:
                            for name, val in zip(('dec', 'drw', 'vs'), item[1:4]):
                                if isinstance(val, (int, float)):
                                    frame[name] = int(val)
                self.dropped += max(0, int(message.get('drop', 0)))
            elif message['t'] == 'clock':
                self.rtt = float(message.get('rtt', 0))
                self.decoder = str(message.get('dec', ''))[:1024]
                self.synced = True

    def input(self, m):
        if not isinstance(m.get('i'), int) or not m['i']:
            return
        with self.lock:
            self.inputs[m['i']] = dict(id=m['i'], kind=m['t'], tv=m.get('tv') or 0,
                                       inj=self.now(), frame=0, cap=0)
            while len(self.inputs) > 512:
                self.inputs.popitem(last=False)

    def summary(self):
        with self.lock:
            now = self.now()
            frames = [f for f in self.frames.values() if f['cap'] >= now-2000000]
            out = dict(captured=self.captured, skipped=self.skipped, dropped=self.dropped,
                       rtt=self.rtt, decoder=self.decoder, synced=self.synced,
                       fps=sum(f['vs'] > 0 for f in frames)/2, sentFps=len(frames)/2,
                       mbps=round(sum(f['b'] for f in frames)*4/1e6, 2))
            for key, start, end in (('capture', 'cap', 'arr'), ('queue', 'arr', 'e0'),
                                    ('encode', 'e0', 'e1'), ('network', 'e1', 'rx'),
                                    ('decode', 'rx', 'dec'), ('draw', 'dec', 'drw'), ('total', 'cap', 'drw')):
                out[key] = distribution([(f[end]-f[start])/1000 for f in frames if f[start] and f[end]])
            return out

    def snapshot(self, since=0, settle=1500000):
        with self.lock:
            return dict(frames=[dict(f) for f in self.frames.values() if f['s'] > since and f['cap'] <= self.now()-settle],
                        inputs=[dict(i) for i in self.inputs.values()], captured=self.captured, summary=self.summary())
