#!/usr/bin/env python3
"""Send local media to Frame Control's own OpenVR player."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import frame_media
import server


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('files', nargs='*', type=Path)
    ap.add_argument('--launch', action='store_true', help='play the one file being sent')
    ap.add_argument('--layout', choices=frame_media.LAYOUTS, default='auto')
    ap.add_argument('--theatre', action='store_true', help='bigger screen and dark surround')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--stop', action='store_true')
    args = ap.parse_args()
    if args.launch and len(args.files) != 1:
        ap.error('--launch needs exactly one file')
    if not args.files and not (args.list or args.stop):
        ap.error('choose files, --list or --stop')
    for path in args.files:
        if not path.is_file():
            ap.error('not a file: %s' % path)
        frame_media.plan(path.name, 'mono')
    if args.stop:
        print(json.dumps(server.media({'action': 'stop'})))
    for path in args.files:
        result = server.push_media(path.resolve())
        print(json.dumps(result))
        if args.launch:
            print(json.dumps(server.media({'action': 'play', 'id': result['id'],
                                           'layout': args.layout, 'theatre': args.theatre})))
    if args.list:
        print(json.dumps(server.media({'action': 'list'})))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, server.Failure) as e:
        sys.exit(str(e))
