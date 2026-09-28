"""Explain APK requirements and find installable versions in F-Droid's indexes."""
from urllib.parse import quote, urlencode

import frame_android
import frame_catalog
from pick import installable

ANDROID = dict(enumerate([
    '1.0', '1.1', '1.5', '1.6', '2.0', '2.0.1', '2.1', '2.2', '2.3', '2.3.3',
    '3.0', '3.1', '3.2', '4.0', '4.0.3', '4.1', '4.2', '4.3', '4.4', '4.4W',
    '5.0', '5.1', '6.0', '7.0', '7.1', '8.0', '8.1', '9', '10', '11', '12',
    '12L', '13', '14', '15', '16',
], 1))
REPOS = (('F-Droid', 'https://f-droid.org/repo/'),
         ('F-Droid archive', 'https://f-droid.org/archive/'))
NOTE = ('Pick a version whose minimum is Android 11 or lower and that has an '
        'arm64-v8a build (or no native code). Installable does not mean every feature works.')


def android_name(sdk):
    return 'Android ' + ANDROID[sdk] if sdk in ANDROID else f'Android API {sdk}'


def describe(info):
    sdk = info.get('min_sdk')
    minimum = f'{android_name(sdk)} (API {sdk})' if sdk else 'not specified'
    try:
        frame_android.check_installable(info)
        verdict = 'Lepton can install this APK. Features may still need services Lepton lacks.'
    except frame_android.FrameError as e:
        verdict = f'Lepton cannot install this APK: {e}'
    return (f"{info['package']} · {info.get('version') or '?'} "
            f"(code {info.get('version_code') if info.get('version_code') is not None else '?'})\n"
            f"Minimum: {minimum}\nABIs: {', '.join(info['abis']) or 'no native code'}\n{verdict}")


def search_links(package):
    q = quote(package, safe='')
    return [{'source': name, 'url': url} for name, url in (
        ('APKMirror', 'https://www.apkmirror.com/?' + urlencode({'post_type': 'app_release', 's': package})),
        ('APKPure', 'https://apkpure.com/search?q=' + q),
        ('Uptodown', 'https://en.uptodown.com/android/search/' + q),
        ('F-Droid', 'https://search.f-droid.org/?q=' + q),
        ('GitHub', 'https://github.com/search?type=repositories&q=' + q),
    )]


def alternatives(package, current_version_code=None):
    """All compatible builds, ordered by version code; keep per-ABI builds distinct."""
    versions, errors, seen = [], [], set()
    for source, repo in REPOS:
        try:
            index = frame_catalog.load_index(repo)
        except (OSError, ValueError) as e:
            errors.append(f'Could not check {source}: {e}')
            continue
        for v in index.get('packages', {}).get(package, {}).get('versions', {}).values():
            m, file = v['manifest'], v['file']
            code = m.get('versionCode', 0)
            if not installable(v):
                continue
            url = repo + file['name'].lstrip('/')
            key = (code, file.get('sha256') or url)
            if code == current_version_code or key in seen:
                continue
            seen.add(key)
            versions.append({'version': m.get('versionName', ''), 'version_code': code,
                             'min_sdk': m.get('usesSdk', {}).get('minSdkVersion', 1),
                             'abis': m.get('nativecode') or [], 'url': url,
                             'source': source, 'sha256': file.get('sha256')})
    versions.sort(key=lambda v: v['version_code'], reverse=True)
    return {'package': package, 'versions': versions, 'links': search_links(package),
            'note': NOTE, 'errors': errors}


def install(package, url):
    # Resolve the selection again: the client cannot supply a trusted hash or arbitrary URL.
    result = alternatives(package)
    version = next((v for v in result['versions'] if v['url'] == url), None)
    if not version:
        raise frame_android.FrameError('That version is no longer available; check the APK again')
    apk = frame_catalog.fetch_apk({'a': version['url'], 'h': version['sha256'], 'n': package})
    info = frame_android.apk_info(apk)
    if info['package'] != package or info.get('version_code') != version['version_code']:
        raise frame_android.FrameError('The downloaded APK does not match the selected version')
    return frame_android.install(apk, source=version['source'])
