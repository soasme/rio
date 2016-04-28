# -*- coding: utf-8 -*-
"""
rio.models.service
~~~~~~~~~~~~~~~~~~
"""

from datetime import datetime

from rio.core import db

class Service(db.Model):

    __tablename__ = 'service'

    __table_args__ = (
        db.UniqueConstraint('project_id', 'host', 'port', name='ux_service_project_host_port'),
    )

    class Type(object):
        HTTP = 0

    id = db.Column(db.Integer(), primary_key=True)
    project_id = db.Column(db.Integer(), db.ForeignKey('project.id'), nullable=False)
    type = db.Column(db.SmallInteger(), nullable=False, default=Type.HTTP)
    host = db.Column(db.String(128), nullable=False)
    port = db.Column(db.SmallInteger(), nullable=False, default=80)
    secret = db.Column(db.String(128), nullable=False)
    created_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)
    updated_at =  db.Column(db.DateTime(), nullable=False,
                            default=datetime.utcnow, onupdate=datetime.utcnow)


    def to_simple_dict(self):
        return dict(
            type=self.Type[self.type],
            host=self.host,
            port=self.port,
            secret=self.secret,
        )
