# -*- coding: utf-8 -*-
"""
rio.blueprints.api_v1.views
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Implement of rio api v1 view functions.
"""

from flask import jsonify
from .core import bp


@bp.route('/webhooks')
def get_webhooks():
    """Get webhook list."""
    pass


@bp.route('/webhooks', methods=['POST'])
def add_webhook():
    """Add a webhook."""
    pass


@bp.route('/webhooks/<int:webhook_id>')
def get_webhook(webhook_id):
    """Get a webhook."""
    pass


@bp.route('/webhooks/<int:webhook_id>', methods=['PUT'])
def update_webhook(webhook_id):
    """Update webhook."""
    pass


@bp.route('/webhooks/<int:webhook_id>', methods=['DELETE'])
def delete_webhook(webhook_id):
    """Delete webhook."""
    pass


@bp.route('/publish/<topic>', methods=['POST'])
def publish(topic):
    """Publish message to topic."""
    pass
