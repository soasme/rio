# -*- coding: utf-8 -*-

from pytest import fixture
from flask import url_for

@fixture
def login(client, owner):
    client.post(
        url_for('user.login'),
        data={'username': 'owner', 'password': '*'},
        follow_redirects=True
    )
    return owner
