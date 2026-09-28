"""Optional software, separate from Frame Control's own controls and HUD.

Public reports are attributed leads, never local verification. Compatibility
reports reuse the existing database with steam:<appid> package keys.
"""
REPORT = 'https://www.roadtovr.com/valve-steam-frame-review/'
UTILITIES = (
    (1009850, 'OVR Advanced Settings', False, 'No verified Frame result. Steam edition is paid; the developer also publishes free source/releases.', 'https://github.com/OpenVR-Advanced-Settings/OpenVR-AdvancedSettings'),
    (1173510, 'XSOverlay', False, 'Supplied research reports Proton support, but the linked review did not corroborate it on recheck. Untested.', REPORT),
    (1068820, 'OVR Toolkit', False, 'Supplied research reports Proton support, but the linked review did not corroborate it on recheck. Untested.', REPORT),
    (908520, 'fpsVR', False, 'No Frame-specific result established in the supplied public research. Untested here.', 'https://store.steampowered.com/app/908520/'),
    (1494460, 'Desktop+', True, 'Free, developer-published software. No verified Frame result.', 'https://github.com/elvissteinjr/DesktopPlus'),
)


def catalogue(ownership, reports):
    owned = {r['id']: r for r in ownership}
    out = []
    for appid, name, free, note, source in UTILITIES:
        local = owned.get(appid, {})
        matches = [r for r in reports if r.get('package') == f'steam:{appid}' and r.get('rating') in ('works', 'issues', 'broken')]
        latest = max(matches, key=lambda r: r.get('date') or '') if matches else None
        out.append({'id': appid, 'name': name, 'free': free, 'owned': local.get('owned'),
                    'installed': local.get('installed', False), 'frame': local.get('frame', 0),
                    'status': latest['rating'] if latest else 'untested',
                    'report': {k: latest.get(k) for k in ('notes', 'date', 'steamos', 'source')} if latest else None,
                    'note': note, 'source': source,
                    'canInstall': bool(free or local.get('owned'))})
    return {'utilities': out}
