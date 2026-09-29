"""Small, bounded CPU Gaussian-splat preview renderer (Frame Control-owned).

Reads the common 32-byte .splat record: position/scale float32 triplets,
RGBA bytes, then normalized quaternion bytes (wxyz). Two perspective cameras,
projected 3D covariance, back-to-front alpha compositing. This is a stationary
stereo preview, not a six-degree-of-freedom scene or a large-scene renderer.
"""
import math
from pathlib import Path
import struct

MAX_SPLATS = 20000
RECORD = struct.Struct('<6f8B')


def read(path):
    size = Path(path).stat().st_size
    if not size or size % RECORD.size or size > MAX_SPLATS * RECORD.size:
        raise ValueError('Use a 32-byte .splat file with 1–20,000 Gaussians; PLY/SPZ and larger scenes are not supported yet')
    values = []
    with open(path, 'rb') as stream:
        for row in RECORD.iter_unpack(stream.read(MAX_SPLATS * RECORD.size + 1)):
            xyz, scales = row[:3], row[3:6]
            if not all(math.isfinite(v) and abs(v) <= 1e6 for v in row[:6]) or min(scales) <= 0:
                raise ValueError('Invalid splat position or scale')
            q = [(v - 128) / 128 for v in row[10:14]]
            length = math.sqrt(sum(v*v for v in q))
            if length < .01:
                raise ValueError('Invalid splat quaternion')
            w, x, y, z = [v / length for v in q]
            rotation = ((1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)),
                        (2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)),
                        (2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)))
            cov = [[sum(rotation[i][k]*rotation[j][k]*scales[k]**2 for k in range(3))
                    for j in range(3)] for i in range(3)]
            values.append((xyz, cov, row[6:10]))
    return values


def render(path, width=320, height=240):
    values = read(path)
    lo = [min(p[0][i] for p in values) for i in range(3)]
    hi = [max(p[0][i] for p in values) for i in range(3)]
    center = [(a+b)/2 for a, b in zip(lo, hi)]
    radius = max(max(b-a for a, b in zip(lo, hi))/2, .01)
    # Normalize captures to a two-metre box. Source units are not assumed metres.
    normalized = [([(xyz[i]-center[i])/radius for i in range(3)],
                   [[v/radius**2 for v in row] for row in cov], color)
                  for xyz, cov, color in values]
    normalized.sort(key=lambda p: p[0][2])  # camera is at z=3; farthest first
    focal = width * .8
    eyes = []
    for eye in (-.032, .032):
        pixels = bytearray(b'\x00\x00\x00\xff' * (width*height))
        for (x, y, z), cov, color in normalized:
            x -= eye
            depth = 3-z
            px, py = width/2+focal*x/depth, height/2-focal*y/depth
            jac = ((focal/depth, 0, focal*x/depth**2),
                   (0, -focal/depth, -focal*y/depth**2))
            screen = [[sum(jac[i][a]*cov[a][b]*jac[j][b] for a in range(3) for b in range(3))
                       for j in range(2)] for i in range(2)]
            a, b, c = screen[0][0]+.3, screen[0][1], screen[1][1]+.3
            det = a*c-b*b
            if det <= 0 or not math.isfinite(det):
                raise ValueError('Splat covariance is not renderable')
            # A footprint cap bounds work on malformed or oversized Gaussians.
            rx, ry = min(32, math.ceil(3*math.sqrt(a))), min(32, math.ceil(3*math.sqrt(c)))
            for sy in range(max(0, int(py)-ry), min(height, int(py)+ry+1)):
                dy = sy+.5-py
                for sx in range(max(0, int(px)-rx), min(width, int(px)+rx+1)):
                    dx = sx+.5-px
                    power = (c*dx*dx-2*b*dx*dy+a*dy*dy)/det
                    if power > 9:
                        continue
                    alpha = color[3]/255 * math.exp(-.5*power)
                    offset = (sy*width+sx)*4
                    for k in range(3):
                        pixels[offset+k] = round(color[k]*alpha+pixels[offset+k]*(1-alpha))
        eyes.append(pixels)
    stride = width*4
    return b''.join(eyes[0][y*stride:(y+1)*stride]+eyes[1][y*stride:(y+1)*stride]
                    for y in range(height)), width*2, height
