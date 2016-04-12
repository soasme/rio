# -*- coding: utf-8 -*-

import json

import pytest
import requests_mock
from flask import url_for


def test_emit_topic_to_undefined_project_slug(client, sender_basic_token, topic):
    url = url_for('event.emit_topic', project_slug='non-exist', topic_slug=topic.slug)
    headers = {'Authorization': 'Basic %s' % sender_basic_token}
    resp = client.get(url, headers=headers)
    assert resp.status_code == 404

def test_emit_topic_to_undefined_topic_slug(client, sender_basic_token, project):
    url = url_for('event.emit_topic', project_slug=project.slug, topic_slug='non-exist')
    headers = {'Authorization': 'Basic %s' % sender_basic_token}
    resp = client.get(url, headers=headers)
    assert resp.status_code == 404


def test_emit_topic_but_sender_has_not_logined(client, project, topic):
    url = url_for('event.emit_topic', project_slug=project.slug, topic_slug=topic.slug)
    resp = client.get(url)
    assert resp.status_code == 401


def test_emit_topic_success(client, project, topic, webhook, sender_basic_token):
    url = url_for('event.emit_topic', project_slug=project.slug, topic_slug=topic.slug)
    headers = {'Authorization': 'Basic %s' % sender_basic_token}
    with requests_mock.Mocker() as m:
        m.get('http://example.org?key=value', text='data')
        resp = client.get(url+'?key=value', headers=headers)
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data['message'] == 'ok'
    assert data['event']['uuid']


def test_emit_topic_but_webhook_ran_failed(client, project, topic, webhook, sender_basic_token):
    url = url_for('event.emit_topic', project_slug=project.slug, topic_slug=topic.slug)
    headers = {'Authorization': 'Basic %s' % sender_basic_token}
    with requests_mock.Mocker() as m:
        m.get('http://example.org?key=value', text='failed', status_code=500)
        resp = client.get(url+'?key=value', headers=headers)
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data['message'] == 'ok'
    assert data['event']['uuid']
