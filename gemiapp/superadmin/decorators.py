from functools import wraps

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.urls import reverse


def is_superadmin(user):
    """Superuser rights AND an address on the reserved list.

    Both are required on every request rather than only when the account is created, so
    that flipping is_superuser directly in the database grants nothing on its own. The
    list lives in settings, which the web interface cannot edit.
    """
    if not user.is_authenticated or not user.is_superuser:
        return False
    return (user.email or user.username or "").strip().lower() in settings.SUPERADMIN_EMAILS


def superadmin_required(view_func):
    """
    Decorator for views that checks if the user is logged in and is a superadmin.
    Non-superadmins get HTTP 403 Permission Denied.
    Unauthenticated users are redirected to the login page.
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"{reverse('login')}?next={request.path}")
        if not is_superadmin(request.user):
            raise PermissionDenied("Πρόσβαση μόνο για Superadmin.")
        return view_func(request, *args, **kwargs)
    return _wrapped_view
