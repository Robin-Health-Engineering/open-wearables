"""Wire names of Celery tasks, importable without importing the tasks themselves.

The task modules import ProviderFactory, so anything the factory reaches — a
strategy, a provider service — cannot import them back. Referencing a name from
here keeps send_task callers free of that cycle.
"""

REGISTER_PROVIDER_WEBHOOKS_TASK = (
    "app.integrations.celery.tasks.register_provider_webhooks_task.register_provider_webhooks"
)
SYNC_PROVIDER_USER_SUBSCRIPTION_TASK = (
    "app.integrations.celery.tasks.register_provider_webhooks_task.sync_provider_user_subscription"
)
