# -*- coding: utf-8 -*-


from datetime import datetime

from rio.core import db


class Environ(db.Model):

    __tablename__ = 'environ'

    id = db.Column(db.Integer(), primary_key=True)
    stage_id = db.Column(db.Integer(), db.ForeignKey('stage.id'), nullable=False)
    key = db.Column(db.String(64), nullable=False)
    value = db.Column(db.String(1024), nullable=False)
    created_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False,
                           default=datetime.utcnow, onupdate=datetime.utcnow)


class Stage(db.Model):

    __tablename__ = 'environ_stage'

    id = db.Column(db.Integer(), primary_key=True)
    project_id = db.Column(db.Integer(), db.ForeignKey('project.id'), nullable=False)
    slug = db.Column(db.String(64), nullable=False)
    name = db.Column(db.String(128), nullable=False)
    created_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False,
                           default=datetime.utcnow, onupdate=datetime.utcnow)
