"""CoEgent's built-in binding; third-party executors live in their own packages."""
from coegent_executor_adapters import ExecutorRegistry


def default_registry():
    registry = ExecutorRegistry(discover_plugins=True)

    def native(context, options):
        from .native import CoEgentExecutorAdapter
        return CoEgentExecutorAdapter(context, options)

    registry.register('coegent', native)
    return registry
