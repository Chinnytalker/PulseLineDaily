
try:
    from decouple import config
except ImportError:
    def config(key, default=None):
        import os
        return os.environ.get(key, default)
