# -*- coding: utf-8 -*-
"""
rio.models.sender
~~~~~~~~~~~~~~~~~
"""

from datetime import datetime

from sqlalchemy import func

from rio.core import db

class Sender(db.Model):
    """to validate trusted senders."""

    __table_name__ = 'rio_sender'

    id = db.Column(db.Integer(), primary_key=True)
    slug = db.Column(db.String(64), nullable=False)
    token = db.Column(db.String(40), nullable=False)
    created_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime(), nullable=False, default=datetime.utcnow)

def validate(slug, token):
    return bool(Sender.query.filter_by(slug=slug, token=token).value(func.Count(Sender.id)))
