import logging

from django.core.cache import cache as django_cache
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


def safe_cache_get(key, default=None):
    try:
        return django_cache.get(key, default)
    except RedisError as exc:
        logger.warning("cache.get failed (%s): %s", key, exc)
        return default


def safe_cache_set(key, value, timeout=None):
    try:
        django_cache.set(key, value, timeout=timeout)
    except RedisError as exc:
        logger.warning("cache.set failed (%s): %s", key, exc)


def safe_cache_delete(key):
    try:
        django_cache.delete(key)
    except RedisError as exc:
        logger.warning("cache.delete failed (%s): %s", key, exc)
