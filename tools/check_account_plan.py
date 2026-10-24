"""After a device run: report how many meal plan rows the run left on the
staging test account's account (meal_plan_items) for the flows' recipe link,
added in the last hour. Phones that sync the plan to the account (trove-app#359,
#360) write one when the add-to-meal-plan flow passes; older builds write none.

Staging only. Prints a count, never values. Never fails the run.
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


def main(platform):
    base = os.environ.get('SUPABASE_URL', '').rstrip('/')
    key = os.environ.get('SUPABASE_ANON_KEY', '')
    if STAGING_REF not in base or not key:
        return
    link = os.environ.get('RECIPE_URL') or DEFAULT_LINK
    session = call('POST', f'{base}/auth/v1/token?grant_type=password', key,
                   body={'email': os.environ['TROVE_TEST_EMAIL'], 'password': os.environ['TROVE_TEST_PASSWORD']})
    token = session['access_token']
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).isoformat(timespec='seconds')
    recipes = call('GET', f'{base}/rest/v1/recipes?select=id&source_url=eq.{urllib.parse.quote(link, safe="")}', key, token) or []
    ids = [r['id'] for r in recipes]
    rows = []
    if ids:
        listed = urllib.parse.quote('(' + ','.join(f'"{i}"' for i in ids) + ')', safe='')
        rows = call('GET', f'{base}/rest/v1/meal_plan_items?select=id&recipe_id=in.{listed}'
                           f'&created_at=gte.{urllib.parse.quote(since, safe="")}', key, token) or []
    print(f'{platform}: account has {len(rows)} meal plan row(s) for the test recipe from the last hour')


if __name__ == '__main__':
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else 'device')
    except Exception as error:
        print(f'account check skipped ({type(error).__name__})')
