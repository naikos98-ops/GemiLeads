from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from gemiapp.views import RateLimitedLoginView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", RateLimitedLoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("password_reset/", auth_views.PasswordResetView.as_view(), name="password_reset"),
    path("password_reset/done/", auth_views.PasswordResetDoneView.as_view(), name="password_reset_done"),
    path("reset/<uidb64>/<token>/", auth_views.PasswordResetConfirmView.as_view(), name="password_reset_confirm"),
    path("reset/done/", auth_views.PasswordResetCompleteView.as_view(), name="password_reset_complete"),
    path("", include("gemiapp.urls")),
]
