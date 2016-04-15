# -*- coding: utf-8 -*-

from flask import url_for
from flask_login import login_user

def test_new_project_require_name(client, login):
    resp = client.post(url_for('dashboard.new_project'), data={'name': ''})
    assert resp.status_code == 400
    assert resp.json['errors']['name']

def test_new_project_success(client, login):
    resp = client.post(url_for('dashboard.new_project'), data={'name': 'New Project'})
    assert resp.status_code == 200
    assert resp.json['id']
    assert resp.json['name'] == 'New Project'
    assert resp.json['slug'] == 'New-Project'
    assert resp.json['owner_id'] == login.id

def test_new_project_twice(client, login):
    resp = client.post(url_for('dashboard.new_project'), data={'name': 'New Project'})
    resp = client.post(url_for('dashboard.new_project'), data={'name': 'New Project'})
    assert resp.status_code == 400
    assert resp.json['errors']['name'] == ['duplicated slug.']

def test_new_project_sensive_slug(client, login):
    resp = client.post(url_for('dashboard.new_project'), data={'name': 'New Project'})
    assert resp.json['slug'] == 'New-Project'
    resp = client.post(url_for('dashboard.new_project'), data={'name': 'new project'})
    assert resp.json['slug'] == 'new-project'
