"""Before a device run, when asked (SEED_PLAN_ROWS > 0): give the staging test account
a long meal plan, so the run checks sign-in with one (trove-app #463: about 40 planned
meals once kept the iPhone's main thread busy for over 30 s after sign-in).

Tops the account up to SEED_PLAN_ROWS meal_plan_items across this week and next, all
pointing at one of the account's own recipes so the phones can resolve them. Rows count
as fresh for an hour, then remove_test_imports.py clears them like any other.

Staging only. Prints counts, never values. Never fails the run.
"""
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request
import uuid

STAGING_HOST = 'kcmldknrfijrhacnqwjx.supabase.co'
MEALS = ('breakfast', 'lunch', 'dinner', 'snack')


def call(method, url, key, token=None, body=None, prefer=None):
    headers = {'apikey': key, 'Authorization': f'Bearer {token or key}', 'Content-Type': 'application/json'}
    if prefer:
        headers['Prefer'] = prefer
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=30) as r:
        text = r.read().decode()
        return json.loads(text) if text else None


def main():
    wanted = int(os.environ.get('SEED_PLAN_ROWS') or 0)
    if wanted <= 0:
        return 0
    base = os.environ.get('SUPABASE_URL', '').rstrip('/')
    key = os.environ.get('SUPABASE_ANON_KEY', '')
    if urllib.parse.urlsplit(base)[:2] != ('https', STAGING_HOST) or not key:
        print('long plan: not trove-staging; skipped')
        return 0
    session = call('POST', f'{base}/auth/v1/token?grant_type=password', key,
                   body={'email': os.environ['TROVE_TEST_EMAIL'], 'password': os.environ['TROVE_TEST_PASSWORD']})
    token = session['access_token']
    owner = urllib.parse.quote(session['user']['id'], safe='')
    recipes = call('GET', f'{base}/rest/v1/recipes?select=id&owner=eq.{owner}&order=created_at.desc&limit=1', key, token) or []
    if not recipes:
        print('long plan: the account has no recipe to plan; skipped')
        return 0
    rows = call('GET', f'{base}/rest/v1/meal_plan_items?select=id&owner=eq.{owner}', key, token) or []
    missing = wanted - len(rows)
    if missing > 0:
        today = datetime.date.today()
        new = [{
            'id': str(uuid.uuid4()).upper(),
            'recipe_id': recipes[0]['id'],
            'plan_date': (today + datetime.timedelta(days=i % 14)).isoformat(),
            'meal': MEALS[i % len(MEALS)],
        } for i in range(missing)]
        call('POST', f'{base}/rest/v1/meal_plan_items', key, token, body=new, prefer='return=minimal')
    print(f'long plan: the account has {max(wanted, len(rows))} meal plan row(s) ({max(missing, 0)} added)')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:  # never block the run on seeding
        print(f'long plan: skipped ({type(error).__name__})')
        sys.exit(0)
