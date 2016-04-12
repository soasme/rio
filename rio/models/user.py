# -*- coding: utf-8 -*-

from datetime import datetime

from flask_user import UserMixin

from rio.core import db

class User(db.Model, UserMixin):

    __table_name__ = 'user'
    __table_args__ = (
        db.UniqueConstraint('username', name='ux_user_username'),
        db.UniqueConstraint('email', name='ux_user_email'),
        db.UniqueConstraint('mobile', name='ux_user_mobile'),
        db.UniqueConstraint('auth_token', name='ux_user_auth_token'),
    )

    id = db.Column(db.Integer(), primary_key=True)
    created_at = db.Column(db.DateTime(), default=datetime.utcnow)
    confirmed_at = db.Column(db.DateTime())
    username = db.Column(db.String(64), nullable=False)
    nickname = db.Column(db.String(32), nullable=False, server_default='')
    mobile = db.Column(db.String(11))
    email = db.Column(db.String(128), nullable=False)
    password = db.Column(db.String(128), nullable=False, server_default='')
    reset_password_token = db.Column(db.String(128), nullable=False, server_default='')
    auth_token = db.Column(db.String(128))
    active = db.Column(db.Boolean(), nullable=False, server_default='0')

    projects = db.relationship('Project', backref=db.backref('owner'), lazy='dynamic')

    def __unicode__(self):
        return 'user (%s)' % self.username
