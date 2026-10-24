"""Before a device run: remove the staging test account's earlier imports of the
flows' recipe link, and the plan rows that point at them, so each run imports
into the same library instead of adding another copy. Only imports older than
an hour go: the iPhone, Android and web runs can overlap, and none may delete
a copy another run is still using.

Staging only (checks the project ref). Prints counts, never values.
Reads SUPABASE_URL, SUPABASE_ANON_KEY, TROVE_TEST_EMAIL, TROVE_TEST_PASSWORD,
and RECIPE_URL (default: the flows' default link).
"""
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request

STAGING_REF = 'kcmldknrfijrhacnqwjx'
DEFAULT_LINK = 'https://www.bbcgoodfood.com/recipes/easy-pancakes'


def call(method, url, key, token=None, body=None):
    headers = {'apikey': key, 'Authorization': f'Bearer {token or key}', 'Content-Type': 'application/json'}
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=30) as r:
        text = r.read().decode()
        return json.loads(text) if text else None


def main():
    base = os.environ.get('SUPABASE_URL', '').rstrip('/')
    key = os.environ.get('SUPABASE_ANON_KEY', '')
    if STAGING_REF not in base or not key:
        print('cleanup: not trove-staging; skipped')
        return 0
    link = os.environ.get('RECIPE_URL') or DEFAULT_LINK
    session = call('POST', f'{base}/auth/v1/token?grant_type=password', key,
                   body={'email': os.environ['TROVE_TEST_EMAIL'], 'password': os.environ['TROVE_TEST_PASSWORD']})
    token = session['access_token']
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).isoformat(timespec='seconds')
    rows = call('GET', f'{base}/rest/v1/recipes?select=id&source_url=eq.{urllib.parse.quote(link, safe="")}'
                       f'&created_at=lt.{urllib.parse.quote(cutoff, safe="")}', key, token) or []
    ids = [r['id'] for r in rows]
    if ids:
        listed = urllib.parse.quote('(' + ','.join(f'"{i}"' for i in ids) + ')', safe='')
        call('DELETE', f'{base}/rest/v1/meal_plan_items?recipe_id=in.{listed}', key, token)
        call('DELETE', f'{base}/rest/v1/recipes?id=in.{listed}', key, token)
    print(f'cleanup: removed {len(ids)} earlier import(s) of the test link')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:  # never block the run on cleanup
        print(f'cleanup: skipped ({type(error).__name__})')
        sys.exit(0)
