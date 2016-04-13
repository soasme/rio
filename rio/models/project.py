# -*- coding: utf-8 -*-
"""
rio.models.project
~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime

from rio.core import db

class Project(db.Model):
    """
    Projects are permission based namespaces which generally
    are the top level entry point for all data.
    """

    class Status:
        VISIBLE = 0
        HIDDEN = 1

    __table_name__ = 'project'
    __table_args__ = (
        db.UniqueConstraint('slug', name='ux_project_slug'),
    )

    id = db.Column(db.Integer(), primary_key=True)
    slug = db.Column(db.String(64), nullable=False)
    name = db.Column(db.String(128), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    status = db.Column(db.SmallInteger(), nullable=False, default=Status.VISIBLE)
    created_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)

    actions = db.relationship('Action', backref=db.backref('project'), lazy='dynamic')
    senders = db.relationship('Sender', backref=db.backref('project'), lazy='dynamic')

    def __unicode__(self):
        return u'%s (%s)' % (self.name, self.slug)

    def to_simple_dict(self):
        return dict(
            id=self.id,
            slug=self.slug,
            name=self.name,
        )
