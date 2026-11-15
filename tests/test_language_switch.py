import pytest
from app import app

def test_language_toggle_and_assets():
    client = app.test_client()

    # 1. Check toggle from default (ar) to en
    with client.session_transaction() as sess:
        sess['lang'] = 'ar'

    resp = client.get('/toggle-lang', follow_redirects=False)
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert sess.get('lang') == 'en'

    # 2. Check toggle back to ar
    resp = client.get('/toggle-lang', follow_redirects=False)
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert sess.get('lang') == 'ar'

    # 3. Check explicit lang query param
    resp = client.get('/toggle-lang?lang=en', follow_redirects=False)
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert sess.get('lang') == 'en'

    # 4. Check translator JS asset is served
    resp = client.get('/static/js/app-translator.js')
    assert resp.status_code == 200
    assert b'DICTIONARY' in resp.data

    # 5. Check login page renders with dir="ltr" when lang is en
    resp = client.get('/login')
    assert resp.status_code == 200
    assert b'dir="ltr"' in resp.data
    assert b'app-translator.js' in resp.data
