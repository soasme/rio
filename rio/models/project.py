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

    __table_name__ = 'rio_project'
    __table_args__ = (
        db.UniqueConstraint('slug', name='ux_project_slug'),
    )

    id = db.Column(db.Integer(), primary_key=True)
    slug = db.Column(db.String(32), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('rio_user.id'), nullable=False)
    status = db.Column(db.SmallInteger(), nullable=False, default=Status.VISIBLE)
    created_at = db.Column(db.DateTime(), default=datetime.utcnow)

    topics = db.relationship('Topic', backref=db.backref('project'), lazy='dynamic')

    def __unicode__(self):
        return u'%s (%s)' % (self.name, self.slug)

    def to_simple_dict(self):
        return dict(
            slug=self.slug,
            name=self.name,
            status=self.status,
        )
