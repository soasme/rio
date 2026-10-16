"""Rio: a coding agent with durable state.

- ``rio.ai`` sends one model request and returns the final message.
- ``rio.agent`` owns SQLite state, journaling, and recovery.
- ``rio.coding`` provides coding cells, process execution, model configuration, and credentials.
- ``rio.cli`` runs coding tasks.
"""
