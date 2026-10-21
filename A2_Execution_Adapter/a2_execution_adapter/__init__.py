"""Lazy A2 executor plugin. Importing this module does not import ROS."""

def factory(context, options):
    from .adapter import A2ExecutorAdapter
    return A2ExecutorAdapter(context, options)
