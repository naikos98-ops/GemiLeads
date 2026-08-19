from functools import wraps
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.urls import reverse


def superadmin_required(view_func):
    """
    Decorator for views that checks if the user is logged in and is a superuser.
    Non-superusers get HTTP 403 Permission Denied.
    Unauthenticated users are redirected to the login page.
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"{reverse('login')}?next={request.path}")
        if not request.user.is_superuser:
            raise PermissionDenied("Πρόσβαση μόνο για Superadmin.")
        return view_func(request, *args, **kwargs)
    return _wrapped_view
